"""/start, /help, /presets, /settings, /status."""
from __future__ import annotations

import time

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from bot.runtime import get_runtime
from bot.ui import messages
from bot.utils.paths import free_disk_bytes
from bot.utils.timefmt import human_size

_started_at = time.time()


@Client.on_message(filters.command("start") & filters.private)
async def cmd_start(_: Client, message: Message) -> None:
    rt = get_runtime()
    async with rt.db.transaction() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, first_seen, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen",
            (message.from_user.id, time.time(), time.time()),
        )
    await message.reply_text(messages.START, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("help") & filters.private)
async def cmd_help(_: Client, message: Message) -> None:
    rt = get_runtime()
    text = messages.HELP
    if message.from_user and message.from_user.id == rt.config.owner_id:
        text += (
            "\n\n👑 <b>OWNER — PAID ACCESS</b>\n"
            "<code>/grant &lt;id|@user&gt; &lt;days&gt; [note]</code> — grant/extend access\n"
            "<code>/revoke &lt;id|@user&gt;</code> — remove access\n"
            "<code>/subs</code> — list active subscribers\n"
            "<i>Set SUBSCRIPTION_MODE=1 to restrict the bot to paid users.</i>"
            "\n\n📦 <b>OWNER — DUMP CHANNELS</b>\n"
            "<code>/adddump &lt;id|@channel&gt;</code> — copy every upload there\n"
            "<code>/removedump &lt;id|@channel&gt;</code> — stop copying\n"
            "<code>/dumps</code> — list the dump channels"
        )
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("presets") & filters.private)
async def cmd_presets(_: Client, message: Message) -> None:
    rt = get_runtime()
    from bot.ui.panels import SEP
    codec_icon = {"libx264": "🎯", "libx265": "🚀", "libsvtav1": "🧪"}
    lines = ["🎛 <b>ENCODING PRESETS</b>", SEP]
    for name, cfg in rt.presets.items():
        res = f"{cfg['resolution']}p" if cfg.get("resolution") else "original"
        icon = codec_icon.get(str(cfg.get("video_codec")), "🎬")
        lines.append(
            f"{icon} <code>{name}</code> — {cfg.get('label', name)}: {res}, "
            f"{cfg.get('video_codec', '?')}, CRF {cfg.get('crf', '?')}"
        )
    lines += ["", "💡 Presets seed the quality menus; pick options per job "
              "after sending a video."]
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


def settings_text(rt, settings: dict) -> str:
    from bot.services.user_settings import SETTINGS, value_of
    from bot.ui.panels import SEP
    cfg = rt.config
    lines = ["⚙️ <b>SETTINGS</b>", SEP,
             "<i>Tap any button below to change it.</i>", ""]
    for s in SETTINGS:
        lines.append(f"{s.label}: <b>{s.display(value_of(settings, s.key))}</b>")
    lines += [
        "", "🧰 <b>BOT LIMITS</b>", SEP,
        f"📥 Max input {human_size(cfg.max_input_size)} · "
        f"📤 max output {human_size(cfg.max_output_size)}",
        f"🌱 Seedr auto-delete: "
        f"<code>{'on' if cfg.seedr_auto_delete else 'off'}</code> · "
        f"👤 jobs/user: {cfg.max_jobs_per_user}",
    ]
    return "\n".join(lines)


@Client.on_message(filters.command("settings") & filters.private)
async def cmd_settings(_: Client, message: Message) -> None:
    from bot.services.user_settings import get_user_settings
    from bot.ui import keyboards
    rt = get_runtime()
    settings = await get_user_settings(rt.db, message.from_user.id)
    await message.reply_text(
        settings_text(rt, settings),
        reply_markup=keyboards.settings_menu(settings),
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("status") & filters.private)
async def cmd_status(_: Client, message: Message) -> None:
    rt = get_runtime()
    from bot.jobs.states import ACTIVE, JobStatus
    from bot.ui.panels import SEP
    active = await rt.repo.by_status(*ACTIVE)
    queued = await rt.repo.by_status(JobStatus.QUEUED)
    caps = ", ".join(k for k, v in rt.capabilities.items() if v) or "unknown"
    uptime = time.time() - _started_at
    seedr = "🟢 configured" if rt.config.seedr_enabled else "⚪ not configured"
    await message.reply_text(
        "📡 <b>BOT STATUS</b>\n"
        f"{SEP}\n"
        f"⏱ Uptime: <b>{uptime / 3600:.1f} h</b>\n"
        f"⚙️ Active: <b>{len(active)}</b> · 📥 Queued: <b>{len(queued)}</b>\n"
        f"💿 Free disk: <b>{human_size(free_disk_bytes(rt.config.work_dir))}</b>\n"
        f"🎬 FFmpeg: <i>{caps}</i>\n"
        f"🌱 Seedr: {seedr}",
        parse_mode=enums.ParseMode.HTML,
    )
