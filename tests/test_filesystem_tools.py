"""Tests for filesystem search tools."""

from pathlib import Path

import pytest

from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.filesystem import GlobTool, GrepTool


@pytest.mark.asyncio
async def test_glob_returns_workspace_relative_matches(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "one.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "alpha" / "two.md").write_text("two\n", encoding="utf-8")

    tool = GlobTool()
    ctx = ToolContext(workspace=tmp_path)

    result = await tool.execute(ctx, pattern="**/*.txt")

    assert result == "alpha/one.txt"


@pytest.mark.asyncio
async def test_grep_searches_workspace_relative_directory(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("register_tool('x')\nignore me\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("no match here\n", encoding="utf-8")
    (tmp_path / "pkg" / "notes.txt").write_text("register_tool in text\n", encoding="utf-8")

    tool = GrepTool()
    ctx = ToolContext(workspace=tmp_path)

    result = await tool.execute(ctx, pattern="register_tool", path="pkg", file_pattern="*.py")

    assert result == "pkg/a.py:1: register_tool('x')"


@pytest.mark.asyncio
async def test_grep_supports_regex_on_single_file(tmp_path: Path) -> None:
    file_path = tmp_path / "app.log"
    file_path.write_text("INFO start\nERROR failed\n", encoding="utf-8")

    tool = GrepTool()
    ctx = ToolContext(workspace=tmp_path)

    result = await tool.execute(ctx, pattern="^ERROR", path="app.log", is_regex=True)

    assert result == "app.log:2: ERROR failed"
