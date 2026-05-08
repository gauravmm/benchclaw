# WHATSAPP PARITY — bring WhatsApp up to Telegram's outbound feature set

After Phases 2 and 5 of `BACKPORT.md`, BenchClaw's Telegram channel
gained a typed segment pipeline and MIME-based outbound media dispatch.
WhatsApp was left as the pre-Phase-2 monolith: a single 304-line
`channel.py` whose `send()` rejects non-image media outright. This spec
brings WhatsApp up to par on outbound media and the structural split,
without porting Telegram's markdown / chunking / typing-indicator work
(those are intentionally out of scope).

## Out of scope

- **Long-message splitting.** WhatsApp's text limit is high enough
  (~65k chars) that the model rarely hits it; no splitter today.
- **Markdown → WhatsApp translation layer.** Rather than convert
  Markdown into WhatsApp's syntax server-side (lossy and
  high-churn), Phase D teaches the model WhatsApp's primitives
  directly via AGENTS.md so it can author native rich messages.
- **Typing indicator.** The current fire-and-forget WS ping is
  acceptable; per-chat dedupe + refresh is Telegram-only.
- **Inbound parity.** WhatsApp's inbound path is already richer than
  Telegram's — bridge-side mention resolution via `nameCache`,
  protocol-aware `summon` detection from `botJids`. No work needed.

## Operating principles

- **No backwards compatibility.** The bridge protocol's `imageBase64` /
  `imageMimeType` fields are renamed to a generic shape; the Node
  bridge and Python channel land in the same commit. Operators
  redeploy both halves on upgrade.
- **Phases land independently.** Phase A (split) is a no-op refactor;
  B (segments + protocol) is a single atomic landing; C (caption)
  builds on B.
- **Match Telegram's shape, not its code.** `outbound.py` /
  `state.py` should look like the Telegram modules so the next reader
  doesn't have to learn two pipelines. Reuse names: `OutboundSegment`,
  `TextSegment`, `MediaSegment`, `plan_segments`, `dispatch`.

---

## Phase A — Package split (no behaviour change)

**Goal:** make `channels/whatsapp/` mirror `channels/telegrm/`'s shape
so subsequent phases land in a focused outbound module.

Replace the current contents:

```
channels/whatsapp/
  __init__.py
  address.py        (unchanged)
  bridge.py         (unchanged)
  channel.py        (-> split)
```

with:

```
channels/whatsapp/
  __init__.py
  address.py        (unchanged)
  bridge.py         (Pydantic models + outbound payload types)
  channel.py        (lifecycle, `make_channel`, `background()`,
                     `send()` — one-line delegate to outbound.send)
  config.py         (`WhatsAppConfig`, the `make_channel` factory)
  inbound.py        (bridge event → bus translation:
                     _handle_bridge_message, _handle_bridge_inbound,
                     _replace_mentions, _detect_summon_source,
                     _message_metadata, _save_bridge_media)
  outbound.py       (bus → bridge dispatch — see Phase B for body)
  state.py          (typed OutboundSegment list — see Phase B)
```

Pure refactor: imports reorganised, no new types, no new behaviour.
WhatsApp inbound tests stay green; the Node bridge is untouched.

**Acceptance:** `pytest -q` green; the `__init__.py` re-exports
`WhatsAppChannel`, `WhatsAppConfig` so existing imports continue to
work.

---

## Phase B — Typed OutboundSegment pipeline + MIME-based dispatch

**Goal:** WhatsApp can send video, audio, and arbitrary documents
through the same code path as images. The Python side dispatches on
MIME family; the Node bridge grows matching message types.

### B1. State + outbound module (Python)

`state.py` — copy-shape from `channels/telegrm/state.py`:

```python
@dataclass(frozen=True)
class TextSegment:
    body: str

@dataclass(frozen=True)
class MediaSegment:
    path: Path
    mime: str
    caption: str | None

OutboundSegment = TextSegment | MediaSegment
```

`outbound.py` — `send()` (top-level) → `plan_segments` → `dispatch`,
identical structure to Telegram's. `dispatch`'s `match` arm on
`MediaSegment` reads the MIME prefix and chooses the bridge payload
shape:

```python
kind = mime.split("/", 1)[0] if mime else ""
match kind:
    case "image":   field, mime_field = "imageBase64",    "imageMimeType"
    case "video":   field, mime_field = "videoBase64",    "videoMimeType"
    case "audio":   field, mime_field = "audioBase64",    "audioMimeType"
    case _:         field, mime_field = "documentBase64", "documentMimeType"
                    # arbitrary file → document path; original_name
                    # is included so the receiver sees a sensible
                    # filename instead of the bridge's generated one.
```

Drop the current `send()`'s hard image-only check (the `ValueError`
on non-`image/*` MIME). Drop the `if not mime or not mime.startswith
("image/")` guard.

