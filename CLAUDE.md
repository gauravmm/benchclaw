# nanobot — Claude Code Guide

nanobot is an ultra-lightweight personal AI assistant (~4,000 lines of core code). It connects one or more chat channels (Telegram, WhatsApp, Email, …) to an LLM agent via an async message bus.

## Package Layout

```
nanobot/
  agent/       AgentLoop, memory, context, skills, subagent
  bus/         Async message bus (InboundMessage / OutboundMessage events)
  channels/    One file per chat platform + base.py + manager.py
  cli/         Typer CLI entry point (commands.py)
  config/      Pydantic schema (schema.py) + YAML loader (loader.py)
  cron/        Scheduled task service
  heartbeat/   Periodic self-prompting service
  providers/   LLM provider registry + transcription
  session/     Conversation session manager
  skills/      Bundled agent skill prompts
bridge/        Node.js WhatsApp bridge (@whiskeysockets/baileys)
config/        Runtime config file location (config.yaml)
```

## Key Conventions

**Channels** — each platform lives in `nanobot/channels/<name>.py` and follows this pattern:
- Define `<Name>Config(ChannelConfig)` in the same file (no `enabled` field — presence in config is sufficient)
- Define `<Name>Channel(BaseChannel)` with `background()` and `send()`
- `background()` runs forever until `CancelledError`; do cleanup in a `finally` block
- `send()` is called by the dispatcher to deliver outbound messages
- `ChannelManager` (in `manager.py`) owns all channel tasks and cancels them on shutdown

**Config** — `nanobot/config/schema.py` is the single source of truth:
- `Config` is a `pydantic_settings.BaseSettings` (env prefix `NANOBOT_`, delimiter `__`)
- `ChannelConfigs` aggregates all channel configs via class-body imports (avoids circular deps with the `nanobot.channels.*` → `nanobot.config` import chain)
- Config is loaded from `config/config.yaml` (YAML, not JSON)

**Message Bus** — `InboundMessage` flows channel → `bus.inbound` → `AgentLoop`; `OutboundMessage` flows `AgentLoop` → `bus.outbound` → dispatcher → channel.

**Async task ownership** — `ChannelManager` creates and cancels all tasks; individual channels do not manage their own task handles.

## Active Refactor

The codebase is undergoing a large refactor to improve code quality. Key changes already made or in progress:

- Channel configs moved from `config/schema.py` into their respective channel files
- `enabled` removed from all channel configs
- `BaseChannel.start()` / `stop()` replaced with `background()` + asyncio task cancellation
- `ChannelManager` now takes `Config` and initialises all channels; stopping is via task cancellation, not `channel.stop()`
- Config serialisation switched from JSON to YAML

When editing channel or config code, follow the new patterns above rather than the old ones (which may still appear in git history or other channels not yet migrated).

## Running Locally

```bash
uv run nanobot gateway          # start all configured channels + agent
python -m nanobot.channels.whatsapp [ws://localhost:3001] [chat_id]  # test WhatsApp bridge
```

Config file: `config/config.yaml` (created automatically on first run with defaults).
