"""Encoding presets.

Built-ins are seeded into the `presets` table on startup (only when absent),
so the owner can edit them in the database — they are defaults, not
hard-coded behaviour. Each preset is a partial EncodePlan option dict; wizard
selections override preset values.
"""
from __future__ import annotations

import json
from typing import Any

from bot.database.connection import Database

BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "360p_small": {
        "label": "360p small size", "resolution": 360, "video_codec": "libx264",
        "crf": 28, "preset": "fast", "audio_bitrate_kbps": 96, "container": "mp4",
    },
    "480p_balanced": {
        "label": "480p balanced", "resolution": 480, "video_codec": "libx264",
        "crf": 24, "preset": "medium", "audio_bitrate_kbps": 128, "container": "mp4",
    },
    "720p_balanced": {
        "label": "720p balanced", "resolution": 720, "video_codec": "libx264",
        "crf": 23, "preset": "medium", "audio_bitrate_kbps": 128, "container": "mp4",
    },
    "1080p_hevc": {
        "label": "1080p HEVC", "resolution": 1080, "video_codec": "libx265",
        "crf": 24, "preset": "medium", "audio_bitrate_kbps": 160, "container": "mkv",
    },
    "1440p_hevc": {
        "label": "1440p HEVC", "resolution": 1440, "video_codec": "libx265",
        "crf": 22, "preset": "medium", "audio_bitrate_kbps": 160, "container": "mkv",
    },
    "2160p_hevc": {
        "label": "2160p HEVC", "resolution": 2160, "video_codec": "libx265",
        "crf": 20, "preset": "medium", "audio_bitrate_kbps": 192, "container": "mkv",
    },
    "tg_mp4": {
        "label": "Telegram-compatible MP4", "resolution": 0,
        "video_codec": "libx264", "crf": 23, "preset": "medium",
        "audio_codec": "aac", "audio_bitrate_kbps": 128, "container": "mp4",
        "pix_fmt": "yuv420p", "faststart": True,
    },
    "anime_hardsub": {
        "label": "Anime hardsub", "resolution": 1080, "video_codec": "libx264",
        "crf": 21, "preset": "slow", "audio_bitrate_kbps": 160,
        "container": "mp4", "subtitle_mode": "hard",
    },
    "archive_hq": {
        "label": "High-quality archive", "resolution": 0,
        "video_codec": "libx265", "crf": 18, "preset": "slow",
        "audio_codec": "copy", "container": "mkv",
    },
    "fast": {
        "label": "Fast encoding", "resolution": 0, "video_codec": "libx264",
        "crf": 25, "preset": "veryfast", "audio_bitrate_kbps": 128,
        "container": "mp4",
    },
    "small": {
        "label": "Small file size", "resolution": 480, "video_codec": "libx265",
        "crf": 28, "preset": "medium", "audio_bitrate_kbps": 80,
        "container": "mp4",
    },
}


async def seed_presets(db: Database) -> None:
    async with db.transaction() as conn:
        for name, config in BUILTIN_PRESETS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO presets (name, config, builtin) VALUES (?, ?, 1)",
                (name, json.dumps(config)),
            )


async def load_presets(db: Database) -> dict[str, dict[str, Any]]:
    rows = await db.fetchall("SELECT name, config FROM presets ORDER BY builtin DESC, name")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            out[row["name"]] = json.loads(row["config"])
        except json.JSONDecodeError:
            continue
    return out
