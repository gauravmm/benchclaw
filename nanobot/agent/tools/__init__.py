"""Agent tools — import this package to register all built-in tools."""

from nanobot.agent.tools.base import Tool, _TOOL_REGISTRY, register_tool
from nanobot.agent.tools.registry import ToolRegistry

# Import tool modules to trigger their register_tool() calls.
# Add a new import here when adding a new tool.
import nanobot.agent.tools.cron  # noqa: F401
import nanobot.agent.tools.filesystem  # noqa: F401
import nanobot.agent.tools.memory  # noqa: F401
import nanobot.agent.tools.message  # noqa: F401
import nanobot.agent.tools.shell  # noqa: F401
import nanobot.agent.tools.spawn  # noqa: F401
import nanobot.agent.tools.web  # noqa: F401

__all__ = ["Tool", "ToolRegistry", "_TOOL_REGISTRY", "register_tool"]
