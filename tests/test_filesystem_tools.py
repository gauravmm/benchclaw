"""Tests for filesystem search tools."""

from pathlib import Path

import pytest

from benchclaw.agent.tools.base import _TOOL_REGISTRY, ToolContext
from benchclaw.agent.tools.filesystem import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    WriteFileTool,
)


def test_list_dir_tool_is_not_registered() -> None:
    assert "list_dir" not in _TOOL_REGISTRY


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


@pytest.mark.asyncio
async def test_write_existing_file_requires_prior_read(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("old\n", encoding="utf-8")

    tool = WriteFileTool()
    ctx = ToolContext(workspace=tmp_path)

    with pytest.raises(RuntimeError, match="has not been read in this session"):
        await tool.execute(ctx, path="notes.txt", content="new\n")


@pytest.mark.asyncio
async def test_write_existing_file_fails_if_changed_after_read(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("old\n", encoding="utf-8")

    read_tool = ReadFileTool()
    write_tool = WriteFileTool()
    ctx = ToolContext(workspace=tmp_path)

    assert await read_tool.execute(ctx, path="notes.txt") == "old\n"
    file_path.write_text("externally changed\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed after it was read"):
        await write_tool.execute(ctx, path="notes.txt", content="new\n")


@pytest.mark.asyncio
async def test_edit_existing_file_succeeds_after_read_and_refreshes_snapshot(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello world\n", encoding="utf-8")

    read_tool = ReadFileTool()
    edit_tool = EditFileTool()
    ctx = ToolContext(workspace=tmp_path)

    assert await read_tool.execute(ctx, path="notes.txt") == "hello world\n"
    result = await edit_tool.execute(
        ctx,
        path="notes.txt",
        old_text="hello",
        new_text="goodbye",
    )

    assert result == "Successfully edited notes.txt"
    assert file_path.read_text(encoding="utf-8") == "goodbye world\n"

    second_result = await edit_tool.execute(
        ctx,
        path="notes.txt",
        old_text="goodbye",
        new_text="hello",
    )

    assert second_result == "Successfully edited notes.txt"
