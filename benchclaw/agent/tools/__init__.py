"""Agent tools — import this package to register all built-in tools."""

# Import tool modules to trigger their register_tool() and register_tool_config() calls.
# Add a new import here when adding a new tool.
import benchclaw.agent.tools.cron  # noqa: F401
import benchclaw.agent.tools.filesystem  # noqa: F401
import benchclaw.agent.tools.media  # noqa: F401
import benchclaw.agent.tools.memory  # noqa: F401
import benchclaw.agent.tools.message  # noqa: F401
import benchclaw.agent.tools.shell  # noqa: F401
import benchclaw.agent.tools.web  # noqa: F401
from benchclaw.agent.tools.base import (
    _TOOL_CONFIG_REGISTRY,
    _TOOL_REGISTRY,
    Tool,
    register_tool,
    register_tool_config,
)
from benchclaw.agent.tools.registry import ToolRegistry

__all__ = [
    "Tool",
    "ToolRegistry",
    "_TOOL_REGISTRY",
    "_TOOL_CONFIG_REGISTRY",
    "register_tool",
    "register_tool_config",
]
