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


def test_build_output_name_clean_hardsub():
    # The real-world messy names the user complained about.
    n = build_output_name(
        "[Erai-raws] Otome Kaijuu Carameliser - 03 [480p CR WEB-DL AVC AAC]"
        "[MultiSub][4A1B5F1C].mkv",
        container="mp4", hardsub=True, sub_lang="fre")
    assert n == "Otome_Kaijuu_Carameliser_S01E03_VOSTFR.mp4"

    n = build_output_name(
        "Mebius Dust S01E03 VOSTFR 720p WEB x264 AAC -Tsundere-Raws (CR).mkv",
        container="mp4", hardsub=True, sub_lang="fr")
    assert n == "Mebius_Dust_S01E03_VOSTFR.mp4"


def test_build_output_name_lang_tags():
    base = "Show S02E05.mkv"
    assert build_output_name(base, hardsub=True, sub_lang="en") \
        == "Show_S02E05_VOSTEN.mp4"
    assert build_output_name(base, hardsub=True, sub_lang="es") \
        == "Show_S02E05_VOSTES.mp4"


def test_build_output_name_no_subs_keeps_resolution_when_no_episode():
    # A movie/single file with no episode number keeps the resolution so
    # different renditions don't collide.
    n = build_output_name("Spirited Away.mkv", container="mkv", height=1080)
    assert n == "Spirited_Away_1080p.mkv"


def test_build_output_name_episode_only():
    assert build_output_name("Great Show - 4.mkv", container="mp4") \
        == "Great_Show_S01E04.mp4"


def test_unique_path(tmp_path: Path):
    p = tmp_path / "out.mp4"
    assert unique_path(p) == p
    p.write_bytes(b"x")
    assert unique_path(p).name == "out_2.mp4"
