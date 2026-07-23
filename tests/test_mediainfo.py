"""MediaInfo rendering: CLI-report wrapping + truncation + HTML escaping."""
from __future__ import annotations

from bot.services.mediainfo import render_card, render_cli
from tests.conftest import make_media

SAMPLE = """General
Complete name : episode.mkv
Format        : Matroska
Duration      : 23 min 40 s

Video
Format        : AVC
Width         : 1 280 pixels
"""


def test_render_cli_wraps_in_pre():
    out = render_cli(SAMPLE, "episode.mkv")
    assert out.startswith("📋 <b>Full MediaInfo</b>")
    assert "<pre>" in out and "</pre>" in out
    assert "Matroska" in out


def test_render_cli_escapes_html():
    out = render_cli("Writing library : x264 <core 164>", "x.mkv")
    assert "&lt;core 164&gt;" in out
    assert "<core" not in out  # raw angle brackets neutralised


def test_render_cli_truncates_long_output():
    big = "\n".join(f"Field {i} : value" for i in range(1000))
    out = render_cli(big, "x.mkv")
    assert len(out) < 4096  # safely under Telegram's message cap
    assert "truncated" in out


def test_render_card_compact():
    media = make_media(width=854, height=480)   # jpn aac stereo, 20 min
    out = render_card(media, "episode.mkv")
    assert "📊 <b>MediaInfo</b>" in out
    assert "854×480" in out
    assert "H264" in out
    assert "AAC Stereo [jpn]" in out
    assert len(out) < 4096
