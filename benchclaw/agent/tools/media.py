"""Media tools for re-reading, searching, and sending stored images."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchclaw.agent.tools.base import Tool, ToolContext
from benchclaw.bus import MessageAddress, OutboundMessage, ToolResult
from benchclaw.channels.whatsapp.address import WhatsAppId
from benchclaw.media import MediaRepository


def _resolve_target_address(ctx: ToolContext, address: str | None) -> MessageAddress | None:
    """Resolve an explicit or implicit target address."""
    target = MessageAddress.from_string(address) if address else ctx.address
    if target is None:
        return None
    if target.channel == "whatsapp":
        return WhatsAppId.from_address(target).as_address()
    return target


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
            "Load a workspace image by its relative path so you can inspect it again. "
            "Use the exact stored path, for example 'media/a3f7b2c1/0310/1423-01.jpg' or 'images/receipt.png'. "
            "Only call this when you need to examine an image to answer a follow-up question."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path to the image file.",
                }
            },
            "required": ["path"],
        }

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> ToolResult:
        if Path(path).is_absolute():
            raise ValueError(f"Path is outside the workspace: {path}")
        media_repo = ctx.media_repo or MediaRepository(ctx.workspace)
        return [media_repo.image_block(path)]


class SendImageTool(Tool):
    """Send a stored workspace image to the current or another chat."""

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
            "Use this instead of the message tool for image delivery. "
            "When sending an image, put the user-visible text in the image caption/body rather than also saying in plain text that you sent it. "
            "Strongly prefer omitting `address` when sending to the current chat."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative image path.",
                },
                "caption": {
                    "type": "string",
                    "description": (
                        "Optional caption/body to send with the image. "
                        "Put all required user-visible text here when applicable, instead of sending a separate plain-text acknowledgement."
                    ),
                },
                "address": {
                    "type": "string",
                    "description": (
                        "Optional target address as channel:chat_id. Defaults to the current chat; "
                        "omit this when sending to the current chat."
                    ),
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

        if Path(path).is_absolute():
            raise ValueError(f"Path is outside the workspace: {path}")
        media_repo = ctx.media_repo or MediaRepository(ctx.workspace)
        _, mime = media_repo.resolve_file(path)
        if not mime or not mime.startswith("image/"):
            raise ValueError(f"Path is not an image: {path}")

        await ctx.bus.publish_outbound(
            OutboundMessage(address=target, content=caption, media=[path])
        )
        return f"Image sent to {target}"


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
            "Search captioned workspace images using stored metadata and model-authored captions. "
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
                    "description": "Optional address filter as channel:chat_id.",
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
        resolved_address = MessageAddress.from_string(address) if address else None
        if resolved_address and resolved_address.channel == "whatsapp":
            resolved_address = WhatsAppId.from_address(resolved_address).as_address()

        results = ctx.media_repo.search(
            query=query or None,
            address=resolved_address,
            sender_id=sender_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
        return json.dumps(results, ensure_ascii=False)


class AnnotateMediaTool(Tool):
    """Persist a caption or annotation for a stored media file."""

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "AnnotateMediaTool":
        return cls()

    @property
    def name(self) -> str:
        return "annotate_media"

    @property
    def description(self) -> str:
        return (
            "Save a concise caption or annotation for a stored workspace media file. "
            "Use this after receiving an image so future turns can search or answer follow-up questions without re-reading it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative media path to annotate.",
                },
                "caption": {
                    "type": "string",
                    "description": "Searchable caption or annotation text.",
                },
            },
            "required": ["path", "caption"],
        }

    async def execute(self, ctx: ToolContext, path: str, caption: str, **_: Any) -> str:
        if not ctx.media_repo:
            raise RuntimeError("annotate_media requires media repository access")
        ctx.media_repo.set_caption(path, caption)
        return f"Saved annotation for {path}"
