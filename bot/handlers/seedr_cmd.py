"""/seedr — account info, storage, transfers and folders."""
from __future__ import annotations

import logging

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from bot.runtime import get_runtime
from bot.services.seedr import SeedrClient, SeedrError
from bot.utils.timefmt import human_size

log = logging.getLogger(__name__)


@Client.on_message(filters.command("seedr") & filters.private)
async def cmd_seedr(_: Client, message: Message) -> None:
    rt = get_runtime()
    if not rt.config.is_authorized(message.from_user.id):
        return
    if not rt.config.seedr_enabled:
        await message.reply_text(
            "⚠️ Seedr is not configured. Set SEEDR_USERNAME and "
            "SEEDR_PASSWORD in the environment."
        )
        return
    status = await message.reply_text("🌱 Checking Seedr…")
    try:
        async with SeedrClient(rt.config.seedr_username,
                               rt.config.seedr_password) as sc:
            total, used = await sc.storage()
            listing = await sc.list_folder(0)
    except SeedrError as exc:
        await status.edit_text(f"⚠️ Seedr error: {exc}")
        return

    lines = ["<b>🌱 Seedr account</b>", ""]
    if total:
        pct = used / total * 100
        lines.append(f"<b>Storage:</b> {human_size(used)} / "
                     f"{human_size(total)} ({pct:.0f}%)")
    if listing.torrents:
        lines += ["", "<b>Active transfers</b>"]
        for t in listing.torrents[:10]:
            lines.append(f"⏬ {t.name[:45]} — {t.progress:.0f}%")
    if listing.folders:
        lines += ["", "<b>Folders</b>"]
        for f in listing.folders[:10]:
            lines.append(f"📁 {f.name[:45]} ({human_size(f.size)})")
    if not listing.torrents and not listing.folders:
        lines.append("\n<i>No transfers or folders.</i>")
    lines += ["", "Send a magnet link to start a hardsub job."]
    await status.edit_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)
