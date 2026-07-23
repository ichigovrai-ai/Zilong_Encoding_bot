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
    await message.reply_text(messages.HELP, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("presets") & filters.private)
async def cmd_presets(_: Client, message: Message) -> None:
    rt = get_runtime()
    lines = ["<b>🎛 Encoding presets</b>", ""]
    for name, cfg in rt.presets.items():
        res = f"{cfg['resolution']}p" if cfg.get("resolution") else "original"
        lines.append(
            f"<code>{name}</code> — {cfg.get('label', name)}: {res}, "
            f"{cfg.get('video_codec', '?')}, CRF {cfg.get('crf', '?')}"
        )
    lines += ["", "Presets seed the quality menus; pick options per job "
              "after sending a video."]
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("settings") & filters.private)
async def cmd_settings(_: Client, message: Message) -> None:
    rt = get_runtime()
    cfg = rt.config
    await message.reply_text(
        "<b>⚙️ Current defaults</b>\n\n"
        f"Video codec: <code>{cfg.default_video_codec}</code>\n"
        f"Audio codec: <code>{cfg.default_audio_codec}</code>\n"
        f"CRF: <code>{cfg.default_crf}</code> · "
        f"Preset: <code>{cfg.default_preset}</code>\n"
        f"Seedr auto-delete: <code>{'on' if cfg.seedr_auto_delete else 'off'}</code>\n"
        f"Max input: {human_size(cfg.max_input_size)} · "
        f"Max output: {human_size(cfg.max_output_size)}\n"
        f"Jobs per user: {cfg.max_jobs_per_user}",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("status") & filters.private)
async def cmd_status(_: Client, message: Message) -> None:
    rt = get_runtime()
    from bot.jobs.states import ACTIVE, JobStatus
    active = await rt.repo.by_status(*ACTIVE)
    queued = await rt.repo.by_status(JobStatus.QUEUED)
    caps = ", ".join(k for k, v in rt.capabilities.items() if v) or "unknown"
    uptime = time.time() - _started_at
    await message.reply_text(
        "<b>📊 Bot status</b>\n\n"
        f"Uptime: {uptime / 3600:.1f} h\n"
        f"Active jobs: {len(active)} · Queued: {len(queued)}\n"
        f"Free disk: {human_size(free_disk_bytes(rt.config.work_dir))}\n"
        f"FFmpeg: {caps}\n"
        f"Seedr: {'configured' if rt.config.seedr_enabled else 'not configured'}",
        parse_mode=enums.ParseMode.HTML,
    )
