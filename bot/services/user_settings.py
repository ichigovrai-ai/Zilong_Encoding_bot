"""Per-user preferences (user_settings table, JSON blob).

Every option lives in the SETTINGS spec below — the /settings menu, the
cycle-on-tap logic and the places that read a value all share it, so the menu
and behaviour can never drift. Tapping a button advances that option to its
next choice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from bot.database.connection import Database


# ── Display helpers ──────────────────────────────────────────────────────

_PANEL = {"A": "Compact", "B": "Blocks", "C": "Accent"}
_UPLOAD = {"auto": "🤖 Auto", "video": "🎬 Video", "document": "📁 Document"}
_LANG = {
    "fr": "🇫🇷 French", "en": "🇬🇧 English", "es": "🇪🇸 Spanish",
    "pt": "🇵🇹 Portuguese", "ar": "🇸🇦 Arabic", "de": "🇩🇪 German",
    "it": "🇮🇹 Italian", "ja": "🇯🇵 Japanese", "off": "Default track",
}
_CODEC = {"x264": "H.264", "x265": "HEVC", "av1": "AV1"}
_AUDIO = {"aac": "AAC", "opus": "Opus", "copy": "Copy"}
_ONOFF = {"on": "✅ On", "off": "❌ Off"}


def _res(v: str) -> str:
    return "Original" if v == "original" else f"{v}p"


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    label: str                       # emoji + short name for the button
    choices: tuple[str, ...]
    default: str
    display: Callable[[str], str]


SETTINGS: tuple[Setting, ...] = (
    Setting("panel_style", "🎨 Panel", ("A", "B", "C"), "A", _PANEL.get),
    Setting("upload_as", "📤 Upload", ("auto", "video", "document"), "auto", _UPLOAD.get),
    Setting("sub_lang", "💬 Sub lang",
            ("fr", "en", "es", "pt", "ar", "de", "it", "ja", "off"), "fr", _LANG.get),
    Setting("seedr_hardsub", "🔥 Seedr hardsub", ("on", "off"), "on", _ONOFF.get),
    # Opt-in: send FreeConvert's temporary download link as soon as the cloud
    # encode is done, as a backup in case fetching/uploading the file fails.
    Setting("fc_link", "🔗 FC link", ("off", "on"), "off", _ONOFF.get),
    Setting("def_resolution", "🖥 Resolution",
            ("original", "360", "480", "720", "1080", "1440", "2160"),
            "original", _res),
    Setting("def_codec", "🎯 Codec", ("x264", "x265", "av1"), "x264", _CODEC.get),
    Setting("def_crf", "🎛 CRF", ("18", "20", "22", "23", "24", "26", "28"), "23", str),
    # Default veryfast: at a fixed CRF, faster presets keep the same visual
    # quality and just make slightly larger files, so this is ~3× faster than
    # medium with no real quality loss. Dial toward slow for smaller files.
    Setting("def_preset", "⏩ Preset",
            ("ultrafast", "superfast", "veryfast", "faster", "fast",
             "medium", "slow", "slower"),
            "veryfast", str),
    Setting("def_audio", "🔊 Audio", ("aac", "opus", "copy"), "aac", _AUDIO.get),
)

_BY_KEY = {s.key: s for s in SETTINGS}

# Back-compat for callers importing the old constants.
UPLOAD_MODE_LABELS = _UPLOAD


def value_of(settings: dict, key: str) -> str:
    spec = _BY_KEY[key]
    v = str(settings.get(key, spec.default))
    return v if v in spec.choices else spec.default


def next_value(settings: dict, key: str) -> str:
    spec = _BY_KEY[key]
    cur = value_of(settings, key)
    return spec.choices[(spec.choices.index(cur) + 1) % len(spec.choices)]


def button_label(settings: dict, key: str) -> str:
    spec = _BY_KEY[key]
    return f"{spec.label}: {spec.display(value_of(settings, key))}"


def is_setting(key: str) -> bool:
    return key in _BY_KEY


# ── Persistence ──────────────────────────────────────────────────────────

async def get_user_settings(db: Database, user_id: int) -> dict[str, Any]:
    row = await db.fetchone(
        "SELECT settings FROM user_settings WHERE user_id = ?", (user_id,)
    )
    if row is None:
        return {}
    try:
        data = json.loads(row["settings"] or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def cycle_setting(db: Database, user_id: int, key: str) -> dict[str, Any]:
    """Advance one setting to its next choice and persist. Returns all settings."""
    settings = await get_user_settings(db, user_id)
    settings[key] = next_value(settings, key)
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO user_settings (user_id, settings) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET settings = excluded.settings",
            (user_id, json.dumps(settings, ensure_ascii=False)),
        )
    return settings
