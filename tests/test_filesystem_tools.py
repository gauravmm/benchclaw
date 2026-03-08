"""Tests for filesystem search tools."""

from pathlib import Path

import pytest

from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.filesystem import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)


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

    result = await tool.execute(ctx, path="notes.txt", content="new\n")

    assert result == (
        "Error: Refusing to modify existing file 'notes.txt' because it has not been read in "
        "this session. Read it first with read_file."
    )


@pytest.mark.asyncio
async def test_write_existing_file_fails_if_changed_after_read(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("old\n", encoding="utf-8")

    read_tool = ReadFileTool()
    write_tool = WriteFileTool()
    ctx = ToolContext(workspace=tmp_path)

    assert await read_tool.execute(ctx, path="notes.txt") == "old\n"
    file_path.write_text("externally changed\n", encoding="utf-8")

    result = await write_tool.execute(ctx, path="notes.txt", content="new\n")

    assert result.startswith(
        "Error: Refusing to modify 'notes.txt' because it changed after it was read."
    )


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


# ── Workspace restriction tests (via build()) ────────────────────────────────


@pytest.mark.asyncio
async def test_read_file_blocks_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    ctx = ToolContext(workspace=tmp_path)
    tool = ReadFileTool.build(None, ctx)

    result = await tool.execute(ctx, path=str(outside))

    assert result.startswith("Error:")
    assert "outside" in result.lower() or "allowed" in result.lower()


@pytest.mark.asyncio
async def test_write_file_blocks_path_outside_workspace(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=tmp_path)
    tool = WriteFileTool.build(None, ctx)

    result = await tool.execute(ctx, path=str(tmp_path.parent / "evil.txt"), content="x")

    assert result.startswith("Error:")
    assert "outside" in result.lower() or "allowed" in result.lower()


@pytest.mark.asyncio
async def test_edit_file_blocks_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "external.txt"
    outside.write_text("old content", encoding="utf-8")

    ctx = ToolContext(workspace=tmp_path)
    read_tool = ReadFileTool.build(None, ctx)
    edit_tool = EditFileTool.build(None, ctx)

    # read tool should also block it
    read_result = await read_tool.execute(ctx, path=str(outside))
    assert read_result.startswith("Error:")

    result = await edit_tool.execute(
        ctx, path=str(outside), old_text="old content", new_text="new content"
    )
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_list_dir_blocks_path_outside_workspace(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=tmp_path)
    tool = ListDirTool.build(None, ctx)

    result = await tool.execute(ctx, path=str(tmp_path.parent))

    assert result.startswith("Error:")
    assert "outside" in result.lower() or "allowed" in result.lower()


@pytest.mark.asyncio
async def test_glob_blocks_search_root_outside_workspace(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=tmp_path)
    tool = GlobTool.build(None, ctx)

    result = await tool.execute(ctx, pattern="*.txt", path=str(tmp_path.parent))

    assert result.startswith("Error:")
    assert "outside" in result.lower() or "allowed" in result.lower()


@pytest.mark.asyncio
async def test_grep_blocks_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "other.txt"
    outside.write_text("some text\n", encoding="utf-8")

    ctx = ToolContext(workspace=tmp_path)
    tool = GrepTool.build(None, ctx)

    result = await tool.execute(ctx, pattern="some", path=str(outside))

    assert result.startswith("Error:")
    assert "outside" in result.lower() or "allowed" in result.lower()


@pytest.mark.asyncio
async def test_read_file_allows_path_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("hello\n", encoding="utf-8")

    ctx = ToolContext(workspace=tmp_path)
    tool = ReadFileTool.build(None, ctx)

    result = await tool.execute(ctx, path="data.txt")

    assert result == "hello\n"

