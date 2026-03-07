"""Tests for MCP reconnection behavior."""

from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from benchclaw.agent.tools.mcp_manager import MCPManager, MCPServerConfig


class _FakeTool:
    def __init__(self, name: str, description: str = "", input_schema: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema


class _FakeSession:
    def __init__(self, tool_name: str, text: str, fail_once: bool = False):
        self._tool_name = tool_name
        self._text = text
        self._fail_once = fail_once
        self.call_count = 0

    async def list_tools(self):
        return SimpleNamespace(
            tools=[_FakeTool(name=self._tool_name, description="fake", input_schema={})]
        )

    async def call_tool(self, name: str, arguments: dict):
        self.call_count += 1
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("connection dropped")

        return SimpleNamespace(
            content=[TextContent(type="text", text=f"{name}:{arguments.get('value', self._text)}")]
        )


@pytest.mark.asyncio
async def test_mcp_manager_retries_initial_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = MCPServerConfig(name="demo", transport="http", url="https://example.test/mcp")
    manager = MCPManager([cfg])
    manager.RETRY_DELAY_S = 0

    attempts = 0

    async def fake_open_session(self, cfg: MCPServerConfig, exit_stack):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary connect failure")
        return _FakeSession(tool_name="echo", text="ok")

    monkeypatch.setattr(MCPManager, "_open_session", fake_open_session)

    async with manager:
        assert "echo" in manager
        assert manager.get_definitions()[0]["function"]["name"] == "echo"

    assert attempts == 3


@pytest.mark.asyncio
async def test_mcp_manager_reconnects_and_retries_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = MCPServerConfig(name="demo", transport="http", url="https://example.test/mcp")
    manager = MCPManager([cfg])
    manager.RETRY_DELAY_S = 0

    sessions = iter(
        [
            _FakeSession(tool_name="echo", text="first", fail_once=True),
            _FakeSession(tool_name="echo", text="recovered"),
        ]
    )

    async def fake_open_session(self, cfg: MCPServerConfig, exit_stack):
        return next(sessions)

    monkeypatch.setattr(MCPManager, "_open_session", fake_open_session)

    async with manager:
        result = await manager.execute("echo", {"value": "payload"})

    assert result == "echo:payload"
