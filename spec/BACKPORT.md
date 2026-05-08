# BACKPORT — TeachClaw → BenchClaw

BenchClaw forked into TeachClaw at `c8dde70` (Media import). Eighty-seven
commits later, TeachClaw has accumulated refactors, bug fixes, and tooling
improvements that are valuable to BenchClaw independently of TeachClaw's
TA-bot domain (lessons, citations, knowledgebase). This spec selects those
generic improvements and stages them as a phased backport.

## Out of scope

The following TeachClaw work is explicitly **not** backported:

- **Citations** — the entire `benchclaw.citations` package, `<citation>`
  parsing/validation, kb-source links, citation pushback retries, and the
  per-user reply-record dict that supports them.
- **Onboarding richness** — the two-stage `/start` welcome, example-prompt
  keyboards, slide-code auth flow. BenchClaw keeps its current minimal
  `/start`.
- **Mermaid rendering** — `rendering/mermaid.py`, `mmdc` shellout, caching,
  config flags, tests. Not used in BenchClaw.
- **Persona overlays** — `personalities.py`, persona switching commands,
  persona-tail injection. **However**, the *mechanism* used to inject
  persistent synthetic messages near the prompt tail is preserved (see
  Phase 3) so future features can use it.
- **Per-user media / filesystem sandboxing** — BenchClaw retains the
  original everyone-shares-media-and-filesystem view. No per-conversation
  media partitioning, no per-user `read_roots` carving.
- **Lessons / SWITCHMODE / workspace-as-lesson** — TeachClaw domain.
- **WhatsApp removal** — BenchClaw keeps WhatsApp + the Node bridge.
- **`claude_code` and `smtp_email` channel removal** — BenchClaw keeps both
  unless the maintainer opts in separately.

## Operating principles

- **No backwards compatibility.** On-disk formats (cron store, session
  JSONL) may change without migration shims. Document the break in the
  phase that introduces it; do not write loaders for old formats.
- **Phases land independently.** Each phase ends with a working tree and
  green tests. Earlier phases do not depend on later ones.
- **Test before refactor.** Phase 1 lands the test scaffolding so later
  phases have a regression net.
- **Keep the WhatsApp channel in scope.** Every phase that touches
  `BaseChannel`, `ChannelManager`, `MessageBus`, or outbound dispatch
  must verify WhatsApp behaviour is preserved.

---

## Phase 1 — Test infrastructure (foundation)

**Goal:** establish the regression net before refactoring. No production
code changes.

- Wire `pytest-cov` into the default `pytest` invocation; record branch
  coverage; add `fail_under` gate (start at the current floor, raise as
  later phases improve it). (TeachClaw: `3bce18d`)
- Add tests for `LiteLLMProvider._parse_response` covering tool-call
  extraction, JSON fallback, missing `finish_reason`, Qwen/Gemma quirks,
  reasoning-content passthrough, and multi-tool ordering. (`578e1dd`)
- Add tests for `web_search` and `web_fetch` with `httpx` mocking,
  covering API-key validation, response formatting, URL validation,
  HTML/JSON extraction, truncation, and HTTP error paths. (`b4c6382`)
- Add tests for cron schedule kinds and the cron store's state machine
  (see Phase 4 for the schedule simplification this anticipates).
  (`1ed00d8`)
- Add tests for Telegram `markdown_to_telegram_html` and `split_long`.
  (`a151f91`)
- Add tests for state-mutating Telegram commands (`/clear`, `/forgetme`,
  `/setsecret`, `/whoauthed`) using lightweight fakes instead of a real
  `Application`. (`062eac7`)

**Acceptance:** `pytest --cov` runs cleanly; coverage gate enforced in CI.

---

## Phase 2 — Telegram channel package split

**Goal:** turn the 1.2k-line `channels/telegrm.py` monolith into a focused
package, and land the channel-level bug fixes that block clean reasoning
about it.

### 2a. Split into a package

Replace `channels/telegrm.py` with `channels/telegrm/` containing:

- `channel.py` — lifecycle, `make_channel`, `background()`, `send()`
- `config.py` — `TelegramConfig`, slash-command lists
- `state.py` — typed inbound/outbound state objects
- `markdown_html.py` — `markdown_to_telegram_html`, `split_long`
- `auth_gate.py` — shared-secret check, per-user marker, group/DM gating
- `inbound.py` — message → bus translation
- `outbound.py` — bus → Telegram dispatch (typed `OutboundSegment` list)
- `reactions.py` — reaction handling
- `commands.py` — slash-command handlers (`/clear`, `/forgetme`, etc.)
- `typing_loop.py` — typing indicator refresh

Pure refactor — no behaviour change. (`0ccb9a9`)

### 2b. Outbound pipeline cleanup

- Unify the send pipeline around a typed `OutboundSegment` list — text,
  media, reactions all flow through one path. (`f5351ac`)
- Collapse `_safe_send_text` into `_post`; tidy reaction dispatch.
  (`29ffc81`)

### 2c. Typing-indicator fixes

