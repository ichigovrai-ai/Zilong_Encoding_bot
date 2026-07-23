"""Inline keyboard builders. Pure functions: job/options in → markup out."""
from __future__ import annotations

from typing import Any

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.ui.callbacks import pack


def _btn(text: str, verb: str, job_id: str, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=pack(verb, job_id, arg))


def _nav_row(job_id: str, back: bool = True) -> list[InlineKeyboardButton]:
    row = []
    if back:
        row.append(_btn("⬅️ Back", "bk", job_id))
    row.append(_btn("✖️ Cancel", "x", job_id))
    return row


def action_menu(job_id: str, has_subtitle_streams: bool) -> InlineKeyboardMarkup:
    rows = [
        [_btn("🎬 Encode", "a", job_id, "encode"),
         _btn("🗜 Compress", "a", job_id, "compress")],
        [_btn("🔄 Convert", "a", job_id, "convert"),
         _btn("💬 Hardsub", "a", job_id, "hardsub")],
        [_btn("📤 Extract streams", "a", job_id, "extract"),
         _btn("📋 Media info", "a", job_id, "minfo")],
        [_btn("✖️ Cancel", "x", job_id)],
    ]
    return InlineKeyboardMarkup(rows)


def resolution_menu(job_id: str, src_short: int = 0) -> InlineKeyboardMarkup:
    icons = {360: "📱", 480: "📺", 720: "💻", 1080: "🖥", 1440: "🎞", 2160: "🍿"}

    def label(name: str, target: int) -> str:
        prefix = icons.get(target, "")
        if src_short and target > src_short:
            return f"{prefix} {name} ⤴"     # would upscale — flagged
        return f"{prefix} {name}"
    rows = [
        [_btn(label("360p", 360), "r", job_id, "360"),
         _btn(label("480p", 480), "r", job_id, "480")],
        [_btn(label("720p", 720), "r", job_id, "720"),
         _btn(label("1080p", 1080), "r", job_id, "1080")],
        [_btn(label("1440p", 1440), "r", job_id, "1440"),
         _btn(label("2160p / 4K", 2160), "r", job_id, "2160")],
        [_btn("♻️ Original", "r", job_id, "orig"),
         _btn("📐 Custom…", "r", job_id, "cust")],
        _nav_row(job_id),
    ]
    return InlineKeyboardMarkup(rows)


def upscale_confirm_menu(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("⤴️ Upscale anyway", "u", job_id, "yes")],
        [_btn("↩️ Keep original size", "u", job_id, "no")],
        _nav_row(job_id),
    ])


def codec_menu(job_id: str, av1_available: bool) -> InlineKeyboardMarkup:
    rows = [
        [_btn("🎯 H.264", "c", job_id, "x264")],
        [_btn("🚀 H.265 / HEVC", "c", job_id, "x265")],
    ]
    if av1_available:
        rows.append([_btn("🧪 AV1", "c", job_id, "av1")])
    rows.append([_btn("📋 Copy (no re-encode)", "c", job_id, "copy")])
    rows.append(_nav_row(job_id))
    return InlineKeyboardMarkup(rows)


def quality_menu(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🤏 Small size", "q", job_id, "small"),
         _btn("⚖️ Balanced", "q", job_id, "bal")],
        [_btn("💎 High quality", "q", job_id, "high")],
        [_btn("🎛 Custom CRF…", "q", job_id, "crf"),
         _btn("🎯 Target size…", "q", job_id, "size")],
        [_btn("📶 Custom bitrate…", "q", job_id, "br")],
        _nav_row(job_id),
    ])


def audio_menu(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🔊 AAC", "au", job_id, "aac"),
         _btn("🎼 Opus", "au", job_id, "opus")],
        [_btn("📋 Copy audio", "au", job_id, "copy")],
        _nav_row(job_id),
    ])


def subtitle_menu(job_id: str, has_embedded: bool) -> InlineKeyboardMarkup:
    rows = [[_btn("🚫 No subtitles", "s", job_id, "none")]]
    if has_embedded:
        rows.append([_btn("📦 Use embedded subtitle", "s", job_id, "emb")])
    rows.append([_btn("📤 Upload subtitle…", "s", job_id, "upl")])
    rows.append(_nav_row(job_id))
    return InlineKeyboardMarkup(rows)