### B2. Bridge protocol (TypeScript)

In `bridge/src/server.ts`'s outbound command type, replace
`imageBase64?` / `imageMimeType?` with a generic media block:

```typescript
interface SendCommand {
  type: 'send';
  to: string;
  text?: string;
  // Exactly one of the following four media payloads, or none.
  imageBase64?: string;     imageMimeType?: string;
  videoBase64?: string;     videoMimeType?: string;
  audioBase64?: string;     audioMimeType?: string;
  documentBase64?: string;  documentMimeType?: string;
  documentName?: string;    // surfaced as the file name in WhatsApp
}
```

In `whatsapp.ts`, extend `sendMessage()` to dispatch on which payload
arrived:

```typescript
if (cmd.imageBase64)        { sendMessage(to, { image:    Buffer.from(cmd.imageBase64,    'base64'), caption: text, mimetype: cmd.imageMimeType ?? 'image/jpeg' }); }
else if (cmd.videoBase64)   { sendMessage(to, { video:    Buffer.from(cmd.videoBase64,    'base64'), caption: text, mimetype: cmd.videoMimeType ?? 'video/mp4' }); }
else if (cmd.audioBase64)   { sendMessage(to, { audio:    Buffer.from(cmd.audioBase64,    'base64'),                mimetype: cmd.audioMimeType ?? 'audio/ogg; codecs=opus', ptt: cmd.audioMimeType?.includes('opus') }); }
else if (cmd.documentBase64){ sendMessage(to, { document: Buffer.from(cmd.documentBase64, 'base64'), caption: text, mimetype: cmd.documentMimeType ?? 'application/octet-stream', fileName: cmd.documentName }); }
else if (text)              { sendMessage(to, { text }); }
```

`audioMimeType` containing `opus` toggles Baileys' `ptt: true` so an
.ogg/opus voice note shows the WhatsApp PTT bubble; everything else
sends as a regular audio file.

### B3. Resolve outbound media (Python)

Lift the resolve-and-MIME-probe block out of `send()` into a
`resolve_outbound_media(channel, msg) -> tuple[Path, str]` helper in
`outbound.py`, mirroring Telegram's. Use `media_repo.resolve_file`
when configured; fall back to `filetype.guess_mime`.

### B4. Acceptance criteria

- `.mp4` upload via `send_media` reaches WhatsApp as a video.
- `.mp3` and `.ogg` upload as audio (Opus-tagged → PTT bubble).
- `.pdf` upload as a document with the original filename surfaced.
- `.jpg` / `.png` continue to upload exactly as before.
- An empty media payload (text-only outbound) takes the `text` branch.
- Bridge rejects malformed payloads (e.g. both `imageBase64` and
  `videoBase64` set) with a `BridgeErrorEvent` rather than sending
  ambiguously.

**Format break.** Operators redeploying must rebuild and restart the
Node bridge. The bridge protocol is not versioned today; document the
upgrade step in the changelog.

---

## Phase C — Caption-as-reply

**Goal:** when the model issues a `send_media` with a `caption`,
WhatsApp delivers media + caption as a single message rather than the
caption being silently lost (today's behaviour for images sometimes,
and always for non-images post-Phase B since we just won't have set
`payload['text']` correctly).

- `outbound.dispatch` for `MediaSegment` passes `caption` into the
  bridge payload's `text` field. The bridge already has caption
  semantics for image / video / document via Baileys (audio is the
  exception — WhatsApp itself doesn't support audio captions, so for
  `MediaSegment(mime='audio/...', caption=non_empty)` the bridge sends
  the audio first, then the caption as a follow-up text message).
- Phase 5b's `terminal_when_lone` flag on `send_media` is already in
  place; no agent-loop changes.

**Acceptance:**
- `send_media({path: 'foo.mp4', caption: 'context here'})` arrives
  on WhatsApp as one video message with the caption attached.
- `send_media({path: 'foo.ogg', caption: 'see attached'})` arrives as
  a voice note followed by a separate text "see attached" message.
- Empty caption ⇒ no orphaned blank text message.

---

---

## Phase D — Teach the model WhatsApp's formatting primitives

**Goal:** when the active session is on WhatsApp, the model emits
WhatsApp-native rich text directly — no server-side translation,
no raw `**asterisks**` leaking through to users.

The system prompt already surfaces the active channel in the
`Session: whatsapp / <chat_id>` line (and via `session_label`), so
the model can branch on it. The missing piece is workspace
documentation describing what to emit — once it's in `AGENTS.md`
the model picks it up the same way it picks up tool descriptions.

### D1. Add a "Per-channel formatting" section to `AGENTS.md`

Append a section like:

