"""Extracting embedded streams: subtitles and audio.

Bitmap subtitles (PGS/VobSub) cannot be converted to text without OCR, which
is not implemented — they are reported as unsupported for hardsub-by-text and
can only be extracted in their native form inside an .mks/.sup container.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bot.services.ffprobe import MediaInfo, SubtitleStream
from bot.utils.subprocesses import CancelToken, run_process, tail_text

log = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    pass


async def extract_text_subtitle(
    input_path: str, stream: SubtitleStream, out_dir: str | Path, *,
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
) -> str:
    """Extract an embedded TEXT subtitle stream to .ass (preserves styling for
    ASS sources; SRT/VTT convert losslessly enough for burn-in)."""
    if stream.is_bitmap:
        raise ExtractionError(
            "This is a bitmap subtitle (PGS/VobSub). It cannot be hardsubbed "
            "as text — OCR is not supported."
        )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # ASS sources stay ASS; everything else becomes SRT to avoid surprising
    # style synthesis.
    ext = "ass" if stream.codec in ("ass", "ssa") else "srt"
    out = out_dir / f"embedded_{stream.type_index}.{ext}"
    args = [
        ffmpeg, "-hide_banner", "-y", "-i", input_path,
        "-map", f"0:s:{stream.type_index}", "-c:s", ext, str(out),
    ]
    code, _, err = await run_process(args, timeout=300, cancel_token=cancel_token)
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        raise ExtractionError(
            f"Could not extract subtitle stream: {tail_text(err, 300)}"
        )
    return str(out)


async def extract_audio(
    input_path: str, audio_type_index: int, out_dir: str | Path, *,
    codec: str = "copy", container: str = "mka",
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
) -> str:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"audio_{audio_type_index}.{container}"
    args = [ffmpeg, "-hide_banner", "-y", "-i", input_path,
            "-map", f"0:a:{audio_type_index}", "-c:a", codec, "-vn", str(out)]
    code, _, err = await run_process(args, timeout=1800, cancel_token=cancel_token)
    if code != 0 or not out.exists():
        raise ExtractionError(f"Audio extraction failed: {tail_text(err, 300)}")
    return str(out)


def describe_subtitle_streams(media: MediaInfo) -> list[str]:
    """Human-readable one-liners for the embedded-subtitle picker."""
    lines = []
    for s in media.subtitles:
        flags = []
        if s.default:
            flags.append("default")
        if s.forced:
            flags.append("forced")
        if s.is_bitmap:
            flags.append("bitmap — no hardsub")
        desc = f"#{s.type_index} {s.language} {s.codec}"
        if s.title:
            desc += f" — {s.title[:30]}"
        if flags:
            desc += f" [{', '.join(flags)}]"
        lines.append(desc)
    return lines
