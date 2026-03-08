"""File system tools: read, write, edit, and search."""

import re
from os import stat_result
from pathlib import Path
from typing import Any

from benchclaw.agent.tools.base import FileSnapshot, Tool, ToolContext, register_tool


def _resolve_path(path: str, ctx: ToolContext, allowed_dir: Path | None = None) -> Path:
    """Resolve path and optionally enforce a directory restriction."""
    if path.startswith("/"):
        resolved = Path(path)
    else:
        resolved = ctx.workspace / path
    resolved = resolved.expanduser().resolve()

    if allowed_dir is not None:
        root = allowed_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise PermissionError(
                f"Path '{path}' is outside the allowed directory '{root}'"
            )
    return resolved


def _display_path(path: Path, ctx: ToolContext) -> str:
    """Return a workspace-relative display path when possible."""
    try:
        return str(path.relative_to(ctx.workspace))
    except ValueError:
        return str(path)


def _make_snapshot(path: Path, file_stat: stat_result) -> FileSnapshot:
    """Build a cached snapshot from stat metadata."""
    return FileSnapshot(path=path, size=file_stat.st_size, mtime_ns=file_stat.st_mtime_ns)


def _record_snapshot(ctx: ToolContext, path: Path) -> None:
    """Record the current metadata for a file in the session cache."""
    ctx.file_snapshots[path] = _make_snapshot(path, path.stat())


def _require_fresh_snapshot(ctx: ToolContext, path: Path) -> str | None:
    """Require that an existing file was read and has not changed since then."""
    display_path = _display_path(path, ctx)
    snapshot = ctx.file_snapshots.get(path)
    if snapshot is None:
        return (
            f"Error: Refusing to modify existing file '{display_path}' because it has not been "
            "read in this session. Read it first with read_file."
        )

    try:
        current_stat = path.stat()
    except FileNotFoundError:
        return (
            f"Error: Refusing to modify '{display_path}' because it existed when read but no "
            "longer exists. Re-read the file and retry."
        )

    if current_stat.st_size != snapshot.size or current_stat.st_mtime_ns != snapshot.mtime_ns:
        return (
            f"Error: Refusing to modify '{display_path}' because it changed after it was read. "
            f"Expected size={snapshot.size} mtime_ns={snapshot.mtime_ns}, current "
            f"size={current_stat.st_size} mtime_ns={current_stat.st_mtime_ns}. Re-read the "
            "file and retry."
        )

    return None


class ReadFileTool(Tool):
    """Tool to read file contents."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "ReadFileTool":
        return cls(allowed_dir=ctx.workspace)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the complete contents of a file from the file system, using a path relative to the workspace by default. "
            "Use this tool when you need to examine the contents of a single file. "
            "Returns a detailed error if the file cannot be read or is not a regular file. "
            "Example: `{'path': 'README.md'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read, relative to the workspace by default",
                }
            },
            "required": ["path"],
        }

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, ctx, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            content = file_path.read_text(encoding="utf-8")
            _record_snapshot(ctx, file_path)
            return content
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "WriteFileTool":
        return cls(allowed_dir=ctx.workspace)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str | None:
        return (
            "Create a new file or completely overwrite an existing file with new content, using a path relative to the workspace by default. "
            "Use with caution because it replaces the full file contents. "
            "Creates parent directories as needed and returns a detailed error if the write fails. "
            "Example: `{'path': 'notes/output.txt', 'content': 'Hello, world!'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to write, relative to the workspace by default",
                },
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, ctx: ToolContext, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, ctx, self._allowed_dir)
            if file_path.exists():
                if not file_path.is_file():
                    return f"Error: Not a file: {path}"
                snapshot_error = _require_fresh_snapshot(ctx, file_path)
                if snapshot_error is not None:
                    return snapshot_error

            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            _record_snapshot(ctx, file_path)
            return f"Successfully wrote {len(content)} bytes to {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "EditFileTool":
        return cls(allowed_dir=ctx.workspace)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Make a targeted edit to an existing text file by replacing an exact string match, using a path relative to the workspace by default. "
            "This is safer than overwriting the whole file when you only need to change part of it. "
            "The edit is rejected if the original text is missing or appears more than once. "
            "Example: `{'path': 'USER.md', 'old_text': 'port: 8080', 'new_text': 'port: 9090'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to edit, relative to the workspace by default",
                },
                "old_text": {"type": "string", "description": "The exact text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self, ctx: ToolContext, path: str, old_text: str, new_text: str, **kwargs: Any
    ) -> str:
        try:
            file_path = _resolve_path(path, ctx, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            snapshot_error = _require_fresh_snapshot(ctx, file_path)
            if snapshot_error is not None:
                return snapshot_error

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return "Error: old_text not found in file. Make sure it matches exactly."

            # Count occurrences
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")
            _record_snapshot(ctx, file_path)

            return f"Successfully edited {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"


class ListDirTool(Tool):
    """Tool to list directory contents."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "ListDirTool":
        return cls(allowed_dir=ctx.workspace)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "Get a detailed listing of the files and directories in a specified path, using a path relative to the workspace by default. "
            "Results clearly distinguish between files and directories and are sorted alphabetically. "
            "This tool is useful for understanding directory structure and locating files before reading or editing them. "
            "Example: `{'path': 'memory/'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list, relative to the workspace by default",
                }
            },
            "required": ["path"],
        }

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> str:
        try:
            dir_path = _resolve_path(path, ctx, self._allowed_dir)
            if not dir_path.exists():
                return f"Error: Directory not found: {path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {path}"

            items = []
            for item in sorted(dir_path.iterdir()):
                prefix = "📁 " if item.is_dir() else "📄 "
                items.append(f"{prefix}{item.name}")

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"


