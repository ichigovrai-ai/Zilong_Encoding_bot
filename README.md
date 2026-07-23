# Zilong Encoding Bot

A Telegram bot focused on **video encoding, resolution conversion,
compression, subtitle processing and automated Seedr hardsubbing**.
Built with **PyroFork** (not Pyrogram), local **FFmpeg** as the only encoding
engine, asyncio throughout, and a **SQLite-backed persistent job queue** that
survives restarts.

```
Receive video, URL, or magnet
        ↓
Analyse the media (ffprobe)
        ↓
Choose resolution / codec / quality via inline menus
        ↓
Optionally select embedded or uploaded subtitles
        ↓
Encode, compress, convert, or hardsub with FFmpeg
        ↓
Upload the result to Telegram
```

Magnet links additionally run the full **Seedr pipeline**: add magnet →
async polling → recursive folder browse → episode detection →
video↔subtitle matching (with user confirmation) → selective download →
hardsub → upload → cleanup.

---

## Requirements

- Python **3.12**
- **FFmpeg** and **ffprobe** with `libx264`, `libx265`, `aac` and the
  `subtitles` filter (**libass**) — hardsubbing does not work without libass.
  `libsvtav1` and `libopus` are optional (AV1/Opus menus appear only when
  available).
- A Telegram bot token, API ID/hash
- A [Seedr.cc](https://www.seedr.cc) account (only for magnet links)

Verify your FFmpeg build:

```bash
ffmpeg -version
ffmpeg -filters   | grep subtitles     # must list the subtitles filter
ffmpeg -encoders  | grep -E "libx264|libx265|libsvtav1|aac|libopus"
ffmpeg -buildconf | grep -E "libass|libx26"
```

## Installation (Ubuntu 22.04 / 24.04)

```bash
sudo apt update && sudo apt install -y ffmpeg python3.12 python3.12-venv
git clone <this repo> encoding-bot && cd encoding-bot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
python -m bot
```

### Docker

```bash
cp .env.example .env        # edit it
docker compose up -d --build
```

The compose file mounts `data/` (SQLite DB + Telegram session) and `work/`
(job scratch space) as volumes and gives the container a 30 s
`stop_grace_period` so SIGTERM shuts jobs down cleanly.

### Local development (Windows/macOS/Linux)

```bash
python -m venv .venv && . .venv/bin/activate   # .venv\Scripts\activate on Win
pip install -r requirements-dev.txt
pytest                                          # 126 unit tests, no network
python -m bot
```

## Configuration

Everything is environment variables (see [.env.example](.env.example)).
Required: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`. Startup validates
the whole configuration at once and prints every problem.

Key optionals:

| Variable | Default | Meaning |
|---|---|---|
| `SEEDR_USERNAME` / `SEEDR_PASSWORD` | — | enables magnet workflow |
| `MAX_CONCURRENT_ENCODES` | 1 | keep at 1 on small VPSes |
| `MAX_INPUT_SIZE_MB` / `MAX_OUTPUT_SIZE_MB` | 4096 / 2000 | hard limits |
| `AUTHORIZED_USERS` | empty = public | allowlist of user ids |
| `OWNER_ONLY` | 0 | lock the bot to the owner |
| `SEEDR_AUTO_DELETE` | 1 | delete Seedr folder when episodes finish |

Secrets are never logged — a redaction filter masks the bot token, API hash,
Seedr password and signed Seedr URLs in every log record.

## Commands

```
/start /help          — intro and full help
/encode               — encode/resize (reply to a video, or just send one)
/compress             — compression profiles / target file size
/convert              — container/codec/resolution/audio conversion
/hardsub              — burn subtitles into a video
/seedr                — Seedr account, storage, transfers, folders
/mediainfo            — full stream report
/extract /extractsub /extractaudio /convertsub
/presets /settings    — presets and current defaults
/queue /status        — job queue and bot status
/cancel [job id]      — cancel a job safely at any stage
/history              — your recent jobs
```

Sending a **video**, **direct http(s) link**, **magnet link** or **.torrent
file** with no command opens the action menu automatically.

## Architecture

```
bot/
  app.py  config.py  logging_config.py  runtime.py
  handlers/    Telegram-facing: intake, wizard state machine, callbacks,
               commands (no FFmpeg/Seedr logic here)
  services/    ffprobe, ffmpeg (arg builder + progress runner), planner,
               subtitles, fonts, extraction, seedr, download, upload,
               mediainfo, thumbnail, presets, cleanup
  jobs/        states (validated transitions), models, repository (SQLite),
               manager (queue/semaphores/recovery), worker (pipeline)
  ui/          compact callback codec, keyboards, message templates
  database/    aiosqlite connection + versioned migrations
  utils/       filenames, matching, validation, torrent, paths,
               subprocesses, progress, timefmt
tests/         126 unit tests (pure logic; no network, no big files)
```

Design points:

- **State machine**: `created → queued → downloading → probing →
  preparing_subtitles → encoding → uploading → completed/failed/cancelled`,
  every transition validated and recorded in `job_events`.
- **Callback data** is only `z|verb|jobid|arg` (≤64 bytes); all wizard state
  lives in the job row.
- **FFmpeg safety**: single command builder, argument lists +
  `create_subprocess_exec`, never `shell=True`; FFmpeg runs with
  `cwd=<job dir>` and controlled relative filenames (`input/input.mkv`,
  `subtitles/sub.ass`, `fonts/`) so Windows paths, apostrophes and Unicode
  names cannot break filter strings.
- **Subtitles**: format detected from **content**, not extension. `.txt`
  files containing SRT/ASS/VTT/MicroDVD/LRC are converted; plain untimed
  text is rejected with a clear message — timings are never invented.
  ASS styles/fonts are preserved (MKV font attachments are extracted and
  passed via `fontsdir`). Subtitles render **after** scaling (crisp text at
  output resolution; libass maps PlayRes coordinates, so positioning is
  preserved). Bitmap subtitles (PGS/VobSub) are reported as unsupported.
- **Scaling**: the "p" target applies to the shorter side, so portrait and
  square videos behave correctly; dimensions are always even; upscaling
  requires explicit confirmation.
- **Restart safety**: queued jobs re-run after a restart; jobs that were
  mid-download/encode are marked failed with a clear message and their
  directories cleaned; wizard sessions expire after 4 h.

## Seedr magnet workflow

1. Magnet is validated (info-hash) and added via Seedr's OAuth API
   (`seedr_xbmc` password grant — works for free accounts).
2. The bot polls the transfer list asynchronously; folder attribution is by
   normalised name (never "the only new folder"), with a stuck-at-98%
   heuristic that accepts a folder whose contents are stable.
3. The finished folder is walked recursively; videos
   (`.mkv .mp4 .webm .mov .avi .ts .m2ts`) and subtitles
   (`.ass .ssa .srt .vtt .sub .txt`) are matched per episode using
   normalised names, episode-number extraction and similarity scoring.
   Uncertain matches are flagged ⚠️ and must be fixed manually.
4. You pick episodes, then resolution/codec/quality once — each episode
   becomes its own queued job (sequential by default with
   `MAX_CONCURRENT_ENCODES=1`).
5. Only the needed files are downloaded from Seedr; after the last episode
   finishes the Seedr folder is deleted (if `SEEDR_AUTO_DELETE=1`).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "FFmpeg has no subtitles filter" at startup | FFmpeg built without libass — install a full build (`apt install ffmpeg`, gyan.dev on Windows) |
| Magnet rejected with "wishlist" | Free Seedr account already has an active transfer — wait or upgrade |
| "Seedr login failed" | Wrong `SEEDR_USERNAME`/`SEEDR_PASSWORD`; note Seedr blocks some datacenter IPs for add_torrent |
| Encode fails with "impossible target" | Target size too small for the duration — the error names the minimum |
| Bitmap subtitle can't be selected | PGS/VobSub needs OCR, which is not implemented |
| Job says "interrupted by restart" | The bot restarted mid-encode; resubmit — inputs are not kept after failure |

## Known limitations

- No OCR for bitmap subtitles (PGS/VobSub).
- "Attempt automatic alignment" for untimed TXT is not implemented — the
  option is presented as unavailable by design rather than faking timings.
- Seedr free accounts allow one active transfer; the bot surfaces Seedr's
  wishlist/quota errors instead of queueing around them.
- Interrupted (mid-encode) jobs are failed on restart, not resumed.
- CloudConvert/FreeConvert are not used at all; encoding is 100 % local.

## Reused from Zilong_multiusage

The Seedr protocol details (OAuth endpoints, resource.php calls, response
normalisation, completion heuristics), the hardened ffprobe invocation
limits, FloodWait-tolerant progress editing and the release-name cleaning
rules were carried over from
[vicMenma/Zilong_multiusage](https://github.com/vicMenma/Zilong_multiusage)
and reimplemented cleanly on aiohttp. See [docs/AUDIT.md](docs/AUDIT.md) for
the full audit of what was reused and why the rest was not.
