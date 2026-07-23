"""Per-user settings persistence."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.database.connection import Database
from bot.services.user_settings import (
    get_user_settings, next_upload_mode, update_user_setting,
)


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "settings.db")
    await database.connect()
    yield database
    await database.close()


async def test_defaults_empty(db: Database):
    assert await get_user_settings(db, 42) == {}


async def test_update_and_roundtrip(db: Database):
    await update_user_setting(db, 42, "panel_style", "B")
    settings = await update_user_setting(db, 42, "upload_as", "document")
    assert settings == {"panel_style": "B", "upload_as": "document"}
    assert await get_user_settings(db, 42) == settings
    # other users unaffected
    assert await get_user_settings(db, 43) == {}


def test_upload_mode_cycle():
    assert next_upload_mode("auto") == "video"
    assert next_upload_mode("video") == "document"
    assert next_upload_mode("document") == "auto"
    assert next_upload_mode("junk") == "video"
