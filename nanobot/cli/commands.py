"""CLI commands for nanobot."""

import asyncio
from nanobot import __logo__

# ============================================================================
# Gateway / Server
# ============================================================================


# TODO: Move this into the main app file.


def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the nanobot gateway."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import load_config
    from nanobot.session.manager import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting nanobot gateway on port {port}...")

    config = load_config()
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        tools_config=config.tools,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
    )

    channels = ChannelManager(config, bus)

    if channels.channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    async def run():
        try:
            async with channels:
                await agent.run()
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            agent.stop()

    asyncio.run(run())


if __name__ == "__main__":
    app()