class GlobTool(Tool):
    """Tool to match filesystem paths with a glob pattern."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "GlobTool":
        return cls(allowed_dir=ctx.workspace)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find files and directories that match a glob pattern, using the workspace as the search root by default. "
            "Use this tool when you know the shape of the path but not the exact filename. "
            "Returns matching paths relative to the workspace when possible. "
            "Example: `{'pattern': 'benchclaw/**/*.py', 'path': '.'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match"},
                "path": {
                    "type": "string",
                    "description": "The directory to search from, relative to the workspace by default",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matches to return",
                    "minimum": 1,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        ctx: ToolContext,
        pattern: str,
        path: str = ".",
        max_results: int = 200,
        **kwargs: Any,
    ) -> str:
        try:
            root = _resolve_path(path, ctx, self._allowed_dir)
            if not root.exists():
                return f"Error: Directory not found: {path}"
            if not root.is_dir():
                return f"Error: Not a directory: {path}"

            matches = sorted(root.glob(pattern))
            if not matches:
                return f"No paths matched pattern: {pattern}"

            limited_matches = matches[:max_results]
            lines = [_display_path(match, ctx) for match in limited_matches]
            if len(matches) > max_results:
                lines.append(f"... and {len(matches) - max_results} more")
            return "\n".join(lines)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error matching glob: {str(e)}"


class GrepTool(Tool):
    """Tool to search file contents for matching lines."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "GrepTool":
        return cls(allowed_dir=ctx.workspace)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search for matching text in a file or directory tree, using a workspace-relative path by default. "
            "Supports plain-text matching or regular expressions and returns matching lines with file and line numbers. "
            "Use `file_pattern` to limit which files are searched when scanning directories. "
            "Example: `{'pattern': 'register_tool', 'path': 'benchclaw', 'file_pattern': '*.py'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The text or regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "The file or directory to search, relative to the workspace by default",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob filter for files when searching a directory",
                },
                "is_regex": {
                    "type": "boolean",
                    "description": "Whether pattern should be treated as a regular expression",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether matching should be case-sensitive",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matching lines to return",
                    "minimum": 1,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        ctx: ToolContext,
        pattern: str,
        path: str = ".",
        file_pattern: str = "*",
        is_regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 200,
        **kwargs: Any,
    ) -> str:
        try:
            target = _resolve_path(path, ctx, self._allowed_dir)
            if not target.exists():
                return f"Error: Path not found: {path}"

            flags = 0 if case_sensitive else re.IGNORECASE
            matcher = re.compile(pattern if is_regex else re.escape(pattern), flags)

            if target.is_file():
                files = [target]
            else:
                files = [
                    candidate
                    for candidate in sorted(target.rglob(file_pattern))
                    if candidate.is_file()
                ]

            results: list[str] = []
            for file_path in files:
                try:
                    lines = file_path.read_text(encoding="utf-8").splitlines()
                except UnicodeDecodeError:
                    continue

                for line_number, line in enumerate(lines, start=1):
                    if matcher.search(line):
                        results.append(f"{_display_path(file_path, ctx)}:{line_number}: {line}")
                        if len(results) >= max_results:
                            return "\n".join(results)

            if not results:
                return f"No matches found for pattern: {pattern}"

            return "\n".join(results)
        except re.error as e:
            return f"Error: Invalid regular expression: {e}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error searching files: {str(e)}"


register_tool("read_file", ReadFileTool)
register_tool("write_file", WriteFileTool)
register_tool("edit_file", EditFileTool)
register_tool("list_dir", ListDirTool)
register_tool("glob", GlobTool)
register_tool("grep", GrepTool)
