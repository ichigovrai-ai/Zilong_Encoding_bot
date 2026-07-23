"""Render MediaInfo as the /mediainfo report text (HTML for Telegram).

Two renderers: the rich MediaInfo-CLI dump (matches the old Zilong bot, used
when the `mediainfo` binary is installed) and a self-contained ffprobe panel
used as a fallback.
"""
from __future__ import annotations

import asyncio
import logging
from html import escape

from bot.services.ffprobe import MediaInfo
from bot.utils.timefmt import fmt_hms, human_size

log = logging.getLogger(__name__)

_TG_LIMIT = 3500  # leave headroom under Telegram's 4096-char message cap


async def mediainfo_cli_text(path: str, *, binary: str = "mediainfo",
                             timeout: float = 25.0) -> str | None:
    """Full MediaInfo CLI report, ready to wrap in <pre>. None if the binary
    is missing, times out, or produces nothing useful."""
    from bot.utils.subprocesses import run_process
    try:
        code, out, _ = await run_process([binary, str(path)], timeout=timeout)
    except FileNotFoundError:
        log.info("mediainfo binary not found (%s) — using ffprobe fallback", binary)
        return None
    except asyncio.TimeoutError:
        log.warning("mediainfo timed out on %s", path)
        return None
    if code != 0:
        return None
    text = out.decode("utf-8", errors="replace").strip()
    return text if len(text) > 80 else None


def render_cli(text: str, filename: str) -> str:
    """Wrap raw MediaInfo output as a Telegram HTML message."""
    body = text
    if len(body) > _TG_LIMIT:
        body = body[:_TG_LIMIT].rsplit("\n", 1)[0] + "\n… (truncated)"
    return f"📋 <b>Full MediaInfo</b> — <code>{escape(filename)}</code>\n<pre>{escape(body)}</pre>"


_CHANNELS = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


def render_card(media: MediaInfo, filename: str) -> str:
    """Compact summary card (the Zilong-style MediaInfo view)."""
    from bot.ui.panels import SEP
    lines = [
        "📊 <b>MediaInfo</b>",
        SEP,
        f"📄 <code>{escape(filename)}</code>",
        f"📦 {human_size(media.size)} · ⏱ {fmt_hms(media.duration)}",
    ]
    v = media.main_video
    if v:
        fps = f" · {v.fps:g}fps" if v.fps else ""
        lines += ["", f"🎬 {escape(v.codec.upper())} {v.width}×{v.height}{fps}"]
    for a in media.audio[:3]:
        ch = _CHANNELS.get(a.channels, f"{a.channels}ch")
        lang = f" [{escape(a.language)}]" if a.language and a.language != "und" else ""
        lines.append(f"🔊 {escape(a.codec.upper())} {ch}{lang}")
    if media.subtitles:
        langs = ", ".join(dict.fromkeys(
            s.language for s in media.subtitles if s.language != "und"))
        lines.append(f"💬 {len(media.subtitles)} subtitle(s)"
                     + (f" — {escape(langs)}" if langs else ""))
    if media.chapters:
        lines.append(f"📚 {media.chapters} chapters")
    return "\n".join(lines)


def _kbps(bps: int) -> str:
    return f"{bps // 1000} kbps" if bps > 0 else "unknown"


def render_mediainfo(media: MediaInfo, filename: str) -> str:
    lines = [
        "<b>📋 Media information</b>",
        "",
        f"<b>File:</b> <code>{escape(filename)}</code>",
        f"<b>Size:</b> {human_size(media.size)}",
        f"<b>Container:</b> {escape(media.container or 'unknown')}",
        f"<b>Duration:</b> {fmt_hms(media.duration)}",
        f"<b>Overall bitrate:</b> {_kbps(media.bitrate)}",
    ]
    for v in media.video:
        fps = f"{v.fps:g} fps" if v.fps else "unknown fps"
        lines += [
            "",
            f"<b>🎞 Video #{media.video.index(v)}:</b> {escape(v.codec)} "
            f"{v.width}×{v.height} · {fps}",
            f"    pixel format {escape(v.pix_fmt or 'unknown')}"
            + (f" · {_kbps(v.bitrate)}" if v.bitrate else ""),
        ]
    if media.audio:
        lines.append("")
        lines.append(f"<b>🔊 Audio streams:</b> {len(media.audio)}")
        for i, a in enumerate(media.audio):
            title = f" — {escape(a.title[:30])}" if a.title else ""
            lines.append(
                f"    #{i} {escape(a.language)} {escape(a.codec)} "
                f"{a.channels}ch{title}"
                + (f" · {_kbps(a.bitrate)}" if a.bitrate else "")
            )
    if media.subtitles:
        lines.append("")
        lines.append(f"<b>💬 Subtitle streams:</b> {len(media.subtitles)}")
        for s in media.subtitles:
            flags = []
            if s.default:
                flags.append("default")
            if s.forced:
                flags.append("forced")
            if s.is_bitmap:
                flags.append("bitmap")
            title = f" — {escape(s.title[:30])}" if s.title else ""
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"    #{s.type_index} {escape(s.language)} "
                         f"{escape(s.codec)}{title}{suffix}")
    if media.attachments:
        n_fonts = sum(1 for a in media.attachments if a.is_font)
        lines.append("")
        lines.append(
            f"<b>📎 Attachments:</b> {len(media.attachments)} ({n_fonts} fonts)"
        )
    if media.chapters:
        lines.append(f"<b>📚 Chapters:</b> {media.chapters}")
    return "\n".join(lines)
