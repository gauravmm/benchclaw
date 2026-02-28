"""Configuration schema and loading utilities for nanobot."""

from collections.abc import Iterator
from pathlib import Path

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_settings import BaseSettings

import nanobot.agent.tools  # noqa: F401  # triggers register_tool_config() calls in all tool modules
import nanobot.channels  # noqa: F401  # triggers register_channel() calls in all channel modules
from nanobot.agent.tools.base import _TOOL_CONFIG_REGISTRY
from nanobot.channels.base import _CONFIG_REGISTRY, ChannelConfig


class AgentConfig(BaseModel):
    """Default agent configuration."""

    workspace: str = "./workspace"
    model: str = "anthropic/claude-opus-4-5"
    max_tokens: int = 8192
    temperature: float = 0.7
    max_tool_iterations: int = 20
    memory_window: int = 50


class AgentsConfig(BaseModel):
    """Agent configuration."""

    master: AgentConfig = Field(default_factory=AgentConfig)


class ProviderConfig(BaseModel):
    """LLM provider configuration."""

    name: str = "anthropic"  # Registry name: "anthropic", "openrouter", "deepseek", etc.
    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790


class _ToolConfigsBase(BaseModel):
    """Base class for dynamically-built ToolsConfig."""

    pass


ToolsConfig: type[_ToolConfigsBase] = create_model(
    "ToolsConfig",
    __base__=_ToolConfigsBase,
    **{name: (cls, Field(default_factory=cls)) for name, cls in _TOOL_CONFIG_REGISTRY.items()},  # type: ignore[arg-type]
)


class _ChannelConfigsBase(BaseModel):
    def __iter__(self) -> Iterator[tuple[str, ChannelConfig]]:
        for name in type(self).model_fields:
            yield name, getattr(self, name)


ChannelConfigs: type[_ChannelConfigsBase] = create_model(
    "ChannelConfigs",
    __base__=_ChannelConfigsBase,
    **{name: (cls, Field(default_factory=cls)) for name, cls in _CONFIG_REGISTRY.items()},  # type: ignore[arg-type]
)


class Config(BaseSettings):
    """Root configuration for nanobot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    channels: ChannelConfigs = Field(default_factory=ChannelConfigs)  # pyright: ignore[reportInvalidTypeForm]
    tools: ToolsConfig = Field(default_factory=ToolsConfig)  # pyright: ignore[reportInvalidTypeForm]

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.master.workspace).expanduser()

    model_config = ConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")  # type: ignore


class ConfigManager:
    """Context manager that loads config on enter and saves it on exit."""

    def __init__(self, config_path: Path = Path("config") / "config.yaml"):
        self._path = config_path
        self.config: Config | None = None
        self._write_on_exit = False

    def __enter__(self) -> Config:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = yaml.safe_load(f)
                data = _migrate_config(data)
                self.config = Config.model_validate(data)
                return self.config
            except (yaml.YAMLError, ValueError) as e:
                logger.warning(f"Failed to load config from {self._path}: {e}")
                logger.warning("Using default configuration.")
        else:
            # Don't clobber an existing config file.
            self._write_on_exit = True

        self.config = Config()
        return self.config

    def __exit__(self, *_) -> None:
        if self._write_on_exit and self.config:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                yaml.dump(self.config.model_dump(), f, default_flow_style=False, allow_unicode=True)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    return data
