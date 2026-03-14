"""MCP (Model Context Protocol) server manager for benchclaw."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
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


@dataclass
class _MCPServerState:
    """Mutable connection state for one configured MCP server."""

    config: MCPServerConfig
    exit_stack: AsyncExitStack | None = None
    session: ClientSession | None = None
    tools: list[Any] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
        self._servers = {cfg.name: _MCPServerState(config=cfg) for cfg in server_configs}
        # public_name -> (server_name, original_tool_name_on_server)
        self._tool_map: dict[str, tuple[str, str]] = {}
        # public_name -> tool schema (OpenAI format)
        self._definitions: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "MCPManager":
        for cfg in self._server_configs:
            try:
                await self._connect_with_retries(self._servers[cfg.name], reason="initial connect")
            except Exception as e:
                logger.warning(f"MCP: failed to connect to '{cfg.name}': {e}")

        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for state in self._servers.values():
            if state.exit_stack is not None:
                try:
                    await state.exit_stack.__aexit__(*exc_info)
                except Exception as e:
                    # A reconnect in an address_loop task may have replaced the
                    # exit_stack with one entered in a different task; anyio
                    # cancel scopes are task-local so the exit fails here.
                    logger.debug(f"MCP: could not clean up '{state.config.name}' on exit: {e}")
                state.exit_stack = None
                state.session = None
        self._tool_map.clear()
        self._definitions.clear()
        for state in self._servers.values():
            state.tools.clear()

    async def _open_session(
        self, cfg: MCPServerConfig, exit_stack: AsyncExitStack
    ) -> ClientSession:
        """Open a transport + ClientSession and return the initialized session."""
        if cfg.transport == "stdio":
            params = StdioServerParameters(
                command=cfg.command,  # type: ignore[arg-type]
                args=cfg.args,
                env=cfg.env or None,
            )
            transport = await exit_stack.enter_async_context(stdio_client(params))
            read, write = transport
        else:
            transport = await exit_stack.enter_async_context(
                streamable_http_client(cfg.url)  # type: ignore[arg-type]
            )
            read, write, *_ = transport

        session = await exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _connect_server(self, state: _MCPServerState) -> None:
        """Establish a fresh session for one server and refresh its tool list."""
        new_stack = AsyncExitStack()
        await new_stack.__aenter__()
        try:
            session = await self._open_session(state.config, new_stack)
            tools_response = await session.list_tools()
        except Exception:
            await new_stack.__aexit__(None, None, None)
            raise

        old_stack = state.exit_stack
        state.exit_stack = new_stack
        state.session = session
        state.tools = list(tools_response.tools)
        self._rebuild_tool_index()

        if old_stack is not None:
            try:
                await old_stack.__aexit__(None, None, None)
            except Exception as e:
                # HTTP transports use anyio cancel scopes that are task-local;
                # reconnects happen in a different task so cleanup may fail.
                # The old connection is already broken, so this is safe to ignore.
                logger.debug(
                    f"MCP: could not clean up old connection for '{state.config.name}': {e}"
                )

        logger.info(f"MCP: connected to '{state.config.name}', {len(state.tools)} tools")

    async def _connect_with_retries(self, state: _MCPServerState, reason: str) -> None:
        """Connect or reconnect one server with bounded retries and backoff."""
        delay = self.RETRY_DELAY_S
        last_error: Exception | None = None

        for attempt in range(1, self.CONNECT_RETRIES + 1):
            try:
                await self._connect_server(state)
                return
            except Exception as e:
                last_error = e
                logger.warning(
                    f"MCP: {reason} failed for '{state.config.name}' "
                    f"(attempt {attempt}/{self.CONNECT_RETRIES}): {e}"
                )
                if attempt < self.CONNECT_RETRIES:
                    await asyncio.sleep(delay)
                    delay *= 2

        assert last_error is not None
        raise last_error

    def _rebuild_tool_index(self) -> None:
        """Rebuild public tool names from the currently known server tool lists."""
        self._tool_map.clear()
        self._definitions.clear()

        server_tools: list[tuple[str, Any]] = []
        raw_name_counts: dict[str, int] = {}

        for server_name, state in self._servers.items():
            for tool in state.tools:
                server_tools.append((server_name, tool))
                raw_name_counts[tool.name] = raw_name_counts.get(tool.name, 0) + 1

        for server_name, tool in server_tools:
            if raw_name_counts[tool.name] > 1:
                public_name = f"{server_name}__{tool.name}"
            else:
                public_name = tool.name

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
        state = self._servers[server_name]

        async with state.lock:
            if state.session is None:
                await self._connect_with_retries(state, reason="session restore")

            assert state.session is not None
            try:
                result = await state.session.call_tool(orig_name, arguments=arguments)
            except Exception as e:
                logger.warning(
                    f"MCP: call to '{public_name}' on '{server_name}' failed, reconnecting: {e}"
                )
                await self._connect_with_retries(state, reason="reconnect after tool failure")

                refreshed = self._tool_map.get(public_name)
                if refreshed is None or refreshed[0] != server_name:
                    raise RuntimeError(
                        f"MCP tool '{public_name}' is no longer available after reconnect"
                    )

                _, refreshed_orig_name = refreshed
                assert state.session is not None
                result = await state.session.call_tool(refreshed_orig_name, arguments=arguments)

        parts = [item.text for item in result.content if isinstance(item, TextContent)]
        return "\n".join(parts) if parts else "(no output)"

    def __contains__(self, name: str) -> bool:
        return name in self._tool_map
