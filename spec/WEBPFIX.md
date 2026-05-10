# WebP outbound fix for WhatsApp

## Problem

Calls to `send_media` with a WebP path (e.g. `cuteness/8c2aa51482aa7e23.webp`
from the `cute-db` shared root) were silently failing to deliver. Symptoms:

- LLM tool call succeeds: `send_media` resolves the path, calls
  `bus.publish_outbound`, and returns `{"status": "sent", ...}`.
- The bridge's WebSocket command handler succeeds without error and emits
  `{"type":"sent"}` back to Python.
- Nothing arrives in the WhatsApp chat. The bridge log only shows inbound
  user messages (`messages.upsert: type=notify ... kind=extendedTextMessage`)
  and never an outbound `imageMessage`.

### Root cause

The Node bridge dispatched WebP through Baileys' `image:` field:

```ts
// bridge/src/whatsapp.ts (sendMessage)
if (payload.imageBase64) {
  await this.sock.sendMessage(to, {
    image: Buffer.from(payload.imageBase64, 'base64'),
    mimetype: payload.imageMimeType || 'image/jpeg',
    caption,
  });
}
```

WhatsApp/Baileys does not reliably render WebP through `image:` — that field
expects JPEG/PNG, and WebP belongs in `sticker:`. Baileys accepted the
upload (no exception, no error event), but the resulting message either was
dropped server-side or rendered as nothing at the recipient. From Python's
point of view the send "succeeded"; from the user's point of view the media
never arrived.

The cute-db shared root is full of WebP files (it caches WhatsApp sticker
assets), so every cute-picture send was hitting this path.

## Solution

Transcode `image/webp` to JPEG (opaque) or PNG (alpha) at the Python
outbound layer, before base64-encoding for the bridge. The bridge protocol
does not change; the WebP just never reaches it.

### Why transcode (not route to `sticker:`)

- The cute-db files are not all 512×512 — many are non-square WebPs (the
  failing example was 468×667). Sending those as stickers would render
  letterboxed at best, and WhatsApp may reject them outright.
- Stickers do not carry captions. `send_media` is built around the contract
  that `caption` is the user-visible reply; routing to `sticker:` would
  silently drop the caption or force a follow-up text bubble.
- The rest of the pipeline (image_block, telegram outbound, etc.) already
  treats these files as plain images. Transcoding keeps that invariant.

### Where the change lives

`benchclaw/channels/whatsapp/outbound.py` — `_dispatch_media` reads the file
bytes, and if `mime == "image/webp"` it calls `_transcode_webp(raw)` to get
back `(bytes, mime)` for either JPEG or PNG. Everything downstream
(base64 encode, payload assembly, bridge) is unchanged.

```python
def _transcode_webp(raw: bytes) -> tuple[bytes, str]:
    img = Image.open(BytesIO(raw))
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    out = BytesIO()
    if has_alpha:
        img.convert("RGBA").save(out, format="PNG")
        return out.getvalue(), "image/png"
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue(), "image/jpeg"
```

JPEG quality is 90 — high enough that the LLM-curated cute pictures don't
visibly degrade, low enough to keep payloads well under WhatsApp's media
size limits.

### Dependency

Adds `pillow>=10.0.0` to `pyproject.toml`. Pillow's WebP decoder handles
both static and animated WebP; for animated input it returns the first
frame, which is acceptable degradation (the alternative is a broken
delivery, which is what we have today).

### Tests

`tests/test_media_tools.py` adds two cases:

- `test_whatsapp_send_transcodes_webp_to_jpeg` — opaque WebP source,
  asserts the bridge payload has `imageMimeType == "image/jpeg"` and the
  decoded base64 starts with the JPEG SOI marker (`FF D8 FF`).
- `test_whatsapp_send_transcodes_webp_with_alpha_to_png` — RGBA WebP
  source, asserts `image/png` and the PNG signature (`89 50 4E 47 ...`).

Full suite: 241 passed.

## Out of scope

- Telegram outbound. Telegram accepts WebP fine; no change needed there.
- Animated WebP → animated GIF/MP4 conversion. Current behavior captures
  the first frame; revisit only if users complain about animated stickers
  losing motion.
- Bridge-side `sticker:` routing. Could be added later as an explicit
  `send_sticker` tool if we want native sticker UX, but it's a separate
  feature, not a fix.
