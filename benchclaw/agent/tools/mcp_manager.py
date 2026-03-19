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

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.task: asyncio.Task[None] | None = None
        self.connection: _MCPLiveConnection | None = None
        self.lock = asyncio.Lock()

    def get_tools(self) -> list[Any]:
        return self.connection.tools if self.connection else []

    def has_tool(self, tool_name: str) -> bool:
        return any(tool.name == tool_name for tool in self.get_tools())

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
                self.connection = _MCPLiveConnection(
                    self.config,
                    session=session,
                    tools=list(tools_response.tools),
                )
                if not startup_future.done():
                    startup_future.set_result(None)

                logger.info(
                    f"MCP: connected to '{self.config.name}', {len(self.connection.tools)} tools"
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

    async def _stop_task(self) -> None:
        """Cancel the current MCP server task and wait for task-local cleanup."""
        try:
            if self.task and self.task.cancel():
                await self.task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"MCP: error while stopping '{self.config.name}': {e}")

    async def _connect_with_retries(
        self, reason: str, attempts: int = 3, delay: float = 2.0
    ) -> None:
        """Connect or reconnect this server with bounded retries and backoff."""

        for attempt in range(attempts):
            try:
                await self._stop_task()
                startup_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self.task = asyncio.create_task(
                    self._run_connection_task(startup_future), name=f"mcp:{self.config.name}"
                )
                try:
                    # Wait for the connection task to signal successful startup or failure:
                    await startup_future
                except Exception:
                    try:
                        await self.task
                    except Exception:
                        pass
                    raise
                return

            except Exception as e:
                logger.warning(
                    f"MCP: {reason} failed for '{self.config.name}' (attempt {attempt}/{attempts}): {e}"
                )
                if attempt < attempts - 1:
                    await asyncio.sleep(delay * 2**attempt)
                else:
                    raise RuntimeError(
                        f"MCP: failed to connect to '{self.config.name}' after {attempts} attempts"
                    ) from e

    async def start(self, reason: str) -> None:
        async with self.lock:
            await self._connect_with_retries(reason)

    async def stop(self) -> None:
        async with self.lock:
            await self._stop_task()

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with self.lock:
            if self.connection is None:
                await self._connect_with_retries("session restore")

            try:
                assert self.connection is not None, (
                    "connection not established after reconnect attempt 1, cannot call tool"
                )
                return await self.connection.call_tool(tool_name, arguments)
            except Exception as e:
                logger.warning(f"MCP: call to '{tool_name}' on '{self.config.name}' failed: {e}")


class MCPManager:
    """
    Manages connections to one or more MCP servers.

    Enter as an async context manager to connect to all configured servers
    and discover their tools. On exit, all connections are cleanly closed.
    """

    def __init__(self, server_configs: list[MCPServerConfig]) -> None:
        server_names = [cfg.name for cfg in server_configs]
        duplicate_names = sorted({name for name in server_names if server_names.count(name) > 1})
        if duplicate_names:
            duplicates = ", ".join(duplicate_names)
            raise ValueError(f"Duplicate MCP server names are not allowed: {duplicates}")

        self._servers = {cfg.name: _MCPServerSlot(cfg) for cfg in server_configs}

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
        for server_name, server in self._servers.items():
            for tool in server.get_tools():
                public_name = f"{server_name}__{tool.name}"
                definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": public_name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema,
                        },
                    }
                )
        return definitions

    def _split_public_name(self, public_name: str) -> tuple[_MCPServerSlot, str]:
        server_name, sep, tool_name = public_name.partition("__")
        if sep and (server := self._servers.get(server_name)):
            return server, tool_name
        raise KeyError(public_name)

    async def execute(self, public_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return its result as a string."""
        server, tool_name = self._split_public_name(public_name)

        result = await server.execute(tool_name, arguments)
        parts = [item.text for item in result.content if isinstance(item, TextContent)]
        return "\n".join(parts) if parts else "(no output)"

    def __contains__(self, name: str) -> bool:
        try:
            server, tool_name = self._split_public_name(name)
            return server.has_tool(tool_name)
        except KeyError:
            return False
