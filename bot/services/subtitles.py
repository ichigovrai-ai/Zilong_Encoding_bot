"""Subtitle processing: encoding detection, content-based format sniffing,
conversion to FFmpeg-friendly formats, and validation.

Key rule from the spec: a `.txt` extension means nothing. The real format is
detected from content — SRT, ASS/SSA, WebVTT, MicroDVD and LRC all hide in
`.txt` files in the wild. Plain text without timestamps is REJECTED with
`UntimedSubtitleError`; we never invent timings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from bot.utils.timefmt import parse_timestamp_ms

log = logging.getLogger(__name__)

SUBTITLE_EXTS = frozenset({".ass", ".ssa", ".srt", ".vtt", ".sub", ".txt"})


class SubtitleError(RuntimeError):
    """User-facing subtitle problem."""


class UntimedSubtitleError(SubtitleError):
    """The file is plain text without usable timestamps."""


class SubtitleFormat(StrEnum):
    ASS = "ass"
    SRT = "srt"
    VTT = "vtt"
    MICRODVD = "microdvd"
    LRC = "lrc"
    UNTIMED = "untimed"
    UNKNOWN = "unknown"


# ── Encoding detection ───────────────────────────────────────────────────────

def detect_encoding(data: bytes) -> str:
    """BOM first, then UTF-8 strict, then UTF-16 heuristics, then cp1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16"
    # BOM-less UTF-16 first: NUL bytes are VALID UTF-8, so a plain utf-8
    # attempt would "succeed" on UTF-16-LE text and produce garbage.
    sample = data[:4096]
    if sample and sample.count(0) / len(sample) > 0.25:
        return "utf-16-le" if sample[1:2] == b"\x00" else "utf-16-be"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        data.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        return "latin-1"


def decode_subtitle(data: bytes) -> str:
    text = data.decode(detect_encoding(data), errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")


# ── Format sniffing ──────────────────────────────────────────────────────────

_SRT_TS = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}",
    re.M,
)
_VTT_TS = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3}\s*-->\s*(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3}",
    re.M,
)
_ASS_DIALOGUE = re.compile(r"^\s*Dialogue\s*:", re.M | re.I)
_MICRODVD = re.compile(r"^\{\d+\}\{\d+\}", re.M)
_LRC = re.compile(r"^\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]", re.M)


def sniff_format(text: str) -> SubtitleFormat:
    """Detect the actual subtitle format from content, not extension."""
    if not text.strip():
        return SubtitleFormat.UNKNOWN
    head = text[:8000]
    if "[Script Info]" in head or "[V4+ Styles]" in head or "[V4 Styles]" in head \
            or _ASS_DIALOGUE.search(text):
        return SubtitleFormat.ASS
    if head.lstrip().startswith("WEBVTT"):
        return SubtitleFormat.VTT
    if _SRT_TS.search(text):
        return SubtitleFormat.SRT
    if _VTT_TS.search(text):
        return SubtitleFormat.VTT
    if len(_MICRODVD.findall(text)) >= 2:
        return SubtitleFormat.MICRODVD
    if len(_LRC.findall(text)) >= 3:
        return SubtitleFormat.LRC
    return SubtitleFormat.UNTIMED


# ── Converters (pure Python, testable) ───────────────────────────────────────

def _ms_to_srt(ms: int) -> str:
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def convert_microdvd_to_srt(text: str, fps: float = 23.976) -> str:
    if fps <= 0:
        fps = 23.976
    events: list[tuple[int, int, str]] = []
    for line in text.split("\n"):
        m = re.match(r"^\{(\d+)\}\{(\d+)\}(.*)$", line.strip())
        if not m:
            continue
        start = int(int(m.group(1)) / fps * 1000)
        end = int(int(m.group(2)) / fps * 1000)
        body = m.group(3).replace("|", "\n")
        body = re.sub(r"\{[^}]*\}", "", body).strip()  # {y:i} style codes
        if body and end > start:
            events.append((start, end, body))
    if not events:
        raise SubtitleError("No usable MicroDVD events found.")
    return _events_to_srt(events)


def convert_lrc_to_srt(text: str, *, default_dur_ms: int = 4000) -> str:
    """LRC lines carry only a start time; each event ends when the next starts
    (capped at `default_dur_ms`). These ARE real timestamps — unlike plain
    text — so this is a legitimate conversion, not invented timing."""
    stamped: list[tuple[int, str]] = []
    for line in text.split("\n"):
        m = re.match(r"^((?:\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\])+)(.*)$", line.strip())
        if not m:
            continue
        body = m.group(2).strip()
        if not body or body.startswith("["):  # metadata like [ar:Artist]
            continue
        for tm in re.finditer(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]", m.group(1)):
            minutes, secs = int(tm.group(1)), int(tm.group(2))
            frac = (tm.group(3) or "0").ljust(3, "0")[:3]
            stamped.append(((minutes * 60 + secs) * 1000 + int(frac), body))
    if len(stamped) < 2:
        raise SubtitleError("No usable LRC timestamps found.")
    stamped.sort()
    events = []
    for i, (start, body) in enumerate(stamped):
        nxt = stamped[i + 1][0] if i + 1 < len(stamped) else start + default_dur_ms
        end = min(start + default_dur_ms, max(start + 500, nxt))
        events.append((start, end, body))
    return _events_to_srt(events)


