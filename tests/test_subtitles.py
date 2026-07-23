"""Subtitle format sniffing (the TXT rules), encoding detection, conversion
and preparation."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.services.subtitles import (
    SubtitleError, SubtitleFormat, UntimedSubtitleError,
    ass_referenced_fonts, convert_lrc_to_srt, convert_microdvd_to_srt,
    convert_vtt_to_srt, decode_subtitle, detect_encoding,
    prepare_subtitle_file, sniff_format, validate_srt,
)

SRT = "1\n00:00:04,000 --> 00:00:07,000\nHello.\n\n2\n00:00:08,000 --> 00:00:10,000\nWorld.\n"
ASS = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
       "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
       "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n[Events]\n"
       "Dialogue: 0,0:00:04.00,0:00:07.00,Default,,0,0,0,,Hello.\n")
VTT = "WEBVTT\n\n00:04.000 --> 00:07.000\nHello.\n\n00:08.000 --> 00:10.000\nWorld.\n"
MICRODVD = "{120}{180}Hello.\n{200}{280}World.\n"
LRC = "[00:04.00]Hello.\n[00:08.00]World.\n[00:12.00]Again.\n"
PLAIN = "Hello.\nThis is the next line.\nAnother line.\n"


# ── Sniffing: extension means nothing, content decides ───────────────────

def test_sniff_srt_in_txt():
    assert sniff_format(SRT) == SubtitleFormat.SRT


def test_sniff_ass_in_txt():
    assert sniff_format(ASS) == SubtitleFormat.ASS


def test_sniff_vtt():
    assert sniff_format(VTT) == SubtitleFormat.VTT


def test_sniff_microdvd():
    assert sniff_format(MICRODVD) == SubtitleFormat.MICRODVD


def test_sniff_lrc():
    assert sniff_format(LRC) == SubtitleFormat.LRC


def test_sniff_plain_text_is_untimed():
    assert sniff_format(PLAIN) == SubtitleFormat.UNTIMED


# ── Encoding detection ───────────────────────────────────────────────────

def test_detect_utf8_bom():
    assert detect_encoding(b"\xef\xbb\xbfHello") == "utf-8-sig"


def test_detect_utf16():
    data = SRT.encode("utf-16")
    assert detect_encoding(data) == "utf-16"
    assert "Hello." in decode_subtitle(data)


def test_detect_utf16le_no_bom():
    data = SRT.encode("utf-16-le")
    assert "Hello." in decode_subtitle(data)


def test_detect_cp1252():
    data = "1\n00:00:04,000 --> 00:00:07,000\nCafé français.\n".encode("cp1252")
    assert "Café" in decode_subtitle(data)


# ── Converters ───────────────────────────────────────────────────────────

def test_microdvd_conversion():
    srt = convert_microdvd_to_srt(MICRODVD, fps=24.0)
    assert "-->" in srt and "Hello." in srt
    assert "00:00:05,000" in srt  # 120 frames @ 24fps


def test_lrc_conversion_uses_next_start():
    srt = convert_lrc_to_srt(LRC)
    assert "00:00:04,000 --> 00:00:08,000" in srt


def test_vtt_conversion():
    srt = convert_vtt_to_srt(VTT)
    assert "00:00:04,000 --> 00:00:07,000" in srt


# ── Validation ───────────────────────────────────────────────────────────

def test_validate_srt_drops_bad_events():
    bad = SRT + "\n3\n00:00:20,000 --> 00:00:15,000\nBackwards.\n"
    text, report = validate_srt(bad)
    assert report.events == 2
    assert "Backwards." not in text
    assert report.warnings


def test_validate_srt_rejects_empty():
    with pytest.raises(SubtitleError):
        validate_srt("no timestamps here at all")


def test_ass_fonts_referenced():
    fonts = ass_referenced_fonts(ASS + "Dialogue: 0,0:00:08.00,0:00:09.00,"
                                       "Default,,0,0,0,,{\\fnTrebuchet MS}Hi\n")
    assert "Arial" in fonts and "Trebuchet MS" in fonts


# ── prepare_subtitle_file end-to-end ─────────────────────────────────────

def test_prepare_srt_stored_as_txt(tmp_path: Path):
    raw = tmp_path / "subs.txt"
    raw.write_text(SRT, encoding="utf-8")
    prep = prepare_subtitle_file(raw, tmp_path / "out")
    assert prep.original_format == SubtitleFormat.SRT
    assert prep.path.endswith("sub.srt")
    assert Path(prep.path).read_text(encoding="utf-8").count("-->") == 2


def test_prepare_ass_preserves_styles(tmp_path: Path):
    raw = tmp_path / "styled.txt"
    raw.write_text(ASS, encoding="utf-8")
    prep = prepare_subtitle_file(raw, tmp_path / "out")
    assert prep.format == SubtitleFormat.ASS
    out_text = Path(prep.path).read_text(encoding="utf-8")
    assert "Style: Default,Arial" in out_text  # styling untouched


def test_prepare_untimed_txt_rejected(tmp_path: Path):
    raw = tmp_path / "plain.txt"
    raw.write_text(PLAIN, encoding="utf-8")
    with pytest.raises(UntimedSubtitleError):
        prepare_subtitle_file(raw, tmp_path / "out")


def test_prepare_utf16_microdvd(tmp_path: Path):
    raw = tmp_path / "old.sub"
    raw.write_bytes(MICRODVD.encode("utf-16"))
    prep = prepare_subtitle_file(raw, tmp_path / "out", fps=24.0)
    assert prep.original_format == SubtitleFormat.MICRODVD
    assert Path(prep.path).read_text(encoding="utf-8").count("-->") == 2
