"""Per-user settings: schema, cycling, persistence."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.database.connection import Database
from bot.services.user_settings import (
    SETTINGS, button_label, cycle_setting, get_user_settings, is_setting,
    next_value, value_of,
)


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "settings.db")
    await database.connect()
    yield database
    await database.close()


def test_defaults_and_french_sub_lang():
    # Subtitle language defaults to French, as required.
    assert value_of({}, "sub_lang") == "fr"
    assert value_of({}, "panel_style") == "A"
    assert value_of({}, "seedr_hardsub") == "on"


def test_value_of_falls_back_on_garbage():
    assert value_of({"def_codec": "nonsense"}, "def_codec") == "x264"


def test_next_value_cycles_and_wraps():
    for s in SETTINGS:
        cur = value_of({}, s.key)
        seen = {cur}
        settings = {}
        for _ in range(len(s.choices)):
            nxt = next_value(settings, s.key)
            settings[s.key] = nxt
            seen.add(nxt)
        assert seen == set(s.choices)          # visits every choice
        assert settings[s.key] == cur          # wraps back to the start


def test_is_setting():
    assert is_setting("sub_lang")
    assert not is_setting("done")
    assert not is_setting("bogus")


def test_button_label_shows_value():
    assert "French" in button_label({}, "sub_lang")
    assert button_label({"def_codec": "x265"}, "def_codec").endswith("HEVC")


async def test_cycle_persists(db):
    s1 = await cycle_setting(db, 42, "def_codec")   # x264 -> x265
    assert s1["def_codec"] == "x265"
    assert (await get_user_settings(db, 42))["def_codec"] == "x265"
    # other user unaffected
    assert await get_user_settings(db, 99) == {}


async def test_cycle_sub_lang(db):
    s = await cycle_setting(db, 1, "sub_lang")      # fr -> en
    assert s["sub_lang"] == "en"