def convert_vtt_to_srt(text: str) -> str:
    events: list[tuple[int, int, str]] = []
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if "-->" in line:
                start_raw, _, rest = line.partition("-->")
                end_raw = rest.strip().split(" ")[0]  # drop cue settings
                start = parse_timestamp_ms(_vtt_ts(start_raw.strip()))
                end = parse_timestamp_ms(_vtt_ts(end_raw.strip()))
                body = "\n".join(lines[i + 1:]).strip()
                body = re.sub(r"</?[cvbiu][^>]*>", "", body)  # cue tags
                if start is not None and end is not None and body and end > start:
                    events.append((start, end, body))
                break
    if not events:
        raise SubtitleError("No usable WebVTT cues found.")
    return _events_to_srt(events)


def _vtt_ts(ts: str) -> str:
    return ts if ts.count(":") == 2 else f"00:{ts}"


def _events_to_srt(events: list[tuple[int, int, str]]) -> str:
    out = []
    for n, (start, end, body) in enumerate(sorted(events), 1):
        out.append(f"{n}\n{_ms_to_srt(start)} --> {_ms_to_srt(end)}\n{body}\n")
    return "\n".join(out)


# ── Validation ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class SubtitleReport:
    format: SubtitleFormat
    events: int
    warnings: list[str] = field(default_factory=list)


def validate_srt(text: str) -> tuple[str, SubtitleReport]:
    """Validate + normalise SRT: drops empty/negative events, renumbers,
    reports overlaps."""
    report = SubtitleReport(SubtitleFormat.SRT, 0)
    events: list[tuple[int, int, str]] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.split("\n") if l.strip()]
        ts_line_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts_line_idx is None:
            continue
        start_raw, _, end_raw = lines[ts_line_idx].partition("-->")
        start = parse_timestamp_ms(start_raw)
        end = parse_timestamp_ms(end_raw.strip().split(" ")[0])
        body = "\n".join(lines[ts_line_idx + 1:]).strip()
        if start is None or end is None:
            report.warnings.append("Dropped event with malformed timestamp.")
            continue
        if end <= start:
            report.warnings.append(f"Dropped event with invalid range at {_ms_to_srt(start)}.")
            continue
        if not body:
            continue  # silently drop empties — safe
        events.append((start, end, body))
    events.sort()
    overlaps = sum(
        1 for a, b in zip(events, events[1:]) if a[1] > b[0] + 500
    )
    if overlaps > max(3, len(events) // 20):
        report.warnings.append(f"{overlaps} heavily overlapping events detected.")
    if not events:
        raise SubtitleError("The subtitle contains no valid timed events.")
    report.events = len(events)
    return _events_to_srt(events), report


def validate_ass(text: str) -> tuple[str, SubtitleReport]:
    """Light validation for ASS/SSA — styles and positioning must survive
    untouched, so the text is NOT rewritten; we only count/verify events."""
    report = SubtitleReport(SubtitleFormat.ASS, 0)
    dialogues = _ASS_DIALOGUE.findall(text)
    if not dialogues:
        raise SubtitleError("The ASS subtitle contains no Dialogue events.")
    report.events = len(dialogues)
    if "[Script Info]" not in text:
        report.warnings.append("Missing [Script Info] section — added a minimal one.")
        text = "[Script Info]\nScriptType: v4.00+\n\n" + text
    return text, report


def ass_referenced_fonts(text: str) -> set[str]:
    """Font names referenced by ASS styles and inline \\fn overrides."""
    fonts: set[str] = set()
    for m in re.finditer(r"^Style:\s*([^\n]+)$", text, re.M):
        parts = m.group(1).split(",")
        if len(parts) >= 2:
            fonts.add(parts[1].strip().lstrip("@"))
    for m in re.finditer(r"\\fn([^\\}]+)", text):
        fonts.add(m.group(1).strip().lstrip("@"))
    return {f for f in fonts if f}


# ── Preparation pipeline ─────────────────────────────────────────────────────

@dataclass(slots=True)
class PreparedSubtitle:
    path: str                 # normalised UTF-8 file, controlled name
    format: SubtitleFormat    # format of the prepared file (ASS or SRT)
    original_format: SubtitleFormat
    report: SubtitleReport


def prepare_subtitle_file(
    raw_path: str | Path, out_dir: str | Path, *, fps: float = 23.976
) -> PreparedSubtitle:
    """Normalise any supported subtitle file into UTF-8 ASS or SRT with a
    controlled ASCII filename inside `out_dir`.

    Raises UntimedSubtitleError for plain text without timestamps, and
    SubtitleError for anything else unusable.
    """
    raw_path = Path(raw_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = raw_path.read_bytes()
    if not data.strip():
        raise SubtitleError("The subtitle file is empty.")
    text = decode_subtitle(data)
    fmt = sniff_format(text)

    if fmt == SubtitleFormat.UNTIMED:
        raise UntimedSubtitleError(
            "This TXT file does not contain usable subtitle timestamps."
        )
    if fmt == SubtitleFormat.UNKNOWN:
        raise SubtitleError("Could not recognise the subtitle format.")

    if fmt == SubtitleFormat.ASS:
        normalised, report = validate_ass(text)
        out = out_dir / "sub.ass"
        out.write_text(normalised, encoding="utf-8")
        return PreparedSubtitle(str(out), SubtitleFormat.ASS, fmt, report)

    if fmt == SubtitleFormat.MICRODVD:
        text = convert_microdvd_to_srt(text, fps)
    elif fmt == SubtitleFormat.LRC:
        text = convert_lrc_to_srt(text)
    elif fmt == SubtitleFormat.VTT:
        text = convert_vtt_to_srt(text)

    normalised, report = validate_srt(text)
    report.format = fmt
    out = out_dir / "sub.srt"
    out.write_text(normalised, encoding="utf-8")
    return PreparedSubtitle(str(out), SubtitleFormat.SRT, fmt, report)
