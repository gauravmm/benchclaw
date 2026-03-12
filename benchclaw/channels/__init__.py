"""Chat channels — import this package to register all built-in channels."""

from benchclaw.channels.base import _CONFIG_REGISTRY, BaseChannel, ChannelConfig, register_channel

__all__ = ["BaseChannel", "ChannelConfig", "_CONFIG_REGISTRY", "register_channel"]
