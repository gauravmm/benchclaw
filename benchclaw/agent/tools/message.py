"""Message tool for sending messages to users."""

from typing import Any, Awaitable, Callable

from benchclaw.agent.tools.base import Tool, ToolContext, register_tool
from benchclaw.bus import MessageAddress, OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    master_only = True

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "MessageTool":
        return cls(send_callback=ctx.bus.publish_outbound if ctx.bus else None)

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self._send_callback = send_callback

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Deliver a text-only proactive message to a different chat or channel. "
            "Your normal assistant reply is already delivered to the current chat automatically, so do not use this for the current turn's reply. "
            "Always provide an explicit channel and chat_id. Use send_image for images. "
            "Example: `{'content': 'Your report is ready!', 'channel': 'telegram', 'chat_id': '123456'}`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The message content to send"},
                "channel": {
                    "type": "string",
                    "description": "Target channel (telegram, whatsapp, smtp_email, etc.)",
                },
                "chat_id": {"type": "string", "description": "Target chat/user ID"},
            },
            "required": ["content", "channel", "chat_id"],
        }

    async def execute(
        self,
        ctx: ToolContext,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        target_channel = (channel or "").strip()
        target_chat_id = (chat_id or "").strip()
        if not target_channel or not target_chat_id:
            raise ValueError("message requires explicit channel and chat_id")
        if ctx.address and (
            target_channel == ctx.address.channel and target_chat_id == ctx.address.chat_id
        ):
            raise ValueError(
                "message cannot target the current conversation; reply with normal assistant text instead"
            )

        if not self._send_callback:
            raise RuntimeError("Message sending not configured")

        msg = OutboundMessage(
            address=MessageAddress(channel=target_channel, chat_id=target_chat_id),
            content=content,
        )

        try:
            await self._send_callback(msg)
            return f"Message sent to {target_channel}:{target_chat_id}"
        except Exception as e:
            raise RuntimeError(f"Error sending message: {e}") from e


register_tool("message", MessageTool)
