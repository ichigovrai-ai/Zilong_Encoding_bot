# Phase 1 — Audit of `vicMenma/Zilong_multiusage`

Audited from a fresh clone (2026-07-23). ~130 files; plugins up to 92 KB each.

## What the existing bot actually does

- Entry: `main.py` builds a PyroFork (`pyrogram` namespace) client with
  `plugins={"root": "plugins"}`, uvloop, PID-file single-instance guard.
- Hardsubbing is **cloud-based**: CloudConvert (`services/cloudconvert_api.py`,
  `plugins/cc_seedr.py`) and FreeConvert (`services/freeconvert_api.py`,
  `plugins/fc_seedr.py`) run the actual burn-in. Local FFmpeg
  (`services/ffmpeg.py`, 82 KB) is used for probing, thumbnails, trims and
  some encodes, but the Seedr hardsub path submits Seedr CDN URLs to
  CloudConvert (`import/url` → `command/ffmpeg` → `export/url`).
- Seedr access (`services/seedr.py`, v6) uses **two strategies**: the
  `seedrcc` library and raw `resource.php` calls over httpx, with an OAuth
  password grant against `https://www.seedr.cc/oauth_test/token`
  (`client_id=seedr_xbmc`). Notable funcs: `add_torrent(torrent_magnet=…)`,
  `list_contents(folder_id=…)`, `fetch_file(folder_file_id=…)`, `rename`.
- Torrent completion detection: poll the root listing; the torrent appears in
  `torrents[]` with a progress %, and on completion a new **folder**
  materialises. Folder↔torrent attribution is by normalised-name match only
  (accounts are shared, so "the only new folder" is not proof). A 98-99 %
  stuck-meter heuristic accepts a folder once its content signature is stable
  for 3 polls or its size reaches 99.5 % of the torrent size.
- Subtitle language logic is **hard-coded to French** (`subtitle_detect.py`).
- Progress UX: `services/utils.py` has `safe_edit` (FloodWait-aware),
  `PanelUpdater` rate limiting, several panel styles.

## Reusable knowledge (adapted into the new bot, not copied verbatim)

| Source | What is reused | Where it lands |
|---|---|---|
| `services/seedr.py` | OAuth endpoints/params, `resource.php` funcs, response normalisation (`files/folders/torrents` with embedded URLs), storage-key fallbacks, poll heuristics (name attribution, 98 % stuck meter, stable content signature), `_clean_seedr_name` | `bot/services/seedr.py` (clean aiohttp client) |
| `services/ffmpeg.py` | Hardened ffprobe invocation (`-analyzeduration/-probesize`, lighter limits + browser UA for URLs, hard process timeouts), HDR-aware thumbnail chain | `bot/services/ffprobe.py`, `bot/services/thumbnail.py` |
| `services/utils.py` | FloodWait-safe message editing, rate-limited progress panel policy, `human_size`/`fmt_hms`, `smart_clean_filename` ideas | `bot/utils/progress.py`, `bot/utils/timefmt.py`, `bot/utils/filenames.py` |
| `services/subtitle_detect.py` | text-vs-bitmap subtitle codec vocabulary | `bot/services/subtitles.py` (generalised, language-agnostic) |
| `main.py` | FloodWait retry on auth, `max_concurrent_transmissions` signature probe | `bot/app.py` |
| `requirements.txt` | pyrofork/TgCrypto/aiohttp pins known to work together | `requirements.txt` |

## Why the rest is not reused

1. **CloudConvert/FreeConvert-centric hardsub** — the new bot's requirement is
   local FFmpeg as the primary engine; the whole CC/FC job-store, webhook
   server, tunnel management, and engine-router stack (≈250 KB) is dead weight
   here. CloudConvert stays out entirely for now (optional future fallback).
2. **Monolithic plugins** — `url_handler.py` (92 KB), `seedr_hardsub.py`
   (73 KB), `resize.py` (58 KB) mix Telegram UI, FFmpeg command text, Seedr
   calls and state in single files. The new spec mandates separation.
3. **In-memory global state** — `_WAITING_SUB: dict[int, dict]` with manual
   TTL eviction; jobs do not survive restart. Replaced by SQLite-backed jobs.
4. **Runtime monkey-patches** — `cc_house_style_patch`, `multiuser_isolation`,
   `zz_*` patch modules rewrite other modules at import time. Fragile;
   replaced by explicit configuration and DI.
5. **Dual HTTP stacks** — httpx + seedrcc + aiohttp + cloudscraper. The new
   bot standardises on aiohttp (required stack) with one Seedr client.
6. **Hard-coded Linux paths** (`/tmp/zilong_bot.pid`, `/tmp/zilong_dl`) and
   French-only subtitle selection.
7. **Polling loops spread across plugins** — `seedr_monitor`, `fc_seedr`,
   `seedr_hardsub` each poll independently. One `SeedrClient.wait_ready()`
   used by the job worker replaces them.

## New architecture (implemented)

```
bot/
  app.py  config.py  logging_config.py  __main__.py
  handlers/   start, media, url_input, magnet, seedr_cmd, callbacks, queue_cmds
  services/   ffprobe, ffmpeg (builder+runner), encoding, subtitles, fonts,
              seedr, download, upload, mediainfo, thumbnail, cleanup
  jobs/       states, models, repository, manager, worker
  ui/         callbacks (compact codec), keyboards, messages
  database/   connection, migrations
  utils/      filenames, matching, validation, progress, subprocesses, timefmt
tests/
```

- **Persistence**: SQLite (aiosqlite), WAL mode; jobs + user settings +
  presets + job events. Queue recovers on restart.
- **State machine**: `created → queued → downloading → probing →
  preparing_subtitles → encoding → uploading → completed | failed | cancelled`,
  with `waiting_for_input` for wizard steps and `cleaning` transitional state.
- **Callback data**: `v1|<verb>|<job36>|<arg>` — short codes, never a full
  config; all wizard state lives in the job row (JSON `options` column).
- **Concurrency**: asyncio semaphores per stage (download/encode/upload),
  encode default 1.
- **FFmpeg**: argument-list builder + `asyncio.create_subprocess_exec`,
  `-progress pipe:1` parsing, no `shell=True` anywhere.
