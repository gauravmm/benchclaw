"""Media tools for re-reading, searching, and sending stored images."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import filetype

from benchclaw.agent.tools.base import Tool, ToolContext, register_tool
from benchclaw.bus import MessageAddress, OutboundMessage, ToolResult
from benchclaw.channels.whatsapp.address import (
    normalize_whatsapp_address,
    parse_normalized_whatsapp_address,
)


def _resolve_target_address(ctx: ToolContext, address: str | None) -> MessageAddress | None:
    """Resolve an explicit or implicit target address."""
    target = parse_normalized_whatsapp_address(address) if address else ctx.address
    if target is None:
        return None
    return normalize_whatsapp_address(target)


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


class SendImageTool(Tool):
    """Send a stored workspace image to the current or another chat."""

    master_only = True

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "SendImageTool":
        return cls()

    @property
    def name(self) -> str:
        return "send_image"

    @property
    def description(self) -> str:
        return (
            "Send one stored workspace image to the current chat by default, or to another chat "
            "when you provide an explicit address like 'telegram:12345'. "
            "Use this instead of the message tool for image delivery."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative image path, usually under media/.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption/body to send with the image.",
                },
                "address": {
                    "type": "string",
                    "description": "Optional target address as channel:chat_id. Defaults to the current chat.",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, ctx: ToolContext, path: str, caption: str = "", address: str | None = None, **_: Any
    ) -> str:
        if not ctx.bus:
            raise RuntimeError("send_image requires message bus access")
        target = _resolve_target_address(ctx, address)
        if target is None:
            raise ValueError("No target address available")

        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = ctx.workspace / file_path
        file_path = file_path.resolve()
        try:
            file_path.relative_to(ctx.workspace.resolve())
        except ValueError as exc:
            raise ValueError(f"Path is outside the workspace: {path}") from exc
        if not file_path.is_file():
            raise FileNotFoundError(f"Image not found: {path}")
        mime = filetype.guess_mime(str(file_path))
        if not mime or not mime.startswith("image/"):
            raise ValueError(f"Path is not an image: {path}")

        await ctx.bus.publish_outbound(
            OutboundMessage(address=target, content=caption, media=[path])
        )
        return f"Image sent to {target}"


register_tool("send_image", SendImageTool)


class SearchImagesTool(Tool):
    """Search stored images using saved metadata and captions."""

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "SearchImagesTool":
        return cls()

    @property
    def name(self) -> str:
        return "search_images"

    @property
    def description(self) -> str:
        return (
            "Search saved images using stored metadata and model-authored captions. "
            "Use this when you remember an image but not its exact media path."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional free-text search over captions and metadata.",
                },
                "address": {
                    "type": "string",
                    "description": "Optional address filter as channel:chat_id. Defaults to the current chat.",
                },
                "sender_id": {
                    "type": "string",
                    "description": "Optional sender_id filter.",
                },
                "date_from": {
                    "type": "string",
                    "description": "Optional inclusive lower timestamp/date bound in ISO format.",
                },
                "date_to": {
                    "type": "string",
                    "description": "Optional inclusive upper timestamp/date bound in ISO format.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum number of results to return. Default 10.",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        ctx: ToolContext,
        query: str = "",
        address: str | None = None,
        sender_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
        **_: Any,
    ) -> str:
        if not ctx.media_repo:
            raise RuntimeError("search_images requires media repository access")
        resolved_address = _resolve_target_address(ctx, address)

        results = ctx.media_repo.search(
            query=query or None,
            address=resolved_address,
            sender_id=sender_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
        return json.dumps(results, ensure_ascii=False)


register_tool("search_images", SearchImagesTool)
