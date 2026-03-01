"""Chat channels — import this package to register all built-in channels."""

# Import channel modules to trigger their register_channel() calls.
# Add a new import here when adding a new channel.
import benchclaw.channels.smtp_email  # noqa: F401
import benchclaw.channels.telegrm  # noqa: F401
import benchclaw.channels.whatsapp  # noqa: F401
from benchclaw.channels.base import _CONFIG_REGISTRY, BaseChannel, ChannelConfig, register_channel

__all__ = ["BaseChannel", "ChannelConfig", "_CONFIG_REGISTRY", "register_channel"]
