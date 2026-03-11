# Plan: Awake/Asleep Indicator For Summon Channels

Expose the attention state transitions already computed by `InboundAttentionFilter` as bus events, then implement two concrete indicator strategies: **T2** (Telegram bot profile description) and **W2** (message-level badge in bot replies).

---

## Implementation Steps

### 1. Add `AttentionEvent` to the bus

In [benchclaw/bus.py](benchclaw/bus.py), add alongside `TypingEvent`:

```python
@dataclass(frozen=True)
class AttentionEvent:
    """Attention (awake/asleep) state change for one address."""
    address: MessageAddress
    awake: bool  # True = just became awake, False = just went asleep
```

Update the outbound queue type annotation:

```python
outbound: dict[str, asyncio.Queue[OutboundMessage | TypingEvent | AttentionEvent]]
```

Add `publish_attention(event: AttentionEvent)` on `MessageBus` — calls `publish_outbound(event)`, routing to `outbound[event.address.channel]`.

### 2. Emit transitions from `InboundAttentionFilter`

`apply()` in [benchclaw/channels/attention.py](benchclaw/channels/attention.py) currently mutates `state.attention_active` silently. Change its return type to `tuple[list[InboundMessage], list[AttentionEvent]]`. Emit:

- `AttentionEvent(address, awake=True)` when `attention_active` transitions False → True (summon received).
- `AttentionEvent(address, awake=False)` when `_expire_attention_if_needed` transitions True → False.

The address is `MessageAddress(channel=self._channel, chat_id=chat_id)`.

Note: expiry is currently **lazy** — it only fires when the next message arrives. This is acceptable for W2. For T2 (global profile), proactive expiry is better: see step 5.

### 3. Thread transitions through `_handle_message`

In `BaseChannel._handle_message` ([benchclaw/channels/base.py](benchclaw/channels/base.py)), unpack the tuple returned by `apply()`:

```python
inbound, attention_events = self._inbound_attention.apply(...)
await self.bus.publish_inbound(*inbound)
for evt in attention_events:
    await self.bus.publish_attention(evt)
```

### 4. Dispatch `AttentionEvent` in `ChannelManager`

In `_dispatch_channel` ([benchclaw/channels/manager.py](benchclaw/channels/manager.py)), add a branch alongside the existing `TypingEvent` branch:

```python
elif isinstance(msg, AttentionEvent):
    await channel._handle_attention(msg)
```

Add `_handle_attention(event: AttentionEvent)` to `BaseChannel` — default no-op, same pattern as `notify_typing`.

### 5. Proactive expiry timer for T2

Since T2 is a global profile update, we can't rely on the next message to trigger the asleep transition. In `TelegramChannel.background()`, after the polling loop is running, start a periodic task (every 30 s) that calls `_inbound_attention.check_expired(now)` and publishes any resulting `AttentionEvent`s.

Add `check_expired(now: datetime) -> list[AttentionEvent]` to `InboundAttentionFilter`: iterates all `_group_state` entries, calls `_expire_attention_if_needed`, and returns transition events for any that flipped.

### 6. Implement T2: Telegram bot profile description

In `TelegramConfig` ([benchclaw/channels/telegrm.py](benchclaw/channels/telegrm.py)):

```python
awake_description: str = "🟢"   # set as bot description when any chat is awake
asleep_description: str = ""    # restored when all chats go asleep (empty = no-op / original)
indicator_debounce: float = 10.0  # seconds; avoid flapping on rapid summon/release
```

In `TelegramChannel`:

- Track `_awake_chat_ids: set[str]` — the set of currently-awake chat IDs.
- Override `_handle_attention(event)`: add/remove from `_awake_chat_ids`, then schedule a debounced call to `_sync_profile()`.
- `_sync_profile()`: if `_awake_chat_ids` is non-empty, call `await bot.set_my_description(config.awake_description)`; else call `await bot.set_my_description(config.asleep_description)`. Log on failure; do not raise.
- Debounce via `asyncio.Task` + cancel-and-reschedule pattern (same as existing `_typing_tasks`).
- The `_bot_user_id` / `_bot_username` fields are already stored after `get_me()` — reuse that init path.

