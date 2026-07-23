"""Message templates (HTML). Progress rendering lives in bot/ui/panels.py —
this module holds static texts, summaries and one-line renderings."""
from __future__ import annotations

from html import escape

from bot.jobs.models import Job
from bot.services.ffprobe import MediaInfo
from bot.ui.panels import SEP
from bot.utils.timefmt import fmt_hms, human_size

START = (
    "👋 <b>ZILONG ENCODING BOT</b>\n"
    f"{SEP}\n"
    "Send me a <b>🎞 video</b> (file or link), a <b>🧲 magnet link</b>, or a "
    "<b>📎 .torrent</b> and I'll encode, compress, convert or hardsub it.\n\n"
    "🖥 <b>Resolutions</b> · 360p → 2160p, aspect ratio preserved\n"
    "🎯 <b>Codecs</b> · H.264, H.265/HEVC, AV1\n"
    "💬 <b>Subtitles</b> · ASS/SSA, SRT, VTT, SUB, TXT (content-detected), "
    "embedded\n"
    "🌱 <b>Seedr</b> · magnets are fetched in the cloud and hardsubbed\n"
    "📊 <b>Live panels</b> · emoji progress with speed, ETA and stages\n\n"
    "Type /help for the full command list. 🚀"
)

HELP = (
    "📖 <b>COMMANDS</b>\n"
    f"{SEP}\n"
    "🎬 /encode — encode/resize a video\n"
    "🗜 /compress — shrink a video (profiles or target size)\n"
    "🔄 /convert — change container / codec / resolution / audio\n"
    "🔥 /hardsub — burn subtitles into a video\n"
    "🌱 /seedr — Seedr account, storage and active transfers\n"
    "📋 /mediainfo — full stream report for a file\n"
    "📤 /extract · /extractsub · /extractaudio · /convertsub\n"
    "🎛 /presets — list encoding presets\n"
    "⚙️ /settings — panel style, upload mode, defaults\n"
    "📊 /queue — waiting / active / finished jobs\n"
    "📡 /status — bot status and limits\n"
    "🛑 /cancel — cancel a job\n"
    "🗂 /history — your recent jobs\n"
    "🔑 /access — check your subscription status\n\n"
    "💡 <b>HOW TO START</b>\n"
    f"{SEP}\n"
    "Just send a video, a direct http(s) link, a magnet link, or a .torrent "
    "file — the menu appears automatically. Replying to a video with /encode "
    "also works.\n\n"
    "🧲 <b>SEEDR MAGNET FLOW</b>\n"
    f"{SEP}\n"
    "magnet → ☁️ Seedr download → ☑️ pick episodes → 💬 confirm subtitle "
    "matches → 🖥 choose resolution/codec → 🔥 hardsub → 📤 upload"
)


def media_summary(media: MediaInfo, filename: str) -> str:
    v = media.main_video
    lines = [
        "🎞 <b>VIDEO RECEIVED</b>",
        SEP,
        f"📄 <code>{escape(filename)}</code>",
        f"📦 {human_size(media.size)} · ⏱ {fmt_hms(media.duration)}",
    ]
    if v:
        fps = f" · {v.fps:g} fps" if v.fps else ""
        lines.append(f"🖼 {escape(v.codec)} {v.width}×{v.height}{fps}")
    if media.audio:
        langs = ", ".join(dict.fromkeys(a.language for a in media.audio))
        lines.append(f"🔊 {len(media.audio)} audio ({escape(langs)})")
    if media.subtitles:
        langs = ", ".join(dict.fromkeys(s.language for s in media.subtitles))
        lines.append(f"💬 {len(media.subtitles)} subtitle(s) ({escape(langs)})")
    lines += ["", "Choose an action:"]
    return "\n".join(lines)


def wizard_header(job: Job) -> str:
    o = job.options
    parts = []
    if o.get("resolution") == "orig":
        parts.append("♻️ original")
    elif isinstance(o.get("resolution"), list):
        parts.append(f"📐 {o['resolution'][0]}×{o['resolution'][1]}")
    elif o.get("resolution"):
        parts.append(f"🖥 {o['resolution']}p")
    if o.get("codec"):
        parts.append({"x264": "🎯 H.264", "x265": "🚀 HEVC", "av1": "🧪 AV1",
                      "copy": "📋 copy"}.get(o["codec"], o["codec"]))
    if o.get("quality"):
        q = o["quality"]
        if q == "crf" and o.get("crf") is not None:
            parts.append(f"🎛 CRF {o['crf']}")
        elif q == "size" and o.get("target_size_mb"):
            parts.append(f"🎯 {o['target_size_mb']} MB")
        elif q == "bitrate" and o.get("video_bitrate"):
            parts.append(f"📶 {o['video_bitrate']} kbps")
        else:
            parts.append({"small": "🤏 small", "bal": "⚖️ balanced",
                          "high": "💎 high"}.get(q, q))
    if o.get("audio"):
        parts.append({"aac": "🔊 AAC", "opus": "🎼 Opus",
                      "copy": "📋 audio copy"}.get(o["audio"], o["audio"]))
    if o.get("subtitle_mode") and o["subtitle_mode"] != "none":
        parts.append({"hard": "🔥 hardsub", "soft": "📎 soft subs"}.get(
            o["subtitle_mode"], o["subtitle_mode"]))
    kind_icon = {"encode": "🎬", "compress": "🗜", "convert": "🔄",
                 "hardsub": "🔥", "seedr_hardsub": "🧲"}.get(job.kind, "🎬")
    chosen = " · ".join(parts) if parts else "nothing chosen yet"
    return (f"{kind_icon} <b>{escape(job.kind.replace('_', ' ').upper())}</b>\n"
            f"{SEP}\n"
            f"📄 <code>{escape(job.input_name[:48])}</code>\n"
            f"🧾 <i>{chosen}</i>\n\n")


UNTIMED_TXT = (
    "⚠️ <b>This TXT file does not contain usable subtitle timestamps.</b>\n\n"
    "It looks like plain text (no SRT/ASS/VTT/MicroDVD/LRC timing). "
    "I can't invent timings for it.\n\nChoose an option:"
)

STATUS_ICONS: dict[str, str] = {
    "created": "🆕", "waiting_for_input": "⏳", "queued": "📥",
    "downloading": "⬇️", "probing": "🔎", "preparing_subtitles": "💬",
    "encoding": "⚙️", "uploading": "⬆️", "completed": "✅",
    "failed": "❌", "cancelled": "🛑", "cleaning": "🧹",
}


def job_line(job: Job) -> str:
    icon = STATUS_ICONS.get(str(job.status), "•")
    name = job.input_name[:35] or job.kind
    pct = f" · {job.progress:.0f}%" if job.status in (
        "downloading", "encoding", "uploading") else ""
    return (f"{icon} <code>{job.id}</code> {escape(name)} — "
            f"<i>{job.status}</i>{pct}")
