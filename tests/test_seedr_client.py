"""Seedr response normalisation and magnet-name matching (pure parts)."""
from __future__ import annotations

import pytest

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


def test_proxy_credentials_split_for_aiohttp():
    # aiohttp ignores userinfo in the proxy URL, so it must be split into
    # a clean URL + BasicAuth or the proxy answers 407.
    c1 = SeedrClient("u", "p", proxy="http://user:pass@host:6540")
    assert c1._proxy == "http://host:6540"
    assert c1._proxy_auth is not None
    assert c1._proxy_auth.login == "user" and c1._proxy_auth.password == "pass"


def test_proxy_without_credentials():
    c = SeedrClient("u", "p", proxy="http://host:6540")
    assert c._proxy == "http://host:6540"
    assert c._proxy_auth is None


def test_no_proxy():
    c = SeedrClient("u", "p")
    assert c._proxy is None and c._proxy_auth is None   # aiohttp wants None


class _FakeResp:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    """Records the form data of each resource.php POST."""
    def __init__(self):
        self.calls: list[dict] = []

    def post(self, url, proxy=None, proxy_auth=None, data=None):
        self.calls.append(dict(data or {}))
        # OAuth token call vs resource.php call
        if "token" in url:
            return _FakeResp({"access_token": "tok"})
        return _FakeResp({"id": data.get("content_id") or data.get("folder_id"),
                          "files": [], "folders": []})


@pytest.mark.asyncio
async def test_list_folder_uses_content_id_for_subfolders():
    # Regression: Seedr ignores folder_id for subfolders — a subfolder MUST be
    # listed with content_type=folder + content_id, or it returns root and the
    # video inside a single-file torrent is never found.
    sess = _FakeSession()
    c = SeedrClient("u", "p", session=sess)
    await c.list_folder(0)
    await c.list_folder(1428982481)
    listing_calls = [c for c in sess.calls if c.get("func") == "list_contents"]
    root_call, sub_call = listing_calls[0], listing_calls[1]
    assert root_call.get("folder_id") == "0"
    assert sub_call.get("content_type") == "folder"
    assert sub_call.get("content_id") == "1428982481"
    assert "folder_id" not in sub_call