- Per-chat typing dedupe: replace the `_typing_active: bool` field with
  `{chat_id: bool}` so a typing bubble in chat A no longer suppresses one
  in chat B. (`06fbf89`)
- Send the initial `chat_action` inline (await first HTTP call before
  the dispatcher proceeds) so fast LLM replies don't lose the typing
  indicator. (`b5f07bb`)

**Acceptance:** Telegram package imports green; DM behaviour byte-identical
to pre-split; typing dedupe regression tests pass; WhatsApp channel
untouched.

**Out of scope.** Reaction handling (heart reactions, threaded reaction
replies, emoji swap/tombstones) and group chat support (admin-gated auth,
per-(chat_id, sender_id) rate limiting, admin-only state mutations,
shared group sessions) are dropped from BenchClaw. Both were valuable for
TeachClaw's classroom domain but pull in per-user state and admin gating
that BenchClaw doesn't carry.

---

## Phase 3 — Agent loop split + tail-injection mechanism

**Goal:** turn the monolithic `agent/loop.py` into a small set of focused
modules, and generalise synthetic-message injection so future features
can hook persistent messages into the prompt tail without owning the
loop.

### 3a. Module extraction

Land the following extractions in order. Each step is a self-contained
refactor with no behaviour change.

1. **`agent/loop_state.py`** — `AddressState`, `ToolCallTracker`,
   `TurnOutcome` enum. `ToolCallTracker` owns the live `asyncio.Task`
   handles keyed by `tool_call_id` plus the in-flight name set.
   (`8d27946`, `0e09441`)
2. **`agent/dump.py`** — pretty-printed prompt dump for debugging,
   isolated from loop logic. Mark omitted from coverage. (`8d27946`,
   `7ad5a4a`)
3. **`agent/prompt.py`** — `PromptBuilder` owns system-prompt rendering,
   render-options selection, message-list construction, and the
   cache-prefix boundary. (`0e09441`)
4. **`agent/response.py`** — `ResponseHandler` owns tool dispatch,
   tool-result stringification, and the user-visible reply publish.
   (`3daba3b`) Drop the citation-validation hooks — BenchClaw doesn't
   ship citations.
5. **`agent/compactor.py`** — proactive compaction, summarisation, token
   estimation. `AgentLoop.compactor.maybe_compact()` is called per turn.
   (`2c878ce`)
6. **`agent/cache_monitor.py`** — per-address watchdog that fingerprints
   the stable prompt prefix and warns once per fingerprint when the
   prefix drifts unexpectedly. Drop snapshots on `/clear` and
   `/forgetme`. (`cdbe56c`, `7cf4e9d`)

### 3b. Lifecycle cleanup

- Replace `AgentLoop.run()`'s ad-hoc task tracking with an
  `asyncio.TaskGroup` + `AsyncExitStack`. The `TaskGroup` owns every
  per-address task and every long-running tool task (cron, etc.).
  (`71f2a0c`)
- Subscribe to `bus.subscribe_new_addresses()` and spawn
  `_address_loop` per `MessageAddress` lazily.
- Collapse `BatchApplication`; extract a per-address `_build_call_ctx`
  factory that owns storage-layout knowledge. (`41ea3a4`)
- Skip the follow-up LLM call after a `terminal_when_lone` tool turn —
  the tool already produced the user-visible reply, so don't nudge the
  model for a second response. (`643a2d8`)

### 3c. Tail-injection mechanism (persona-free)

TeachClaw moved persona out of the system prompt and into a synthetic
tail (`<persona>`, `<current_time>`, `<storage_listing>`) so persona
switches don't bust the cache prefix. BenchClaw skips persona but
**keeps the mechanism**:

- `PromptBuilder._inject_tail()` returns
  `(messages, stable_prefix_end)` — a list of synthetic tail messages
  plus the index marking the cacheable boundary.
- Built-in tail entries: `<current_time>`, `<storage_listing>`. (Drop
  `<persona>` entirely.)
- Expose a registration hook so other modules (event-loop sources,
  tools) can append persistent synthetic messages to the tail without
  reaching into the loop. This is the substrate for future "ambient
  fact" features.
- The cache monitor uses `stable_prefix_end` as the boundary it
  fingerprints. (`cdbe56c`, `5e2e2f0`, `c9d0d34`)

### 3d. System-prompt content

- Include model ID and context-window size in the system prompt so the
  model can reason about its own budget. (`7cf4e9d`)
- Do **not** include persona block.

**Acceptance:** all existing agent-loop tests pass; new tests cover
`TurnOutcome` transitions, tail injection, and cache monitor warnings;
WhatsApp + Telegram + claude_code + smtp_email channels continue to
deliver messages end-to-end.

---

## Phase 4 — Tool framework + cron simplification

**Goal:** standardise tool parameters on Pydantic, drop dead lifecycle
machinery, and rewrite the cron tool around durations.

### 4a. Tool framework cleanup

- Each tool declares a Pydantic `Params` model and an `execute(ctx,
  **kwargs)` that takes validated params. (`5e2e2f0`)
