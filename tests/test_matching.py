"""Episode extraction and video↔subtitle matching."""
from __future__ import annotations

from bot.utils.matching import (
    MatchConfidence, extract_episode, match_subtitles, normalize_name,
    score_pair,
)


def test_normalize_strips_tags():
    n = normalize_name("[Erai-raws] Show Name - 04 [1080p][HEVC][ABCD1234].mkv")
    assert "erai" not in n and "1080p" not in n and "abcd1234" not in n
    assert "show name" in n


def test_normalize_case_and_punctuation():
    assert normalize_name("Show.Name_S01E02.mkv") == normalize_name(
        "show name s01e02.ass"
    )


def test_extract_episode_patterns():
    assert extract_episode("Show S01E05.mkv").episode == 5
    assert extract_episode("Show S01E05.mkv").season == 1
    assert extract_episode("Show 2x07.mkv").episode == 7
    assert extract_episode("Show - Episode 12.mkv").episode == 12
    assert extract_episode("Show Ep 3.mkv").episode == 3
    assert extract_episode("[Group] Show - 08 [720p].mkv").episode == 8
    assert extract_episode("Show - 08v2.mkv").episode == 8
    assert extract_episode("Movie Title.mkv") is None


def test_exact_match():
    assert score_pair("Episode 01.mkv", "Episode 01.ass") == 1.0


def test_episode_mismatch_disqualifies():
    assert score_pair("Show - 01.mkv", "Show - 02.ass") < 0.45


def test_match_multi_episode_folder():
    videos = ["Series/Episode 01.mkv", "Series/Episode 02.mkv",
              "Series/Episode 03.mkv"]
    subs = ["Series/Episode 01.ass", "Series/Episode 02.ass",
            "Series/Episode 03.txt"]
    results = match_subtitles(videos, subs)
    assert len(results) == 3
    by_video = {r.video: r for r in results}
    assert by_video["Series/Episode 01.mkv"].subtitle == "Series/Episode 01.ass"
    assert by_video["Series/Episode 02.mkv"].subtitle == "Series/Episode 02.ass"
    assert by_video["Series/Episode 03.mkv"].subtitle == "Series/Episode 03.txt"
    assert all(r.confidence == MatchConfidence.EXACT for r in results)


def test_match_release_group_naming():
    videos = ["[SubsPlease] Kaiju No. 8 - 03 (1080p) [ABCD1234].mkv"]
    subs = ["Kaiju_No_8_-_03.fr.ass"]
    r = match_subtitles(videos, subs)[0]
    assert r.subtitle == subs[0]
    assert r.confidence in (MatchConfidence.HIGH, MatchConfidence.EXACT)


def test_no_silent_pairing_when_unrelated():
    r = match_subtitles(["Some Movie.mkv"], ["Totally Different Show 05.srt"])[0]
    assert r.subtitle is None or r.confidence in (
        MatchConfidence.UNCERTAIN, MatchConfidence.NONE
    )


def test_subtitle_not_reused():
    videos = ["Show - 01.mkv", "Show - 02.mkv"]
    subs = ["Show - 01.ass"]
    results = match_subtitles(videos, subs)
    assigned = [r.subtitle for r in results if r.subtitle]
    assert assigned.count("Show - 01.ass") == 1
