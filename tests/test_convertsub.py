"""Standalone subtitle format conversion (needs the ffmpeg binary)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bot.services.extraction import ExtractionError, convert_subtitle_format
from bot.services.subtitles import UntimedSubtitleError

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)

SRT = ("1\n00:00:04,000 --> 00:00:07,000\nHello.\n\n"
       "2\n00:00:08,000 --> 00:00:10,000\nWorld.\n")


@needs_ffmpeg
async def test_srt_in_txt_to_ass(tmp_path: Path):
    raw = tmp_path / "subs.txt"          # SRT content hiding in .txt
    raw.write_text(SRT, encoding="utf-8")
    out = await convert_subtitle_format(str(raw), "ass", tmp_path / "out",
                                        output_stem="Episode 01")
    text = Path(out).read_text(encoding="utf-8")
    assert out.endswith(".ass")
    assert "Dialogue:" in text and "Hello." in text


@needs_ffmpeg
async def test_srt_to_vtt(tmp_path: Path):
    raw = tmp_path / "subs.srt"
    raw.write_text(SRT, encoding="utf-8")
    out = await convert_subtitle_format(str(raw), "vtt", tmp_path / "out")
    text = Path(out).read_text(encoding="utf-8")
    assert text.startswith("WEBVTT")
    assert "Hello." in text


async def test_same_format_is_passthrough(tmp_path: Path):
    raw = tmp_path / "subs.srt"
    raw.write_text(SRT, encoding="utf-8")
    out = await convert_subtitle_format(str(raw), "srt", tmp_path / "out")
    assert Path(out).read_text(encoding="utf-8").count("-->") == 2


async def test_untimed_rejected(tmp_path: Path):
    raw = tmp_path / "plain.txt"
    raw.write_text("Just some prose.\nNo timing at all.\n", encoding="utf-8")
    with pytest.raises(UntimedSubtitleError):
        await convert_subtitle_format(str(raw), "srt", tmp_path / "out")


async def test_bad_target_rejected(tmp_path: Path):
    raw = tmp_path / "subs.srt"
    raw.write_text(SRT, encoding="utf-8")
    with pytest.raises(ExtractionError):
        await convert_subtitle_format(str(raw), "pdf", tmp_path / "out")
