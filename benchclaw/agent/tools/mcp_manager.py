"""MCP (Model Context Protocol) server manager for benchclaw."""

from __future__ import annotations

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


class MCPManager:
    """
    Manages connections to one or more MCP servers.

    Enter as an async context manager to connect to all configured servers
    and discover their tools. On exit, all connections are cleanly closed.
    """

    def __init__(self, server_configs: list[MCPServerConfig]) -> None:
        self._server_configs = server_configs
        self._exit_stack = AsyncExitStack()
        # public_name -> (session, original_tool_name_on_server)
        self._tool_map: dict[str, tuple[ClientSession, str]] = {}
        # public_name -> tool schema (OpenAI format)
        self._definitions: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "MCPManager":
        await self._exit_stack.__aenter__()

        # Collect all (server_name, session, tool) tuples
        server_tools: list[tuple[str, ClientSession, Any]] = []
        for cfg in self._server_configs:
            try:
                session = await self._connect(cfg)
                tools_response = await session.list_tools()
                for tool in tools_response.tools:
                    server_tools.append((cfg.name, session, tool))
                logger.info(f"MCP: connected to '{cfg.name}', {len(tools_response.tools)} tools")
            except Exception as e:
                logger.warning(f"MCP: failed to connect to '{cfg.name}': {e}")

        # Resolve name conflicts: if same tool name appears on >1 server, prefix all
        raw_name_counts: dict[str, int] = {}
        for _, _, tool in server_tools:
            raw_name_counts[tool.name] = raw_name_counts.get(tool.name, 0) + 1

        for server_name, session, tool in server_tools:
            if raw_name_counts[tool.name] > 1:
                public_name = f"{server_name}__{tool.name}"
            else:
                public_name = tool.name

            # Warn if shadowing a built-in (caller checks after registry is built)
            self._tool_map[public_name] = (session, tool.name)
            self._definitions[public_name] = {
                "type": "function",
                "function": {
                    "name": public_name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }

        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._exit_stack.__aexit__(*exc_info)
        self._tool_map.clear()
        self._definitions.clear()

    async def _connect(self, cfg: MCPServerConfig) -> ClientSession:
        """Open a transport + ClientSession and return the initialized session."""
        if cfg.transport == "stdio":
            params = StdioServerParameters(
                command=cfg.command,  # type: ignore[arg-type]
                args=cfg.args,
                env=cfg.env or None,
            )
            transport = await self._exit_stack.enter_async_context(stdio_client(params))
            read, write = transport
        else:
            transport = await self._exit_stack.enter_async_context(
                streamable_http_client(cfg.url)  # type: ignore[arg-type]
            )
            read, write, *_ = transport

        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return OpenAI-format tool schemas for all MCP tools."""
        return list(self._definitions.values())

    async def execute(self, public_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return its result as a string."""
        session, orig_name = self._tool_map[public_name]
        result = await session.call_tool(orig_name, arguments=arguments)
        parts = [item.text for item in result.content if isinstance(item, TextContent)]
        return "\n".join(parts) if parts else "(no output)"

    def __contains__(self, name: str) -> bool:
        return name in self._tool_map
