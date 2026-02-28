"""Entry point for nanobot: python -m nanobot [options]"""

import argparse
import asyncio
import logging
import sys

from nanobot import __logo__
from nanobot.agent.loop import AgentLoop
from nanobot.bus import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config import ConfigManager
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.session.manager import SessionManager


def _make_provider(config):
    p = config.get_provider()
    model = config.agents.defaults.model
    if not (p and p.api_key):
        print("Error: No API key configured.")
        print("Set one in config/config.yaml under providers section.")
        sys.exit(1)
    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        provider_name=config.get_provider_name(),
    )


def gateway(port: int = 18790, verbose: bool = False) -> None:
    """Start the nanobot gateway."""

    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    with ConfigManager() as config:
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

        print(f"{__logo__} nanobot gateway starting on port {port}")
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
        "--port",
        "-p",
        type=int,
        default=18790,
        metavar="PORT",
        help="gateway port (default: 18790)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable debug logging",
    )
    args = parser.parse_args()
    gateway(port=args.port, verbose=args.verbose)


if __name__ == "__main__":
    main()
