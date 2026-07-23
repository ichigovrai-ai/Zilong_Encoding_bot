"""Embedded-subtitle selection for Seedr hardsub (embedded_auto)."""
from __future__ import annotations

from bot.jobs.worker import _pick_embedded_sub
from bot.services.ffprobe import MediaInfo, SubtitleStream


def _sub(idx, lang, codec="ass", default=False, bitmap_codec=None):
    return SubtitleStream(
        index=idx, type_index=idx, codec=bitmap_codec or codec,
        language=lang, title="", default=default, forced=False,
    )


def _media(subs):
    return MediaInfo(path="x.mkv", container="matroska", duration=1400,
                     size=1, bitrate=1, subtitles=subs)


def test_prefers_configured_language():
    m = _media([_sub(0, "eng", default=True), _sub(1, "fre"), _sub(2, "spa")])
    picked = _pick_embedded_sub(m, "fr")
    assert picked.language == "fre"          # fr matches fre, over the default


def test_language_prefix_matches_iso_variants():
    # "fr" should match "fra" too
    m = _media([_sub(0, "eng"), _sub(1, "fra")])
    assert _pick_embedded_sub(m, "fr").language == "fra"


def test_falls_back_to_default_track():
    m = _media([_sub(0, "eng"), _sub(1, "spa", default=True)])
    assert _pick_embedded_sub(m, "").language == "spa"


def test_falls_back_to_first_when_no_default():
    m = _media([_sub(0, "eng"), _sub(1, "spa")])
    assert _pick_embedded_sub(m, "").type_index == 0


def test_ignores_bitmap_subs():
    m = _media([_sub(0, "eng", bitmap_codec="hdmv_pgs_subtitle")])
    assert _pick_embedded_sub(m, "") is None


def test_none_when_no_subs():
    assert _pick_embedded_sub(_media([]), "fr") is None


def test_preferred_language_missing_falls_back():
    m = _media([_sub(0, "eng", default=True), _sub(1, "spa")])
    # want French, none present → default track
    assert _pick_embedded_sub(m, "fr").language == "eng"
