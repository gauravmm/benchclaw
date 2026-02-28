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


def gateway(args) -> None:
    """Start the nanobot gateway."""
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR)

    with ConfigManager(args.config) as config:
        bus = MessageBus()
        provider = LiteLLMProvider(config.provider)

        agent = AgentLoop(
            config=config,
            bus=bus,
            provider=provider,
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
            except asyncio.CancelledError:
                return

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
        help="enable info logging",
    )
    args = parser.parse_args()
    gateway(args)


if __name__ == "__main__":
    main()
