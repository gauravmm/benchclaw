# benchclaw

![benchclaw](benchclaw.png)

An ultra-lightweight personal AI assistant. Connects one or more chat channels (Telegram, WhatsApp, Email) to an LLM agent via a fully async message bus — the agent can receive and process new user messages while tools are still running in the background.

Built on nanobot but essentially a rewrite, focused on:

- Consistent architecture with less dead code
- Asynchronous tool use — the LLM can receive messages while tools are running
- Per-address message queues dispatched directly (no contested shared queue)
- Simplified LLM provider configuration via LiteLLM
- CRON-based scheduled tasks (replaces HEARTBEAT)

## Running

```bash
uv run benchclaw
```

Config file: `config/config.yaml` (created automatically on first run with defaults).

## Adding a Tool

1. Create `benchclaw/agent/tools/<name>.py`
2. Define a class inheriting `Tool`, implementing `name`, `description`, `parameters`, and `execute(ctx, **kwargs)`
3. Call `register_tool("name", ToolClass)` at module level
4. Import the module in `benchclaw/agent/tools/__init__.py`

If the tool has config, call `register_tool_config("name", ConfigClass)` — the config becomes a field on `ToolsConfig` automatically.

Set `master_only = True` to exclude the tool from subagent registries.

`ToolContext` provides: `workspace`, `bus`, `address`, `background_tasks` (keyed by `tool_call_id`), and `is_subagent`.

## Adding a Channel

1. Create `benchclaw/channels/<name>.py`
2. Define `<Name>Config(ChannelConfig)` with a `make_channel(bus)` method
3. Define `<Name>Channel(BaseChannel)` with `background()` and `send(msg)`
   - `background()` runs forever; do cleanup in a `finally` block on `CancelledError`
   - `send()` delivers outbound messages
4. Call `register_channel("name", ConfigClass)` at module level

Channel config is picked up automatically — presence in `config/config.yaml` is sufficient to enable the channel, no `enabled` flag needed.
