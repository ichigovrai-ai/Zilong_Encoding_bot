"""Render MediaInfo as the /mediainfo report text (HTML for Telegram)."""
from __future__ import annotations

from html import escape

from bot.services.ffprobe import MediaInfo
from bot.utils.timefmt import fmt_hms, human_size


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
