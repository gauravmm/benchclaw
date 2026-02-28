"""Entry point for nanobot: python -m nanobot [options]"""

import argparse
import asyncio
import logging
from pathlib import Path

from loguru import logger

from nanobot import __logo__
from nanobot.agent.loop import AgentLoop
from nanobot.bus import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config import ConfigManager
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.session.manager import SessionManager


def _make_provider(config):
    p = config.provider
    if not p.api_key:
        logger.error("No API key configured.")
        logger.error("Set one in config/config.yaml under provider section.")
        raise RuntimeError("No API key configured")

    return LiteLLMProvider(
        provider_name=p.name,
        api_key=p.api_key,
        api_base=p.api_base,
        default_model=config.agents.defaults.model,
        extra_headers=p.extra_headers,
    )


def gateway(args) -> None:
    """Start the nanobot gateway."""

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    with ConfigManager(args.config) as config:
        bus = MessageBus()
        provider = _make_provider(config)
        session_manager = SessionManager(config.workspace_path)

        agent = AgentLoop(
            config=config,
            bus=bus,
            provider=provider,
            session_manager=session_manager,
        )

        channels = ChannelManager(config, bus)

        print(f"{__logo__} nanobot gateway starting")
        if channels.channels:
            print(f"Channels: {', '.join(channels.channels)}")
        else:
            print("Warning: no channels enabled")

        async def run():
            try:
                async with channels:
                    await agent.run()
            except KeyboardInterrupt:
                print("\nShutting down...")
                agent.stop()

        asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nanobot",
        description="nanobot — personal AI assistant gateway",
    )
    parser.add_argument(
        "--config", type=Path, default="config/config.yaml", help="config.yaml file to use"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable debug logging",
    )
    args = parser.parse_args()
    gateway(args)


if __name__ == "__main__":
    main()