### 7. Implement W2: WhatsApp message badge

In `WhatsAppConfig` ([benchclaw/channels/whatsapp.py](benchclaw/channels/whatsapp.py)):

```python
awake_badge: str = "🟢 "  # prepended to bot replies when attention is active; "" to disable
```

In `WhatsAppChannel`:

- Track `_awake_chat_ids: set[str]`.
- Override `_handle_attention(event)`: add/remove from `_awake_chat_ids`.
- In `send(msg)`: if `msg.address.chat_id in _awake_chat_ids` and `config.awake_badge`, prepend `config.awake_badge` to `msg.content` before sending.

No bridge changes needed.

---

## Config Defaults

```yaml
telegram:
  awake_description: "🟢"
  asleep_description: ""
  indicator_debounce: 10.0

whatsapp:
  awake_badge: "🟢 "
```

---

## Verification

1. **Unit — transition emission**: mock `InboundAttentionFilter.apply()`, confirm `AttentionEvent(awake=True)` emitted on first summon for a group chat, `AttentionEvent(awake=False)` emitted after gap expires (both lazy and via `check_expired`), and no duplicate events on repeated summons within the window.
2. **Unit — bus routing**: `publish_attention` enqueues to the correct channel's outbound queue; other channels' queues are unaffected.
3. **Unit — T2 debounce**: rapid alternating attention events produce at most one `set_my_description` call per debounce window.
4. **Unit — W2 badge injection**: `send()` prepends badge iff chat_id is in `_awake_chat_ids`; private-chat messages (never in `_awake_chat_ids`) are unmodified.
5. **Manual — Telegram**: summon bot in group → bot description shows `awake_description` within ~10 s; wait for gap → description resets.
6. **Manual — WhatsApp**: summon bot in group → next bot reply is prefixed with `awake_badge`; after gap, badge stops appearing.
7. **Failure path**: Telegram `set_my_description` raises → logged, no crash, retried on next sync.

---

## Relevant Files

- [benchclaw/bus.py](benchclaw/bus.py) — add `AttentionEvent`, update outbound queue type, add `publish_attention`.
- [benchclaw/channels/attention.py](benchclaw/channels/attention.py) — change `apply()` return type, emit transition events, add `check_expired()`.
- [benchclaw/channels/base.py](benchclaw/channels/base.py) — unpack attention events in `_handle_message`, add `_handle_attention` no-op.
- [benchclaw/channels/manager.py](benchclaw/channels/manager.py) — dispatch `AttentionEvent` to `_handle_attention`.
- [benchclaw/channels/telegrm.py](benchclaw/channels/telegrm.py) — T2: `_awake_chat_ids`, debounced `_sync_profile`, `check_expired` timer.
- [benchclaw/channels/whatsapp.py](benchclaw/channels/whatsapp.py) — W2: `_awake_chat_ids`, badge injection in `send()`.
- [summon-attention-plan.md](summon-attention-plan.md) — upstream summon contract (`_summon_source` → `summon` in metadata).

---

## Decisions

- **T2 + W2 only.** T1 (typing loop) conflicts with the existing agent typing indicator. T3 (message badge) is subsumed by W2. W1 (WhatsApp typing) is separate presence mechanism — not needed.
- **Lazy expiry is fine for W2** (next-message trigger). T2 needs the proactive `check_expired` timer so the profile resets even in silent chats.
- **Per-chat tracking, global T2 projection**: T2 fires when the set of awake chats crosses zero. This means the bot shows "awake" if *any* group has summoned it.
- **No new bus inbound event type.** `AttentionEvent` goes on the *outbound* queue (channel → display), not inbound (channel → agent). The agent does not need to know about indicator state.
