"""Seedr response normalisation and magnet-name matching (pure parts)."""
from __future__ import annotations

from bot.services.seedr import SeedrClient, _name_key


def test_normalise_flat_listing():
    listing = SeedrClient._normalise({
        "space_used": 100, "space_max": 1000,
        "folders": [{"id": "7", "name": "Show S01", "size": "500"}],
        "files": [{"folder_file_id": 42, "name": "ep.mkv", "size": 99}],
        "torrents": [{"id": 3, "name": "Show S01", "progress": "42.5%",
                      "size": 123}],
    })
    assert listing.folders[0].id == 7
    assert listing.files[0].id == 42
    assert listing.torrents[0].progress == 42.5
    assert listing.space_max == 1000


def test_normalise_nested_result():
    listing = SeedrClient._normalise({
        "result": {"folders": [{"id": 1, "name": "X", "size": 0}],
                   "files": []},
    })
    assert len(listing.folders) == 1


def test_normalise_skips_broken_rows():
    listing = SeedrClient._normalise({
        "folders": [{"name": "no id"}, {"id": "notanumber", "name": "x"}],
        "files": [{"name": "no id at all"}],
        "torrents": [{"id": None, "name": "t", "progress": "garbage"}],
    })
    assert listing.folders == []
    assert listing.files == []
    assert listing.torrents[0].progress == 0.0


def test_name_key_normalisation():
    assert _name_key("Show.Name S01 [1080p]") == _name_key("show name-s01 1080P")
    assert _name_key("") == ""


def test_proxy_stored_and_optional():
    c1 = SeedrClient("u", "p", proxy="http://user:pass@host:6540")
    assert c1._proxy == "http://user:pass@host:6540"
    c2 = SeedrClient("u", "p")
    assert c2._proxy is None       # aiohttp expects None, not ""
