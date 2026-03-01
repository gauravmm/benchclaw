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
            "Deliver a text message to a user on a chat channel (Telegram, Discord, etc.) that is not the current conversation channel. "
            "Specify `channel` and `chat_id` explicitly to redirect a message to a different conversation. "
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
                    "description": "Optional: target channel (telegram, discord, etc.)",
                },
                "chat_id": {"type": "string", "description": "Optional: target chat/user ID"},
            },
            "required": ["content"],
        }

    async def execute(
        self,
        ctx: ToolContext,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        # Use explicit channel/chat_id if provided, otherwise fall back to session address
        target_channel = channel or (ctx.address.channel if ctx.address else "")
        target_chat_id = chat_id or (ctx.address.chat_id if ctx.address else "")

        if not target_channel or not target_chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            address=MessageAddress(channel=target_channel, chat_id=target_chat_id),
            content=content,
        )

        try:
            await self._send_callback(msg)
            return f"Message sent to {target_channel}:{target_chat_id}"
        except Exception as e:
            return f"Error sending message: {str(e)}"


register_tool("message", MessageTool)