```markdown
## Per-channel formatting

The active channel appears in your system prompt's `Session:` line.
Match the channel's native syntax — using one channel's markup on
another produces visible noise.

### Telegram

Telegram replies are post-processed from Markdown into Telegram HTML.
Use standard Markdown:
- `**bold**` or `__bold__`
- `_italic_`
- `~~strikethrough~~`
- `` `inline code` `` and ``` ```fenced``` ``` blocks
- `[label](https://example.com)` links
- `# Heading`, `> quote`, `- bullet` / `* bullet`

### WhatsApp

WhatsApp does not understand Markdown. Emit its native syntax directly:
- `*bold*` (single asterisks; pairs render as literal characters)
- `_italic_`
- `~strikethrough~` (single tildes)
- `` `inline code` `` and ``` ```fenced``` ``` blocks
- `> quoted line` at the start of a line
- Bulleted lists: `- item` or `* item` at line start
- Numbered lists: `1. item` at line start
- No inline-link markup. Send the URL on its own — WhatsApp
  auto-links and previews it. Do not write `[label](url)`; the
  brackets render literally.
- No headings. WhatsApp has no heading syntax. Use a bold first
  line followed by a blank line if you need emphasis.
- Mentions: `@<phone-without-plus>` notifies that participant in a
  group chat (e.g. `@14155550100`).

### Other channels

If the active channel is neither Telegram nor WhatsApp, default to
plain text. Do not emit Markdown speculatively.
```

### D2. Stop sending Markdown-style mention syntax

WhatsApp's inbound path rewrites `@<jid_localpart>` → `@<display_name>`
in the body for readability (`channel.py:_replace_mentions`). The
*outbound* path doesn't need anything new for D — the bot just
needs to know to write `@14155550100` (raw) when it wants to ping
someone in a group, and the WhatsApp client will turn that into a
linked mention. Document this in the AGENTS.md section above.

### D3. Acceptance

- Manual: ask the bot in a WhatsApp DM to "send me a bulleted list
  of three things, with the second item bold." Confirm the reply
  uses `*…*` for bold and `- ` bullets — no `**…**` leaks.
- Manual: same prompt in a Telegram DM continues to use Markdown
  (`**…**`).
- Manual: WhatsApp group chat — ask the bot to "tag @+14155550100".
  The reply renders as a clickable mention in the WhatsApp client.

### D4. Notes

- This depends on Phase 5b's tail-injection mechanism only insofar
  as the channel name needs to be reliably visible in every prompt;
  it already is.
- It does **not** require any code change in `channels/whatsapp/`.
  The only file touched is `workspace_default/AGENTS.md` (and any
  deployed `<workspace>/AGENTS.md` that overrides it — operators
  pull the new section in on upgrade).
- No tests fit this phase well; it's a documentation change whose
  effect is observable only at the LLM behavioural level. The smoke
  checks above are the verification.

---

## Phase ordering & dependencies

```
Phase A (split)
   │
   ├──> Phase B (segments + bridge MIME dispatch)
   │        │
   │        └──> Phase C (caption-as-reply)
   │
   └──> Phase D (AGENTS.md formatting primitives)
```

A is a no-op refactor and can land alone. B is the load-bearing
phase — Python and Node land atomically. C is a small follow-up to
B. D is independent of A/B/C and can land at any point — pure
workspace doc edit, no code change.

## Per-phase smoke checklist

For every phase, before merging:

- [ ] `pytest -q` green; coverage gate not regressed.
- [ ] Manual: send a WhatsApp text DM and confirm reply.
- [ ] Manual: send a WhatsApp image with caption and confirm round-trip.
- [ ] Manual (Phase B+): trigger `send_media` for each MIME family
      (image, video, audio, document) to a WhatsApp DM.
- [ ] Manual (Phase B+): confirm video and audio play; document
      surfaces with the right filename.
- [ ] Manual: send a WhatsApp group message and confirm the rendered
      user prefix shows the sender's name (existing behaviour — guard
      against regressing the inbound metadata path during the split).
- [ ] Telegram channel untouched: re-run a Telegram DM smoke test.

## Notes on what we're not doing, and why

- **No WhatsApp-specific markdown layer.** The model emits
  Markdown-flavoured text (`**bold**`, `[label](url)`, `# Heading`).
  WhatsApp's flavour is different (`*bold*`, no inline links, no
  headings) and partially incompatible (asterisk pairs collide).
  Workspace docs say nothing about it. Translating Markdown →
  WhatsApp markup well is more work than this spec is worth, and
  doing it badly is worse than the current "raw passthrough" which
  at least produces predictable text.
- **No Phase 2c-style typing dedupe / refresh.** WhatsApp's "typing…"
  indicator self-expires server-side; the current one-shot
  `notify_typing` ping is sufficient for typical reply latencies. Add
  a refresher if a future workload routinely takes >10 seconds and
  the bubble's disappearance becomes noticeable.
- **No reconnect work.** The current `while True` + 5s sleep around
  `websockets.connect` is fine. The bridge handles its own
  reconnection to WhatsApp Web internally.
