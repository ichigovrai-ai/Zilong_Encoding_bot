"""Message templates (HTML). All user-visible text lives here."""
from __future__ import annotations

from html import escape

from bot.jobs.models import Job
from bot.services.ffprobe import MediaInfo
from bot.utils.progress import progress_bar
from bot.utils.timefmt import fmt_hms, human_size

START = (
    "👋 <b>Zilong Encoding Bot</b>\n\n"
    "Send me a <b>video</b> (file or link), a <b>magnet link</b>, or a "
    "<b>.torrent</b> and I'll encode, compress, convert or hardsub it.\n\n"
    "• Resolutions: 360p → 2160p, aspect ratio preserved\n"
    "• Codecs: H.264, H.265/HEVC, AV1\n"
    "• Subtitles: ASS/SSA, SRT, VTT, SUB, TXT (content-detected), embedded\n"
    "• Magnet links are fetched through your Seedr account and hardsubbed\n\n"
    "Type /help for the full command list."
)

HELP = (
    "<b>Commands</b>\n"
    "/encode — encode/resize a video\n"
    "/compress — shrink a video (profiles or target size)\n"
    "/convert — change container / codec / resolution / audio\n"
    "/hardsub — burn subtitles into a video\n"
    "/seedr — Seedr account, storage and active transfers\n"
    "/mediainfo — full stream report for a file\n"
    "/extract — extract subtitle or audio streams\n"
    "/extractsub · /extractaudio · /convertsub — quick extraction helpers\n"
    "/presets — list encoding presets\n"
    "/settings — your preferences\n"
    "/queue — waiting / active / finished jobs\n"
    "/status — bot status and limits\n"
    "/cancel — cancel a job\n"
    "/history — your recent jobs\n\n"
    "<b>How to start</b>\n"
    "Just send a video, a direct http(s) link, a magnet link, or a .torrent "
    "file — the menu appears automatically. Reply to a video with /encode "
    "also works.\n\n"
    "<b>Seedr magnet flow</b>\n"
    "magnet → Seedr download → pick episodes → confirm subtitle matches → "
    "choose resolution/codec → hardsub → upload."
)


def media_summary(media: MediaInfo, filename: str) -> str:
    v = media.main_video
    lines = [
        "<b>🎞 Video received</b>",
        "",
        f"<b>File:</b> <code>{escape(filename)}</code>",
        f"<b>Size:</b> {human_size(media.size)} · "
        f"<b>Duration:</b> {fmt_hms(media.duration)}",
    ]
    if v:
        lines.append(
            f"<b>Video:</b> {escape(v.codec)} {v.width}×{v.height}"
            + (f" · {v.fps:g} fps" if v.fps else "")
        )
    if media.audio:
        langs = ", ".join(dict.fromkeys(a.language for a in media.audio))
        lines.append(f"<b>Audio:</b> {len(media.audio)} stream(s) ({escape(langs)})")
    if media.subtitles:
        langs = ", ".join(dict.fromkeys(s.language for s in media.subtitles))
        lines.append(f"<b>Subtitles:</b> {len(media.subtitles)} stream(s) ({escape(langs)})")
    lines += ["", "Choose an action:"]
    return "\n".join(lines)


def wizard_header(job: Job) -> str:
    o = job.options
    parts = []
    if o.get("resolution") == "orig":
        parts.append("original resolution")
    elif o.get("resolution"):
        parts.append(f"{o['resolution']}p")
    if o.get("codec"):
        parts.append({"x264": "H.264", "x265": "HEVC", "av1": "AV1",
                      "copy": "copy"}.get(o["codec"], o["codec"]))
    if o.get("quality"):
        q = o["quality"]
        if q == "crf" and o.get("crf") is not None:
            parts.append(f"CRF {o['crf']}")
        elif q == "size" and o.get("target_size_mb"):
            parts.append(f"→ {o['target_size_mb']} MB")
        else:
            parts.append({"small": "small size", "bal": "balanced",
                          "high": "high quality"}.get(q, q))
    if o.get("subtitle_mode") and o["subtitle_mode"] != "none":
        parts.append({"hard": "hardsub", "soft": "soft subs"}.get(
            o["subtitle_mode"], o["subtitle_mode"]))
    chosen = " · ".join(parts) if parts else "nothing chosen yet"
    return (f"<b>{escape(job.kind.capitalize())}</b> — "
            f"<code>{escape(job.input_name[:50])}</code>\n"
            f"<i>{escape(chosen)}</i>\n\n")


