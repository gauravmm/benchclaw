"""Base class for agent tools."""

import asyncio
from abc import abstractmethod
from asyncio import Task
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import BaseModel

_TOOL_REGISTRY: dict[str, type["Tool"]] = {}
_TOOL_CONFIG_REGISTRY: dict[str, type[BaseModel]] = {}


def register_tool(name: str, cls: type["Tool"]) -> None:
    """Register a tool class under the given name."""
    _TOOL_REGISTRY[name] = cls


def register_tool_config(name: str, cls: type[BaseModel]) -> None:
    """Register a tool config class under the given name."""
    _TOOL_CONFIG_REGISTRY[name] = cls


@dataclass
class ToolBuildContext:
    """Runtime context passed to Tool.build() during agent initialisation."""

    workspace: Path
    restrict_to_workspace: bool = False
    bus: Any = None             # MessageBus; None for subagents
    process_direct: Any = None  # AgentLoop.process_direct; None for subagents
    subagent_manager: Any = None  # SubagentManager; None for subagents


class Tool(AbstractAsyncContextManager):
    """
    Abstract base class for agent tools.

    Tools are capabilities that the agent can use to interact with
    the environment, such as reading files, executing commands, etc.
    """

    _task: Task | None = None
    agent_only: ClassVar[bool] = False  # True → tool excluded from subagent registries

    async def background(self) -> None:
        """Optional long-running coroutine started on __aenter__. No-op by default."""
        pass

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self.background(), name=self.name)
        return self

    async def __aexit__(self, *_) -> None:
        if self._task:
            try:
                async with asyncio.timeout(5):
                    self._task.cancel()
            except TimeoutError:
                pass
            self._task = None

    @classmethod
    def build(cls, config: Any, ctx: ToolBuildContext) -> "Self":
        """Instantiate this tool from a config object and build context."""
        raise NotImplementedError(f"{cls.__name__}.build() is not implemented")

    def set_context(self, channel: str, chat_id: str) -> None:
        """Update per-message routing context. Override in tools that need it."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        pass

    @property
    def skill(self) -> str | None:
        """Skill name to inject into agent context when this tool is selected. None if no skill applies."""
        return None

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters."""
        pass

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters.

        Returns:
            String result of the tool execution.
        """
        pass

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return _validate_schema(params, {**schema, "type": "object"}, "")

    def to_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _validate_schema(val: Any, schema: dict[str, Any], label: str = "") -> list[str]:
    t = schema.get("type")
    if t in _TYPE_MAP and not isinstance(val, _TYPE_MAP[t]):
        return [f"{label} should be {t}"]

    errors = []
    if "enum" in schema and val not in schema["enum"]:
        errors.append(f"{label} must be one of {schema['enum']}")
    elif t in ("integer", "number"):
        if "minimum" in schema and val < schema["minimum"]:
            errors.append(f"{label} must be >= {schema['minimum']}")
        if "maximum" in schema and val > schema["maximum"]:
            errors.append(f"{label} must be <= {schema['maximum']}")
    elif t == "string":
        if "minLength" in schema and len(val) < schema["minLength"]:
            errors.append(f"{label} must be at least {schema['minLength']} chars")
        if "maxLength" in schema and len(val) > schema["maxLength"]:
            errors.append(f"{label} must be at most {schema['maxLength']} chars")
    elif t == "object":
        for k in schema.get("required", []):
            if k not in val:
                errors.append(f"missing required {label + '.' + k}")
        props = schema.get("properties", {})
        for k, v in val.items():
            if k in props:
                errors.extend(_validate_schema(v, props[k], label + "." + k))
    if t == "array" and "items" in schema:
        for i, item in enumerate(val):
            errors.extend(_validate_schema(item, schema["items"], f"{label}[{i}]"))
    return errors
