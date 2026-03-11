"""Media tools: read_image lets the agent re-examine a previously received image."""

from __future__ import annotations

import base64
from typing import Any

import filetype

from benchclaw.agent.tools.base import Tool, ToolContext, register_tool
from benchclaw.bus import ToolResult


class ReadImageTool(Tool):
    """Tool to re-load a media image into the LLM context."""

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "ReadImageTool":
        return cls()

    @property
    def name(self) -> str:
        return "read_image"

    @property
    def description(self) -> str:
        return (
            "Load a previously received image by its media path so you can inspect it again. "
            "Use the exact path from the image stub or a prior caption "
            "(e.g. 'media/a3f7b2c1/0310/1423-01.jpg'). "
            "Only call this when you need to re-examine an image to answer a follow-up question."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path to the media file (e.g. 'media/a3f7b2c1/0310/1423-01.jpg').",
                }
            },
            "required": ["path"],
        }

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> ToolResult:
        if not path.startswith("media/"):
            raise ValueError("read_image is restricted to the media/ directory")
        file_path = ctx.workspace / path
        if not file_path.is_file():
            raise FileNotFoundError(f"Media file not found: {path}")
        mime = filetype.guess_mime(str(file_path)) or "image/jpeg"
        data = base64.b64encode(file_path.read_bytes()).decode()
        return [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]


register_tool("read_image", ReadImageTool)
