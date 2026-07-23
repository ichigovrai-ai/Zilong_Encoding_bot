"""Cleanup safety and .torrent → magnet conversion."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from bot.utils.paths import create_job_dirs, is_job_dir, remove_job_dir
from bot.utils.torrent import TorrentParseError, torrent_to_magnet


def test_create_and_recognise_job_dir(tmp_path: Path):
    work = tmp_path / "work"
    d = create_job_dirs(work, 12345, "abcd1234")
    assert d.exists() and (d / "input").exists() and (d / "fonts").exists()
    assert is_job_dir(work, d)


def test_refuses_to_delete_non_job_dirs(tmp_path: Path):
    work = tmp_path / "work"
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "file.txt").write_text("keep me")
    assert not remove_job_dir(work, victim)
    assert victim.exists()
    # user dir itself is not a job dir either
    user_dir = work / "12345"
    user_dir.mkdir(parents=True)
    assert not remove_job_dir(work, user_dir)
    assert user_dir.exists()


def test_removes_real_job_dir(tmp_path: Path):
    work = tmp_path / "work"
    d = create_job_dirs(work, 99, "zzzz9999")
    (d / "input" / "big.mkv").write_bytes(b"data")
    assert remove_job_dir(work, d)
    assert not d.exists()


def test_bad_job_id_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        create_job_dirs(tmp_path, 1, "../evil")


# ── Torrent parsing ──────────────────────────────────────────────────────

def _mini_torrent() -> bytes:
    info = b"d6:lengthi123e4:name8:test.mkv12:piece lengthi16384e6:pieces20:" \
           + b"\x00" * 20 + b"e"
    return b"d8:announce18:http://tracker/ann4:info" + info + b"e"


def test_torrent_to_magnet():
    data = _mini_torrent()
    magnet = torrent_to_magnet(data)
    assert magnet.startswith("magnet:?xt=urn:btih:")
    assert "dn=test.mkv" in magnet
    assert "tr=http" in magnet
    # info-hash is sha1 of the bencoded info dict
    info_start = data.index(b"4:info") + len(b"4:info")
    expected = hashlib.sha1(data[info_start:-1]).hexdigest()
    assert expected in magnet


def test_torrent_garbage_rejected():
    with pytest.raises(TorrentParseError):
        torrent_to_magnet(b"this is not bencode")
    with pytest.raises(TorrentParseError):
        torrent_to_magnet(b"d4:spam4:eggse")  # no info dict