def subtitle_disposition_menu(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🔥 Hardsub (burn-in)", "s", job_id, "hard")],
        [_btn("📎 Soft subtitles (mux)", "s", job_id, "soft")],
        _nav_row(job_id),
    ])


def embedded_subtitle_menu(job_id: str, streams: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for s in streams[:12]:
        mark = " 🚫" if s.get("bitmap") else ""
        label = f"#{s['type_index']} {s['language']} {s['codec']}{mark}"
        if s.get("title"):
            label += f" · {s['title'][:15]}"
        rows.append([_btn(label[:60], "ss", job_id, str(s["type_index"]))])
    rows.append(_nav_row(job_id))
    return InlineKeyboardMarkup(rows)


def confirm_menu(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🚀 Start", "go", job_id)],
        _nav_row(job_id),
    ])


def cancel_only(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("✖️ Cancel", "x", job_id)]])


def retry_menu(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("🔁 Retry", "rt", job_id)]])


def convertsub_menu(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("➡️ To SRT", "cv", job_id, "srt"),
         _btn("➡️ To ASS", "cv", job_id, "ass")],
        [_btn("➡️ To VTT", "cv", job_id, "vtt")],
        [_btn("✖️ Cancel", "x", job_id)],
    ])


def untimed_txt_menu(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("Upload timed subtitles", "s", job_id, "upl")],
        [_btn("Cancel", "x", job_id)],
    ])


def episodes_menu(job_id: str, matches: list[dict[str, Any]],
                  selected: list[int]) -> InlineKeyboardMarkup:
    """Multi-episode picker. `matches` mirrors job.seedr['matches']."""
    rows = []
    for i, m in enumerate(matches[:20]):
        chosen = "☑️" if i in selected else "⬜️"
        conf = m.get("confidence", "none")
        mark = {"exact": "✅", "high": "🟢", "uncertain": "⚠️", "none": "➖"}[conf]
        name = m.get("video_label") or m["video"].rsplit("/", 1)[-1]
        rows.append([_btn(f"{chosen} {mark} {name[:40]}", "e", job_id, str(i))])
        if conf == "uncertain":
            rows.append([_btn("   └ pick subtitle…", "fx", job_id, str(i))])
    rows.append([_btn("Select all", "ea", job_id),
                 _btn("🚀 Continue", "go", job_id)])
    rows.append([_btn("✖️ Cancel", "x", job_id)])
    return InlineKeyboardMarkup(rows)


def subtitle_pick_menu(job_id: str, episode_idx: int,
                       subtitles: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i, sub in enumerate(subtitles[:15]):
        name = sub.rsplit("/", 1)[-1]
        rows.append([_btn(name[:55], "fs", job_id, f"{episode_idx}:{i}")])
    rows.append([_btn("No subtitle for this episode", "fs", job_id,
                      f"{episode_idx}:-1")])
    rows.append(_nav_row(job_id))
    return InlineKeyboardMarkup(rows)


def settings_menu(panel_style: str, upload_mode: str) -> InlineKeyboardMarkup:
    """User preferences — settings callbacks carry no job id."""
    from bot.services.user_settings import UPLOAD_MODE_LABELS
    from bot.ui.panels import style_label
    return InlineKeyboardMarkup([
        [_btn(f"🎨 Panel style: {style_label(panel_style)}", "st", "", "style")],
        [_btn(f"📤 Upload as: "
              f"{UPLOAD_MODE_LABELS.get(upload_mode, '🤖 Auto')}",
              "st", "", "upload")],
    ])


def extract_menu(job_id: str, n_subs: int, n_audio: int) -> InlineKeyboardMarkup:
    rows = []
    if n_subs:
        rows.append([_btn(f"💬 Extract subtitles ({n_subs})", "a", job_id, "exsub")])
    if n_audio:
        rows.append([_btn(f"🔊 Extract audio ({n_audio})", "a", job_id, "exaud")])
    rows.append(_nav_row(job_id))
    return InlineKeyboardMarkup(rows)
