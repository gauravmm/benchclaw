# Summon Attention Mode

## Summary

Add a reusable inbound attention filter for channels, with per-channel policy override (`email` always attends), and represent attention durations as `timedelta` with text parsing/serialization. Replace split summon flags with one field that encodes both presence and source.

## Key Changes

- Shared channel config in `ChannelConfig`:
  - `attention_policy`: enum (`always`, `summon_group`)
  - `attention_lookback`: `timedelta` (default 5m)
  - `attention_gap`: `timedelta` (default 2m)
- Add duration parsing + serialization on config fields:
  - Accept `timedelta`, numeric seconds, and human text like `300s`, `5m`, `2 min`, `1h30m`.
  - Serialize back to compact text (for YAML readability), preferring `h/m/s` form.
- Add reusable inbound attention filter (shared module, invoked by `BaseChannel` before bus publish):
  - For `always`: forward everything.
  - For `summon_group`: private chats always forward; group chats require summon to enter attention mode.
  - On summon in group chat, replay contiguous history backward up to `attention_lookback`, stopping at first gap `> attention_gap`.
  - Keep attention on while inter-message gaps are `<= attention_gap`; turn off after `> attention_gap`.
- `BaseChannel._handle_message(...)` gains optional source timestamp and applies inbound filters.
- Summon metadata contract becomes a single field:
  - `summon`: `null | "mention" | "reply"` (null = not a summon trigger)
- Telegram:
  - Detect summon from mention or reply-to-bot.
  - Set `metadata["_summon_source"]` (internal) and pass source timestamp from Telegram message date.
- WhatsApp:
  - Bridge protocol extended with mention/reply context (plus bot identity needed for detection).
  - Python channel maps bridge data into `metadata["_summon_source"]` (internal) and source timestamp.

## Public Interfaces / Types

- `ChannelConfig` adds `attention_policy`, `attention_lookback: timedelta`, `attention_gap: timedelta` with validator/serializer behavior above.
- `BaseChannel._handle_message` accepts `timestamp` for source-time attention decisions.
- Inbound metadata contract: channels set `_summon_source` (internal, consumed and popped by filter); filter sets `summon: null | "mention" | "reply"` on forwarded messages (public output). `_summon_source` is never visible outside the filter.

## Test Plan

- Config tests:
  - Parse durations from `timedelta`, numeric seconds, and text (`5m`, `2 min`, `1h30m`).
  - Serialize configured durations to stable compact text.
- Filter tests:
  - Group non-summon dropped when attention off.
  - Summon starts attention and replays bounded contiguous history.
  - Replay stops at first gap above threshold.
  - Attention persists across short gaps and expires after long gap.
  - `always` policy always forwards.
- Channel mapping tests:
  - Telegram mention/reply sets `_summon_source` correctly.
  - WhatsApp bridge mention/reply maps to `_summon_source`.
- Regression:
  - `allow_from` checks still apply before bus publication.

## Assumptions

- Accepted summon sources are only `mention` and `reply`.
- Summon state remains in-memory (not persisted across process restarts).
- Duration text parser is strict enough to reject ambiguous strings and surface config errors early.
