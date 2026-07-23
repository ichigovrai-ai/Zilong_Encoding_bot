"""Emoji panel rendering: bars, styles, speed emojis, stage checklists and
the high-level panels stay phone-safe and HTML-valid."""
from __future__ import annotations

import re

from bot.ui.panels import (
    FOOTER, bar, cancelled_panel, done_panel, encode_panel, encode_speed_emoji,
    episodes_panel, error_panel, next_style, norm_style, pipeline_stages,
    seedr_panel, seedr_stages, short_name, speed_emoji, stage_checklist,
    style_label, transfer_panel,
)

_MiB = 1024 * 1024


def test_bar_styles_and_bounds():
    assert bar(0, "A") == "▱" * 10
    assert bar(100, "A") == "▰" * 10
    assert bar(50, "A") == "▰" * 5 + "▱" * 5
    assert bar(50, "B") == "█" * 6 + "░" * 6
    assert bar(50, "C") == "━" * 6 + "╌" * 6
    assert bar(-10, "A") == "▱" * 10       # clamped
    assert bar(250, "A") == "▰" * 10


def test_style_cycle():
    assert next_style("A") == "B"
    assert next_style("B") == "C"
    assert next_style("C") == "A"
    assert norm_style("garbage") == "A"
    assert norm_style(None) == "A"
    assert style_label("B") == "Blocks"


def test_speed_emoji_scale():
    assert speed_emoji(0) == "🐌"
    assert speed_emoji(100 * 1024) == "🐢"
    assert speed_emoji(1 * _MiB) == "🔥"
    assert speed_emoji(5 * _MiB) == "⚡"
    assert speed_emoji(20 * _MiB) == "🚀"
    assert speed_emoji(100 * _MiB) == "🌪️"


def test_encode_speed_emoji():
    assert encode_speed_emoji(0.3) == "🐢"
    assert encode_speed_emoji(1.5) == "⚡"
    assert encode_speed_emoji(3.0) == "🚀"


def test_short_name_keeps_suffix():
    long = "[Group] Very Long Series Name - Episode 07 [1080p][HEVC].mkv"
    out = short_name(long, 30)
    assert len(out) <= 30
    assert out.endswith("].mkv") or "07" in out  # meaningful tail preserved


def test_stage_checklist_icons():
    rows = stage_checklist([
        ("Download", "done"), ("Analyse", "active"),
        ("Encode", "pending"), ("Upload", "failed"),
    ])
    joined = "\n".join(rows)
    assert "✅ Download" in joined
    assert "🔄 Analyse" in joined
    assert "▫️ Encode" in joined
    assert "❌ Upload" in joined


def test_pipeline_stages_progression():
    stages = pipeline_stages("encode", with_subtitles=True)
    d = dict(stages)
    assert d["Download"] == "done" and d["Subtitles"] == "done"
    assert d["Encode"] == "active" and d["Upload"] == "pending"
    no_subs = pipeline_stages("encode", with_subtitles=False)
    assert "Subtitles" not in dict(no_subs)


def test_seedr_stages():
    d = dict(seedr_stages("cloud"))
    assert d["Magnet"] == "done"
    assert d["Seedr cloud"] == "active"
    assert d["Match subs"] == "pending"


def _assert_phone_safe(html: str) -> None:
    # No line should be wildly long, and code/bold tags must be balanced.
    for tag in ("b", "i", "code"):
        assert html.count(f"<{tag}>") == html.count(f"</{tag}>")
    for line in re.sub(r"<[^>]+>", "", html).split("\n"):
        assert len(line) <= 72, f"line too long: {line!r}"


def test_transfer_panel_render():
    html = transfer_panel(
        title="Downloading", icon="📥", filename="Episode 03.mkv",
        done=800 * _MiB, total=2000 * _MiB, speed=8 * _MiB, elapsed=95,
        source="Telegram 📲",
        stages=pipeline_stages("download", with_subtitles=True), style="A",
    )
    assert "📥" in html and "40.0%" in html and "▰" in html
    assert "⚡" in html          # speed emoji
    assert "ETA" in html
    assert FOOTER not in html    # style A is compact
    _assert_phone_safe(html)


def test_transfer_panel_footer_on_style_b():
    html = transfer_panel(title="Uploading", icon="📤", filename="x.mp4",
                          done=10, total=100, style="B")
    assert FOOTER in html and "█" in html


def test_encode_panel_render():
    html = encode_panel(
        filename="Episode 03.mkv", settings_line="libx265 · 1280×720 · CRF 24",
        pct=43.7, done_s=768, total_s=1771, speed=1.7, fps=41,
        out_size=184 * _MiB, elapsed=452, eta=553,
        stages=pipeline_stages("encode", with_subtitles=True),
        pass_label="pass 2/2", style="A",
    )
    assert "ENCODING" in html and "43.7%" in html
    assert "pass 2/2" in html
    assert "🔄 Encode" in html and "✅ Subtitles" in html
    assert "ETA" in html and "fps" in html
    _assert_phone_safe(html)


def test_encode_panel_no_fake_eta():
    html = encode_panel(filename="x.mkv", settings_line="s", pct=1,
                        done_s=1, total_s=100, speed=0, fps=0, out_size=0,
                        elapsed=5, eta=None)
    assert "ETA" not in html


def test_seedr_panel_render():
    html = seedr_panel(torrent_name="Series Name S01", pct=67,
                       current="cloud", elapsed=760, style="A")
    assert "SEEDR HARDSUB" in html and "67%" in html
    assert "✅ Magnet" in html and "🔄 Seedr cloud" in html
    _assert_phone_safe(html)


def test_episodes_panel_render():
    html = episodes_panel(folder_name="Series S01", n_videos=12, n_subs=11,
                          n_selected=12, n_uncertain=2)
    assert "12" in html and "⚠️" in html and "uncertain" in html
    _assert_phone_safe(html)


def test_done_error_cancelled_panels():
    done = done_panel(filename="out.mp4", out_size=184 * _MiB, elapsed=452,
                      settings_line="720p · HEVC", job_id="abc12345")
    assert "✅" in done and "abc12345" in done
    err = error_panel(filename="in.mkv", error="Encoder <boom> & bang",
                      job_id="abc12345")
    assert "❌" in err and "&lt;boom&gt;" in err   # HTML-escaped
    can = cancelled_panel(filename="in.mkv", job_id="abc12345")
    assert "🛑" in can
    for html in (done, err, can):
        _assert_phone_safe(html)
