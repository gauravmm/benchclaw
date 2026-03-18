"""MCP (Model Context Protocol) server manager for benchclaw."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any, Callable, Literal

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from pydantic import BaseModel, model_validator


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str
    transport: Literal["stdio", "http"]

    # stdio fields
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}

    # http field
    url: str | None = None

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("MCP stdio server requires 'command'")
        if self.transport == "http" and not self.url:
            raise ValueError("MCP http server requires 'url'")
        return self


class _MCPLiveConnection:
    """Single-use MCP session and discovered tool set."""

    def __init__(self, server: "_MCPServerSlot") -> None:
        self._server = server
        self.config = server.config
        self._exit_stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list[Any] = []

    async def __aenter__(self) -> "_MCPLiveConnection":
        exit_stack = AsyncExitStack()
        await exit_stack.__aenter__()
        try:
            session = await self._server._open_session(exit_stack)
            tools_response = await session.list_tools()
        except Exception:
            await exit_stack.__aexit__(None, None, None)
            raise

        self._exit_stack = exit_stack
        self.session = session
        self.tools = list(tools_response.tools)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        exit_stack = self._exit_stack
        self._exit_stack = None
        self.session = None
        self.tools.clear()
        if exit_stack is not None:
            await exit_stack.__aexit__(*exc_info)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        assert self.session is not None
        return await self.session.call_tool(tool_name, arguments=arguments)

    async def run_until_cancelled(
        self,
        startup_future: asyncio.Future[None],
        on_ready: Callable[[], None],
        on_closed: Callable[[], None],
    ) -> None:
        """Own one MCP connection for its full lifetime in a single task."""
        try:
            async with self:
                on_ready()
                if not startup_future.done():
                    startup_future.set_result(None)

                logger.info(f"MCP: connected to '{self.config.name}', {len(self.tools)} tools")

                await asyncio.Future()
        except asyncio.CancelledError:
            if not startup_future.done():
                startup_future.set_exception(
                    RuntimeError("MCP server task cancelled during startup")
                )
            raise
        except Exception as e:
            if not startup_future.done():
                startup_future.set_exception(e)
            raise
        finally:
            on_closed()


class _MCPServerSlot:
    """Restartable controller for one configured MCP server."""

    def __init__(self, manager: "MCPManager", config: MCPServerConfig) -> None:
        self._manager = manager
        self.config = config
        self.task: asyncio.Task[None] | None = None
        self.connection: _MCPLiveConnection | None = None
        self.lock = asyncio.Lock()

    @property
    def tools(self) -> list[Any]:
        if self.connection is None:
            return []
        return self.connection.tools

    def _set_connection(self, connection: _MCPLiveConnection) -> None:
        self.connection = connection
        self._manager._rebuild_tool_index()

    def _clear_connection(self) -> None:
        if self.task is asyncio.current_task():
            self.task = None
            self.connection = None
            self._manager._rebuild_tool_index()

    async def _open_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        """Open a transport + ClientSession and return the initialized session."""
        if self.config.transport == "stdio":
            params = StdioServerParameters(
                command=self.config.command,  # type: ignore[arg-type]
                args=self.config.args,
                env=self.config.env or None,
            )
            transport = await exit_stack.enter_async_context(stdio_client(params))
            read, write = transport
        else:
            transport = await exit_stack.enter_async_context(
                streamable_http_client(self.config.url)  # type: ignore[arg-type]
            )
            read, write, *_ = transport

        session = await exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _start_task(self) -> None:
        """Start a fresh task-owned MCP connection and wait for it to initialize."""
        startup_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        connection = _MCPLiveConnection(self)
        task = asyncio.create_task(
            connection.run_until_cancelled(
                startup_future,
                on_ready=lambda: self._set_connection(connection),
                on_closed=self._clear_connection,
            ),
            name=f"mcp:{self.config.name}",
        )
        self.task = task

        try:
            await startup_future
        except Exception:
            try:
                await task
            except Exception:
                pass
            raise

    async def _stop_task(self) -> None:
        """Cancel the current MCP server task and wait for task-local cleanup."""
        task = self.task
        if task is None:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"MCP: error while stopping '{self.config.name}': {e}")

    async def _connect_with_retries(self, reason: str) -> None:
        """Connect or reconnect this server with bounded retries and backoff."""
        delay = self._manager.RETRY_DELAY_S
        last_error: Exception | None = None

        for attempt in range(1, self._manager.CONNECT_RETRIES + 1):
            try:
                await self._stop_task()
                await self._start_task()
                return
            except Exception as e:
                last_error = e
                logger.warning(
                    f"MCP: {reason} failed for '{self.config.name}' "
                    f"(attempt {attempt}/{self._manager.CONNECT_RETRIES}): {e}"
                )
                if attempt < self._manager.CONNECT_RETRIES:
                    await asyncio.sleep(delay)
                    delay *= 2

        assert last_error is not None
        raise last_error

    async def _call_connected_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        connection = self.connection
        assert connection is not None
        return await connection.call_tool(tool_name, arguments)

    async def start(self, reason: str) -> None:
        async with self.lock:
            await self._connect_with_retries(reason)

    async def stop(self) -> None:
        async with self.lock:
            await self._stop_task()

    async def execute(self, public_name: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with self.lock:
            if self.connection is None:
                await self._connect_with_retries("session restore")

            try:
                return await self._call_connected_tool(tool_name, arguments)
            except Exception as e:
                logger.warning(
                    f"MCP: call to '{public_name}' on '{self.config.name}' failed, reconnecting: {e}"
                )
                await self._connect_with_retries("reconnect after tool failure")

                refreshed = self._manager._tool_map.get(public_name)
                if refreshed is None or refreshed[0] != self.config.name:
                    raise RuntimeError(
                        f"MCP tool '{public_name}' is no longer available after reconnect"
                    )

                _, refreshed_tool_name = refreshed
                return await self._call_connected_tool(refreshed_tool_name, arguments)


class MCPManager:
    """
    Manages connections to one or more MCP servers.

    Enter as an async context manager to connect to all configured servers
    and discover their tools. On exit, all connections are cleanly closed.
    """

    CONNECT_RETRIES = 3
    RETRY_DELAY_S = 1.0

    def __init__(self, server_configs: list[MCPServerConfig]) -> None:
        self._server_configs = server_configs
        server_names = [cfg.name for cfg in server_configs]
        duplicate_names = sorted({name for name in server_names if server_names.count(name) > 1})
        if duplicate_names:
            duplicates = ", ".join(duplicate_names)
            raise ValueError(f"Duplicate MCP server names are not allowed: {duplicates}")

        self._servers = {cfg.name: _MCPServerSlot(self, cfg) for cfg in server_configs}
        # public_name -> (server_name, original_tool_name_on_server)
        self._tool_map: dict[str, tuple[str, str]] = {}
        # public_name -> tool schema (OpenAI format)
        self._definitions: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "MCPManager":
        for server in self._servers.values():
            try:
                await server.start(reason="initial connect")
            except Exception as e:
                logger.warning(f"MCP: failed to connect to '{server.config.name}': {e}")

        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for server in self._servers.values():
            await server.stop()
        self._tool_map.clear()
        self._definitions.clear()

    def _rebuild_tool_index(self) -> None:
        """Rebuild public tool names from the currently known server tool lists."""
        self._tool_map.clear()
        self._definitions.clear()

        for server_name, state in self._servers.items():
            for tool in state.tools:
                public_name = f"{server_name}__{tool.name}"

                self._tool_map[public_name] = (server_name, tool.name)
                self._definitions[public_name] = {
                    "type": "function",
                    "function": {
                        "name": public_name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema,
                    },
                }

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return OpenAI-format tool schemas for all MCP tools."""
        return list(self._definitions.values())

    async def execute(self, public_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return its result as a string."""
        server_name, orig_name = self._tool_map[public_name]
        server = self._servers[server_name]

        result = await server.execute(public_name, orig_name, arguments)

        parts = [item.text for item in result.content if isinstance(item, TextContent)]
        return "\n".join(parts) if parts else "(no output)"

    def __contains__(self, name: str) -> bool:
        return name in self._tool_map
