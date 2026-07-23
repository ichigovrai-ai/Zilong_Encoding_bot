from __future__ import annotations

from pathlib import Path

from bot.utils.filenames import (
    build_output_name, clean_title, sanitize_filename, unique_path,
)


def test_sanitize_removes_unsafe():
    assert "/" not in sanitize_filename("a/b\\c:d*e?.mkv")
    assert sanitize_filename('a<b>|c".mkv') == "a_b__c_.mkv"


def test_sanitize_blocks_traversal():
    out = sanitize_filename("../../etc/passwd")
    assert ".." not in out and "/" not in out


def test_sanitize_reserved_names():
    assert sanitize_filename("CON.mkv") != "CON.mkv"


def test_sanitize_preserves_unicode():
    assert sanitize_filename("進撃の巨人 01.mkv") == "進撃の巨人 01.mkv"


def test_sanitize_length_limit():
    out = sanitize_filename("x" * 400 + ".mkv")
    assert len(out) <= 130


def test_clean_title_strips_release_clutter():
    t = clean_title("[Erai-raws] Great Show - 04 [1080p][HEVC][x265].mkv")
    assert "Erai" not in t and "1080p" not in t and "x265" not in t.lower()
    assert "Great Show" in t and "04" in t


def test_build_output_name_examples():
    assert build_output_name("Episode 01.mkv", height=360, codec="libx264",
                             container="mp4") == "Episode_01_360p_H264.mp4"
    assert build_output_name("Episode 01.mkv", height=720, codec="libx265",
                             container="mkv") == "Episode_01_720p_HEVC.mkv"
    name = build_output_name("Episode 01.mkv", height=1080, codec="libx264",
                             container="mp4", hardsub=True)
    assert name == "Episode_01_1080p_H264_HARDSUB.mp4"


def test_unique_path(tmp_path: Path):
    p = tmp_path / "out.mp4"
    assert unique_path(p) == p
    p.write_bytes(b"x")
    assert unique_path(p).name == "out_2.mp4"
