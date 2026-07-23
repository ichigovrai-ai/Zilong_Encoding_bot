"""Media probing via ffprobe.

Hardened invocation carried over from the audited project: JSON output,
bounded analyze/probe sizes (lighter for URLs, which stall on slow CDNs),
hard process timeout, browser user-agent for CDNs that throttle Lavf.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from fractions import Fraction

from bot.utils.subprocesses import run_process, tail_text

log = logging.getLogger(__name__)

BITMAP_SUB_CODECS = frozenset({
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "pgssub", "dvdsub",
    "xsub",
})
TEXT_SUB_CODECS = frozenset({
    "ass", "ssa", "subrip", "srt", "webvtt", "vtt", "mov_text", "text",
    "microdvd",
})


class ProbeError(RuntimeError):
    """ffprobe failed or the input is not analysable media."""


@dataclass(slots=True)
class VideoStream:
    index: int
    codec: str
    width: int
    height: int
    fps: float
    pix_fmt: str
    bitrate: int  # bps, 0 if unknown


@dataclass(slots=True)
class AudioStream:
    index: int
    codec: str
    channels: int
    language: str
    title: str
    bitrate: int


@dataclass(slots=True)
class SubtitleStream:
    index: int          # absolute stream index
    type_index: int     # index among subtitle streams (for -si / -map 0:s:N)
    codec: str
    language: str
    title: str
    default: bool
    forced: bool

    @property
    def is_bitmap(self) -> bool:
        return self.codec in BITMAP_SUB_CODECS


@dataclass(slots=True)
class Attachment:
    index: int
    filename: str
    mimetype: str

    @property
    def is_font(self) -> bool:
        mt = self.mimetype.lower()
        fn = self.filename.lower()
        return (
            "font" in mt or mt in ("application/x-truetype-font", "application/vnd.ms-opentype")
            or fn.endswith((".ttf", ".otf", ".ttc"))
        )


@dataclass(slots=True)
class MediaInfo:
    path: str
    container: str
    duration: float
    size: int
    bitrate: int
    video: list[VideoStream] = field(default_factory=list)
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleStream] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    chapters: int = 0

    @property
    def main_video(self) -> VideoStream | None:
        return self.video[0] if self.video else None


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        try:
            value = float(Fraction(raw))
            if 0 < value < 1000:
                return round(value, 3)
        except (ValueError, ZeroDivisionError):
            continue
    return 0.0


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_ffprobe_json(payload: dict, path: str) -> MediaInfo:
    fmt = payload.get("format") or {}
    info = MediaInfo(
        path=path,
        container=(fmt.get("format_name") or "").split(",")[0],
        duration=float(fmt.get("duration") or 0.0),
        size=_int(fmt.get("size")),
        bitrate=_int(fmt.get("bit_rate")),
        chapters=len(payload.get("chapters") or []),
    )
    sub_type_index = 0
    for s in payload.get("streams") or []:
        ctype = s.get("codec_type")
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        if ctype == "video" and not disp.get("attached_pic"):
            info.video.append(VideoStream(
                index=_int(s.get("index")),
                codec=(s.get("codec_name") or "").lower(),
                width=_int(s.get("width")),
                height=_int(s.get("height")),
                fps=_fps(s),
                pix_fmt=s.get("pix_fmt") or "",
                bitrate=_int(s.get("bit_rate")),
            ))
        elif ctype == "audio":
            info.audio.append(AudioStream(
                index=_int(s.get("index")),
                codec=(s.get("codec_name") or "").lower(),
                channels=_int(s.get("channels"), 2),
                language=(tags.get("language") or "und").lower(),
                title=tags.get("title") or "",
                bitrate=_int(s.get("bit_rate")),
            ))
        elif ctype == "subtitle":
            info.subtitles.append(SubtitleStream(
                index=_int(s.get("index")),
                type_index=sub_type_index,
                codec=(s.get("codec_name") or "").lower(),
                language=(tags.get("language") or "und").lower(),
                title=tags.get("title") or "",
                default=bool(disp.get("default")),
                forced=bool(disp.get("forced")),
            ))
            sub_type_index += 1
        elif ctype == "attachment":
            info.attachments.append(Attachment(
                index=_int(s.get("index")),
                filename=tags.get("filename") or "",
                mimetype=tags.get("mimetype") or "",
            ))
    return info


async def probe_media(path: str, *, ffprobe: str = "ffprobe",
                      timeout: float = 30.0) -> MediaInfo:
    is_url = "://" in path
    args = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters",
    ]
    if is_url:
        args += ["-user_agent", "Mozilla/5.0",
                 "-analyzeduration", "5000000", "-probesize", "5000000"]
    else:
        args += ["-analyzeduration", "20000000", "-probesize", "50000000"]
    args.append(path)

    try:
        code, out, err = await run_process(args, timeout=timeout)
    except asyncio.TimeoutError:
        raise ProbeError(f"ffprobe timed out after {timeout:.0f}s") from None
    if code != 0:
        raise ProbeError(f"ffprobe failed (exit {code}): {tail_text(err, 500)}")
    try:
        payload = json.loads(out.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON: {exc}") from exc

    info = parse_ffprobe_json(payload, path)
    if not info.video and not info.audio:
        raise ProbeError("No audio or video streams found — the file may be corrupt.")
    return info
