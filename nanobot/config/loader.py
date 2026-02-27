"""Configuration loading utilities."""

from pathlib import Path

import yaml

from nanobot.config.schema import Config


# TODO: Clean up the paths.
def get_config_path() -> Path:
    """Get the default configuration file path."""
    return Path("config") / "config.yaml"


class ConfigManager:
    """Context manager that loads config on enter and saves it on exit."""

    def __init__(self, config_path: Path | None = None):
        self._path = config_path or get_config_path()
        self.config: Config | None = None

    def __enter__(self) -> Config:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = yaml.safe_load(f)
                data = _migrate_config(data)
                self.config = Config.model_validate(data)
                return self.config
            except (yaml.YAMLError, ValueError) as e:
                print(f"Warning: Failed to load config from {self._path}: {e}")
                print("Using default configuration.")

        self.config = Config()
        return self.config

    def __exit__(self, *_) -> None:
        if self.config is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                yaml.dump(self.config.model_dump(), f, default_flow_style=False, allow_unicode=True)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    return data
