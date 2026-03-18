"""Tests for MCP reconnection behavior."""

import asyncio
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
    def __init__(
        self,
        tool_name: str,
        text: str,
        fail_once: bool = False,
        fail_times: int = 0,
    ):
        self._tool_name = tool_name
        self._text = text
        self._fail_once = fail_once
        self._fail_times = fail_times
        self.call_count = 0

    async def list_tools(self):
        return SimpleNamespace(
            tools=[_FakeTool(name=self._tool_name, description="fake", input_schema={})]
        )

    async def call_tool(self, name: str, arguments: dict):
        self.call_count += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("connection dropped")
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("connection dropped")

        return SimpleNamespace(
            content=[TextContent(type="text", text=f"{name}:{arguments.get('value', self._text)}")]
        )


class _TaskBoundContext:
    def __init__(self):
        self.enter_task = None
        self.exit_task = None

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_task = asyncio.current_task()
        if self.exit_task is not self.enter_task:
            raise RuntimeError("context exited in different task")


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


@pytest.mark.asyncio
async def test_mcp_manager_reconnect_cleans_up_in_same_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = MCPServerConfig(name="demo", transport="http", url="https://example.test/mcp")
    manager = MCPManager([cfg])
    manager.RETRY_DELAY_S = 0

    contexts: list[_TaskBoundContext] = []
    sessions = iter(
        [
            _FakeSession(tool_name="echo", text="first", fail_once=True),
            _FakeSession(tool_name="echo", text="recovered"),
        ]
    )

    async def fake_open_session(self, cfg: MCPServerConfig, exit_stack):
        context = _TaskBoundContext()
        contexts.append(context)
        await exit_stack.enter_async_context(context)
        return next(sessions)

    monkeypatch.setattr(MCPManager, "_open_session", fake_open_session)

    async with manager:
        result = await manager.execute("echo", {"value": "payload"})

    assert result == "echo:payload"
    assert len(contexts) == 2
    assert all(context.enter_task is context.exit_task for context in contexts)


@pytest.mark.asyncio
async def test_mcp_manager_retries_tool_call_at_most_once_after_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = MCPServerConfig(name="demo", transport="http", url="https://example.test/mcp")
    manager = MCPManager([cfg])
    manager.RETRY_DELAY_S = 0

    first_session = _FakeSession(tool_name="echo", text="first", fail_once=True)
    second_session = _FakeSession(tool_name="echo", text="second", fail_times=1)
    sessions = iter([first_session, second_session])

    async def fake_open_session(self, cfg: MCPServerConfig, exit_stack):
        return next(sessions)

    monkeypatch.setattr(MCPManager, "_open_session", fake_open_session)

    async with manager:
        with pytest.raises(RuntimeError, match="connection dropped"):
            await manager.execute("echo", {"value": "payload"})

    assert first_session.call_count == 1
    assert second_session.call_count == 1
