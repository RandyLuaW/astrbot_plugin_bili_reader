# Let the model watch the video itself, instead of reading someone else's summary

> AstrBot plugin · feeds raw Bilibili metadata, hot comments and keyframes into the model's own context

## The problem

A typical "video understanding" plugin does: download → extract frames → send to a vision model → get a summary → feed the summary to the chat model.

That's one extra LLM call, and one extra layer of **someone else's judgement**. The chat model no longer sees the video — it sees another model's opinion of the video. Tone, details and nuance are already filtered out.

This plugin only gathers raw material:

- Title, uploader, duration, view/danmaku/comment counts, description (truncated)
- Top comments by likes
- Keyframes sampled **evenly over time** (9 max by default)
- Everything goes into the model's context as-is

Whether it's worth talking about, and what to say, is the model's call. No scoring, no memory writes, no video reposting.

## Features

| Tool | What it does |
|---|---|
| `bili_read` | Read one video: text material + keyframe images |
| `bili_replies` | See who replied to her and what they said (read-only) |
| `bili_sticker` | Pick an image from the local archive, saved as a file for commenting |
| Reply wake-up | Pokes her when someone replies (delivers a note, never auto-replies) |

**Zero external tooling**: Bilibili public APIs + local Pillow cropping. No yt-dlp, no ffmpeg, no separate vision model.

## Install

### Option 1: Plugin marketplace (recommended)

Search "B站看一眼" in the AstrBot WebUI plugin marketplace.

### Option 2: Download from Releases

Grab the zip from [Releases](../../releases), then WebUI → Plugins → Upload.

### Option 3: Clone manually

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_bili_reader.git
```

Requires `Pillow` (bundled with AstrBot). Restart or hot-reload the plugin afterwards.

## Configuration

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch |
| `admin_only` | bool | `true` | Only usable in admin private chat |
| `sessdata` | string | empty | Your Bilibili cookie; when empty it borrows `bilibili_ai_bot`'s |
| `daily_limit` | int | `12` | Max videos per day (each costs thousands of tokens) |
| `max_frames` | int | `9` | Max keyframes per call (480×270 each, ~300 tokens) |
| `max_comments` | int | `20` | Max hot comments |
| `desc_chars` | int | `300` | Description truncation length |
| `notify_enabled` | bool | `true` | Wake her up when someone replies |
| `notify_umo` | string | empty | Target session for wake-ups |
| `notify_poll_minutes` | int | `15` | Reply polling interval |
| `notify_gap_minutes` | int | `60` | Minimum gap between wake-ups |
| `notify_daily_wakes` | int | `8` | Max wake-ups per day |

> Guest mode returns only 3 hot comments; with a login cookie you get ~20. When `sessdata` is empty the plugin reads `data/config/astrbot_plugin_bilibili_ai_bot_config.json` and reuses that cookie.

## LLM tools

### `bili_read`

```
bili_read(bvid: string, frames: number = 6, comments: number = 15)
```

`bvid` accepts a BV id, a full URL, or a `b23.tv` short link. Returns text plus `ImageContent` entries inside a `CallToolResult`; the framework caches the images into the context.

### `bili_replies`

```
bili_replies(limit: number = 0)
```

Read-only. Replying is up to the model — pass `target` / `root` / `parent` to `bili_action(action=comment)` (requires `astrbot_plugin_bilibili_ai_bot`).

### `bili_sticker`

```
bili_sticker(keyword: string = "", page: number = 1, num: string = "")
```

Looks up an image in the local archive and returns a local path for image comments.

## How it works

```
bili_read(bvid)
   ├─ x/web-interface/view        title / desc / stats / cid / aid
   ├─ x/player/videoshot          one request → full-video storyboard grid
   │     └─ Pillow crop → evenly sampled N frames → JPEG
   └─ x/v2/reply                  top comments by likes
        ↓
   TextContent + ImageContent[]  → model context
```

Keyframes come from Bilibili's `videoshot` endpoint: one request returns the whole-film storyboard (measured: 63 frames, 350KB, 480×270 each). Frames are cropped locally — **the video is never downloaded**. A typical call takes ~0.7s.

## Notes

- **Admin private chat only** by default.
- Each call costs thousands of tokens; `daily_limit` is a hard guard.
- No memory writes, no video reposting, no likes/comments — the model does that itself via `bili_action`.
- Danmaku endpoints are encrypted now (`dm/list.so` returns binary for any cookie), so only danmaku *counts* are used.
- Depends on public Bilibili endpoints; field changes can break parsing. Types are defensively handled (e.g. `videos` is an int = page count, not a list).

## License

MIT