- Drop the `MessageTool` abstraction and the `Tool.background` /
  `Tool._task` lifecycle from the base class. (`11910d2`)
- Collapse `ProviderSpecs` into a 2-entry `_BACKENDS` dict (LiteLLM,
  Scripted). (`5e2e2f0`)
- Drop the self-shadowing `Params` redeclaration anti-pattern that
  TeachClaw cleaned up. (`148f928`)
- Tidy `_display_path` / `_resolve_path` helpers in the filesystem
  tools. (`1ccd1e4`)

### 4b. Cron tool rewrite

Drop `croniter` entirely. Rewrite the cron tool around duration strings:

- `Params`: `delay: str | None` (ISO duration or compact form like
  `"30m"`), `every: str | None` (interval), `prompt: str`, plus job ID.
- `CronSchedule.next_run(ref: datetime | None = None)` — `ref=None`
  returns the schedule's anchor so first-fire is well-defined without
  sentinel values.
- Flatten `CronStore`: drop `heapdict`, drop unused
  `last_status` / `last_error`, keep only `last_run_at` per job. Linear
  scan over the dict; job count is small.
- Wire `CronTool.run_loop` directly into `AgentLoop.run`'s `TaskGroup`
  (per Phase 3b). The tool no longer owns its own background task.
  (`5b343da`)

**No on-disk migration.** The new format is incompatible with the old;
operators wipe `cron/` on upgrade.

### 4c. Builtin tooling

- Inline skills enumeration into `PromptBuilder` (parse `SKILL.md`
  frontmatter directly with PyYAML; drop the `SkillsLoader` /
  `SkillInfo` classes). (`94b1d1a`) Only applies if BenchClaw retains
  skills infrastructure; skip if not.

**Acceptance:** cron tool tests from Phase 1 pass against the new
implementation; tool registry boots; `croniter` removed from
`pyproject.toml`.

---

## Phase 5 — Sessions, media, providers

**Goal:** make sessions crash-safe, fix media dispatch, and pick up
provider improvements.

### 5a. Session persistence

- Switch session persistence to JSONL: each `Session.append` /
  `Session.clear` writes one line immediately. No deferred flush in
  `__aexit__`. (`163c5d8`)
- `/clear` and `/forgetme` write `ClearEvent` markers instead of
  truncating the file. The rendered history skips past the marker, but
  the audit trail is preserved. (`163c5d8`)
- Promote `Session.render_history` to a public method; drop the
  `Session.messages` property and `get_history` shim. (`5e2e2f0`,
  `11910d2`)
- **Format break.** Old session files won't load; document in the
  changelog and wipe `sessions/` on upgrade.

### 5b. Media

- Keep the original everyone-shares-media-and-filesystem view. Do
  **not** introduce per-user media partitioning or per-conversation
  read-roots carving.
- `send_media` returns a JSON status (`{"status": "sent",
  "turn_complete": true, "path": ...}`) instead of an English sentence
  — Gemma misreads prose as a draft reply. (`de49d36`)
- Mark `send_media` as `terminal_when_lone = True` so a lone send_media
  turn isn't followed by a spurious model echo. (Phase 3b's
  `terminal_when_lone` skip applies.)
- Telegram outbound dispatches on MIME type (image/video/audio/
  document) so `.mp4` and friends actually upload correctly. (`f79d38f`)
- Caption-as-reply: a media tool's caption becomes the user-visible
  reply for that turn. (`f79d38f`)

### 5c. Providers

- Gemma sampling + thinking wiring in `LiteLLMProvider`. (`278cb9d`)
- Tool-call parsing fix from `8769e6b`.

**Acceptance:** `kill -9` mid-turn no longer drops in-memory tail; old
sessions don't load (expected); `.mp4` uploads on Telegram and
WhatsApp; LiteLLM provider tests from Phase 1 pass against Gemma and
Qwen response shapes.

---

## Phase ordering & dependencies

```
Phase 1 (tests)
   │
   ├──> Phase 2 (telegram split)            ─┐
   │                                          │
   └──> Phase 3 (agent loop split)            │
            │                                 │
            ├──> Phase 4 (tools + cron)       │── Phase 5 (sessions, media, providers)
            │                                 │
            └────────────────────────────────>┘
```

- Phase 2 and Phase 3 are independent and can land in either order
  after Phase 1.
- Phase 4 depends on Phase 3 (TaskGroup ownership of cron).
- Phase 5 depends on Phase 3 (uses tail-injection + ResponseHandler
  hooks for media) and is otherwise independent.

## Per-phase smoke checklist

For every phase, before merging:

- [ ] `pytest --cov` green; coverage gate not regressed.
- [ ] Manual: send a Telegram DM and confirm reply.
- [ ] Manual: send a Telegram group message (admin) and confirm reply.
- [ ] Manual: send a WhatsApp message and confirm reply.
- [ ] Manual: confirm typing indicator appears within 1s on both
      channels.
- [ ] Manual: trigger `/clear` and confirm session resumes empty.
- [ ] Manual: trigger a tool call (e.g. `read_file`) and confirm the
      reply integrates the result.
