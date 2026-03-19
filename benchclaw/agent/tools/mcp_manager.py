"""MCP (Model Context Protocol) server manager for benchclaw."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any, Literal

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
    """Runtime state for one live MCP session."""

    def __init__(self, config: MCPServerConfig, session: ClientSession, tools: list[Any]) -> None:
        self.config = config
        self.session = session
        self.tools = tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return await self.session.call_tool(tool_name, arguments=arguments)


class _MCPServerSlot:
    """Restartable controller for one configured MCP server."""

    def __init__(self, manager: "MCPManager", config: MCPServerConfig) -> None:
        self._manager = manager
        self.config = config
        self.task: asyncio.Task[None] | None = None
        self.connection: _MCPLiveConnection | None = None
        self.lock = asyncio.Lock()
        self._tool_map: dict[str, str] = {}
        self._definitions: dict[str, dict[str, Any]] = {}

    def get_definitions(self) -> list[dict[str, Any]]:
        return list(self._definitions.values())

    def get_tool_name(self, public_name: str) -> str | None:
        return self._tool_map.get(public_name)

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
            assert self.config.url is not None
            transport = await exit_stack.enter_async_context(
                streamable_http_client(self.config.url)
            )
            read, write, *_ = transport

        session = await exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _run_connection_task(self, startup_future: asyncio.Future[None]) -> None:
        """Own one MCP connection for its full lifetime in a single task."""
        try:
            async with AsyncExitStack() as exit_stack:
                session = await self._open_session(exit_stack)
                tools_response = await session.list_tools()
                connection = _MCPLiveConnection(
                    self.config,
                    session=session,
                    tools=list(tools_response.tools),
                )
                self.connection = connection
                self._tool_map.clear()
                self._definitions.clear()
                for tool in connection.tools:
                    public_name = f"{self.config.name}__{tool.name}"
                    self._tool_map[public_name] = tool.name
                    self._definitions[public_name] = {
                        "type": "function",
                        "function": {
                            "name": public_name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema,
                        },
                    }
                if not startup_future.done():
                    startup_future.set_result(None)

                logger.info(
                    f"MCP: connected to '{self.config.name}', {len(connection.tools)} tools"
                )

                await asyncio.Future()
        except Exception as e:
            if not startup_future.done():
                startup_future.set_exception(e)
            raise
        finally:
            if self.task is asyncio.current_task():
                self.task = None
                self.connection = None
                self._tool_map.clear()
                self._definitions.clear()

    async def _stop_task(self) -> None:
        """Cancel the current MCP server task and wait for task-local cleanup."""
        try:
            if self.task and self.task.cancel():
                await self.task
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
                startup_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self.task = asyncio.create_task(
                    self._run_connection_task(startup_future), name=f"mcp:{self.config.name}"
                )
                try:
                    await startup_future
                except Exception:
                    try:
                        await self.task
                    except Exception:
                        pass
                    raise
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

    async def start(self, reason: str) -> None:
        async with self.lock:
            await self._connect_with_retries(reason)

    async def stop(self) -> None:
        async with self.lock:
            await self._stop_task()

    async def execute(self, public_name: str, arguments: dict[str, Any]) -> Any:
        async with self.lock:
            if self.connection is None:
                await self._connect_with_retries("session restore")

            resolved_tool_name = self.get_tool_name(public_name)
            if resolved_tool_name is None:
                raise RuntimeError(
                    f"MCP tool '{public_name}' is not available on '{self.config.name}'"
                )

            try:
                assert self.connection is not None
                return await self.connection.call_tool(resolved_tool_name, arguments)
            except Exception as e:
                logger.warning(
                    f"MCP: call to '{public_name}' on '{self.config.name}' failed, reconnecting: {e}"
                )
                await self._connect_with_retries("reconnect after tool failure")

                refreshed_tool_name = self.get_tool_name(public_name)
                if refreshed_tool_name is None:
                    raise RuntimeError(
                        f"MCP tool '{public_name}' is no longer available after reconnect"
                    )

                assert self.connection is not None
                return await self.connection.call_tool(refreshed_tool_name, arguments)


class MCPManager:
    """
    Manages connections to one or more MCP servers.

    Enter as an async context manager to connect to all configured servers
    and discover their tools. On exit, all connections are cleanly closed.
    """

    CONNECT_RETRIES = 3
    RETRY_DELAY_S = 1.0

    def __init__(self, server_configs: list[MCPServerConfig]) -> None:
        server_names = [cfg.name for cfg in server_configs]
        duplicate_names = sorted({name for name in server_names if server_names.count(name) > 1})
        if duplicate_names:
            duplicates = ", ".join(duplicate_names)
            raise ValueError(f"Duplicate MCP server names are not allowed: {duplicates}")

        self._servers = {cfg.name: _MCPServerSlot(self, cfg) for cfg in server_configs}

    async def __aenter__(self) -> "MCPManager":
        servers = list(self._servers.values())
        results = await asyncio.gather(
            *(server.start(reason="initial connect") for server in servers),
            return_exceptions=True,
        )
        for server, result in zip(servers, results):
            if isinstance(result, Exception):
                logger.warning(f"MCP: failed to connect to '{server.config.name}': {result}")

        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await asyncio.gather(*(server.stop() for server in self._servers.values()))

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return OpenAI-format tool schemas for all MCP tools."""
        definitions: list[dict[str, Any]] = []
        for server in self._servers.values():
            definitions.extend(server.get_definitions())
        return definitions

    def _find_server_for_tool(self, public_name: str) -> _MCPServerSlot | None:
        server_name, sep, _ = public_name.partition("__")
        if not sep:
            return None
        return self._servers.get(server_name)

    async def execute(self, public_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return its result as a string."""
        server = self._find_server_for_tool(public_name)
        if server is None:
            raise KeyError(public_name)

        result = await server.execute(public_name, arguments)

        parts = [item.text for item in result.content if isinstance(item, TextContent)]
        return "\n".join(parts) if parts else "(no output)"

    def __contains__(self, name: str) -> bool:
        return self._find_server_for_tool(name) is not None
