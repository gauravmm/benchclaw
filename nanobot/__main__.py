"""Entry point for nanobot: python -m nanobot [options]"""

import argparse
import asyncio
import logging
from pathlib import Path

from nanobot import __logo__
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import ToolContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config import ConfigManager
from nanobot.providers.litellm_provider import LiteLLMProvider


def gateway(args) -> None:
    """Start the nanobot gateway."""
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR)

    bus = MessageBus()

    with ConfigManager(args.config) as config:
        provider = LiteLLMProvider(config.provider)
        channels = ChannelManager(config, bus)

        master_ctx = ToolContext(
            workspace=config.workspace_path,
            bus=bus,
            # subagent_manager=self.subagents,
        )
        tools = ToolRegistry(config.tools, master_ctx)

        print(f"{__logo__} nanobot gateway starting")
        if channels.channels:
            print(f"Channels: {', '.join(channels.channels)}")
        else:
            print("Warning: no channels enabled")

        async def run():
            agent = AgentLoop(config=config, bus=bus, provider=provider, tools=tools)

            try:
                async with channels, tools:
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