UNTIMED_TXT = (
    "⚠️ <b>This TXT file does not contain usable subtitle timestamps.</b>\n\n"
    "It looks like plain text (no SRT/ASS/VTT/MicroDVD/LRC timing). "
    "I can't invent timings for it.\n\nChoose an option:"
)


def encode_progress(job: Job, *, pct: float, done_s: float, total_s: float,
                    speed: float, fps: float, est_size: int,
                    elapsed: float, eta: float | None) -> str:
    o = job.options
    res = f"{o.get('resolution', '?')}p" if str(o.get("resolution", "")).isdigit() \
        else "original"
    lines = [
        "<b>⚙️ Encoding video</b>",
        "",
        f"<b>File:</b> <code>{escape(job.input_name[:60])}</code>",
        f"<b>Target:</b> {res} · {escape(str(o.get('codec', '?')))}",
        "",
        f"{progress_bar(pct)}  {pct:.1f}%",
        f"<b>Processed:</b> {fmt_hms(done_s)} / {fmt_hms(total_s)}",
    ]
    stats = []
    if speed > 0:
        stats.append(f"{speed:.2f}×")
    if fps > 0:
        stats.append(f"{fps:.0f} fps")
    if stats:
        lines.append(f"<b>Speed:</b> {' · '.join(stats)}")
    if est_size > 0:
        lines.append(f"<b>Output so far:</b> {human_size(est_size)}")
    lines.append(f"<b>Elapsed:</b> {fmt_hms(elapsed)}")
    if eta is not None:
        lines.append(f"<b>Remaining:</b> ~{fmt_hms(eta)}")
    return "\n".join(lines)


def transfer_progress(title: str, filename: str, done: int, total: int,
                      stage_icon: str = "⬇️") -> str:
    pct = done / total * 100 if total > 0 else 0.0
    lines = [
        f"<b>{stage_icon} {escape(title)}</b>",
        "",
        f"<b>File:</b> <code>{escape(filename[:60])}</code>",
    ]
    if total > 0:
        lines += [f"{progress_bar(pct)}  {pct:.1f}%",
                  f"{human_size(done)} / {human_size(total)}"]
    else:
        lines.append(f"{human_size(done)} downloaded")
    return "\n".join(lines)


def seedr_progress(torrent_name: str, pct: float) -> str:
    return (
        "<b>🌱 Seedr download</b>\n\n"
        f"<b>Torrent:</b> <code>{escape(torrent_name[:60])}</code>\n"
        f"<b>Status:</b> downloading on Seedr\n"
        f"{progress_bar(pct)}  {pct:.0f}%"
    )


def job_line(job: Job) -> str:
    icon = {
        "created": "🆕", "waiting_for_input": "⏳", "queued": "📥",
        "downloading": "⬇️", "probing": "🔎", "preparing_subtitles": "💬",
        "encoding": "⚙️", "uploading": "⬆️", "completed": "✅",
        "failed": "❌", "cancelled": "🚫", "cleaning": "🧹",
    }.get(str(job.status), "•")
    name = job.input_name[:35] or job.kind
    pct = f" {job.progress:.0f}%" if job.status in ("downloading", "encoding",
                                                    "uploading") else ""
    return (f"{icon} <code>{job.id}</code> {escape(name)} — "
            f"{job.status}{pct}")


def error_text(job: Job) -> str:
    return (
        f"❌ <b>Job failed</b> — <code>{escape(job.input_name[:50])}</code>\n\n"
        f"{escape(job.error[:800] or 'Unknown error')}"
    )


def completed_text(job: Job, elapsed: float, size: int) -> str:
    return (
        f"✅ <b>Done</b> — <code>{escape(job.input_name[:50])}</code>\n"
        f"{human_size(size)} in {fmt_hms(elapsed)}"
    )
