"""Per-user preferences stored in the user_settings table (JSON blob).

Currently: panel_style (A/B/C emoji panel styles) and upload_as
(auto/video/document).
"""
from __future__ import annotations

import json
from typing import Any

from bot.database.connection import Database

UPLOAD_MODES: tuple[str, ...] = ("auto", "video", "document")
UPLOAD_MODE_LABELS: dict[str, str] = {
    "auto": "🤖 Auto", "video": "🎬 Video", "document": "📁 Document",
}


def next_upload_mode(mode: str) -> str:
    cur = mode if mode in UPLOAD_MODES else "auto"
    return UPLOAD_MODES[(UPLOAD_MODES.index(cur) + 1) % len(UPLOAD_MODES)]


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


async def update_user_setting(db: Database, user_id: int, key: str,
                              value: Any) -> dict[str, Any]:
    settings = await get_user_settings(db, user_id)
    settings[key] = value
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO user_settings (user_id, settings) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET settings = excluded.settings",
            (user_id, json.dumps(settings, ensure_ascii=False)),
        )
    return settings
