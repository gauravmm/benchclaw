"""Configuration loading utilities."""

from pathlib import Path

import yaml

from nanobot.config.schema import Config


# TODO: Clean up the paths.
def get_config_path() -> Path:
    """Get the default configuration file path."""
    return Path("config") / "config.yaml"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            data = _migrate_config(data)
            return Config.model_validate(data)

        except (yaml.YAMLError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False, allow_unicode=True)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    return data
