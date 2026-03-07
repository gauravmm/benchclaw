"""File system tools: read, write, edit."""

from pathlib import Path
from typing import Any

from benchclaw.agent.tools.base import Tool, ToolContext, register_tool


def _resolve_path(path: str, ctx: ToolContext) -> Path:
    """Resolve path and optionally enforce directory restriction."""
    if path.startswith("/"):
        resolved = Path(path)
    else:
        resolved = ctx.workspace / path
    resolved = resolved.expanduser().resolve()

    # TODO: Support allowed_dir
    # if ctx.allowed_dir and not str(resolved).startswith(str(ctx.allowed_dir.resolve())):
    #     raise PermissionError(f"Path {path} is outside allowed directory {ctx.allowed_dir}")
    return resolved


class ReadFileTool(Tool):
    """Tool to read file contents."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "ReadFileTool":
        return cls(allowed_dir=ctx.workspace if ctx.is_subagent else None)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the complete contents of a file from the file system. "
            "Use this tool when you need to examine the contents of a single file. "
            "Returns a detailed error if the file cannot be read or is not a regular file. "
            "Example: `{'path': 'README.md'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The file path to read"}},
            "required": ["path"],
        }

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, ctx)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            content = file_path.read_text(encoding="utf-8")
            return content
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "WriteFileTool":
        return cls(allowed_dir=ctx.workspace if ctx.is_subagent else None)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str | None:
        return (
            "Create a new file or completely overwrite an existing file with new content. "
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
                    "description": "The file path to write to relative to the workspace dir",
                },
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, ctx: ToolContext, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, ctx)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "EditFileTool":
        return cls(allowed_dir=ctx.workspace if ctx.is_subagent else None)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Make a targeted edit to an existing text file by replacing an exact string match. "
            "This is safer than overwriting the whole file when you only need to change part of it. "
            "The edit is rejected if the original text is missing or appears more than once. "
            "Example: `{'path': 'config/config.yaml', 'old_text': 'port: 8080', 'new_text': 'port: 9090'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to edit"},
                "old_text": {"type": "string", "description": "The exact text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self, ctx: ToolContext, path: str, old_text: str, new_text: str, **kwargs: Any
    ) -> str:
        try:
            file_path = _resolve_path(path, ctx)
            if not file_path.exists():
                return f"Error: File not found: {path}"

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return "Error: old_text not found in file. Make sure it matches exactly."

            # Count occurrences
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")

            return f"Successfully edited {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"


class ListDirTool(Tool):
    """Tool to list directory contents."""

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "ListDirTool":
        return cls(allowed_dir=ctx.workspace if ctx.is_subagent else None)

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "Get a detailed listing of the files and directories in a specified path. "
            "Results clearly distinguish between files and directories and are sorted alphabetically. "
            "This tool is useful for understanding directory structure and locating files before reading or editing them. "
            "Example: `{'path': 'benchclaw/agent/tools'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The directory path to list"}},
            "required": ["path"],
        }

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> str:
        try:
            dir_path = _resolve_path(path, ctx)
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


register_tool("read_file", ReadFileTool)
register_tool("write_file", WriteFileTool)
register_tool("edit_file", EditFileTool)
register_tool("list_dir", ListDirTool)
