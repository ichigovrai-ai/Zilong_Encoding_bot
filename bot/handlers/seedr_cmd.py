"""/seedr — multi-account status and management (owner only).

Shows every configured Seedr account with its storage and folders, and lets
the owner clean folders / clear stuck transfers per account or across all.
"""
from __future__ import annotations

import logging

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from bot.runtime import Runtime, get_runtime
from bot.services.seedr import SeedrClient, SeedrError
from bot.ui import keyboards
from bot.ui.panels import SEP
from bot.utils.timefmt import human_size

log = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML


def _mask(email: str) -> str:
    """Show enough of an email to identify the account without dumping it all."""
    name, _, domain = email.partition("@")
    head = name[:4] + "…" if len(name) > 5 else name
    return f"{head}@{domain}" if domain else head


async def render_seedr(rt: Runtime) -> tuple[str, object]:
    """Build the /seedr panel text + keyboard (shared by command and refresh)."""
    accounts = rt.config.seedr_accounts
    lines = [f"🌱 <b>SEEDR — {len(accounts)} account(s)</b>", SEP]
    account_infos: list[tuple[int, int]] = []
    for i, (user, pwd) in enumerate(accounts):
        emoji = keyboards.num_emoji(i)
        try:
            async with SeedrClient(user, pwd, proxy=rt.config.seedr_proxy) as sc:
                total, used = await sc.storage()
                listing = await sc.list_folder(0)
            n_fold, n_tor = len(listing.folders), len(listing.torrents)
            account_infos.append((i, n_fold))
            pct = (used / total * 100) if total else 0
            free = human_size(max(0, total - used)) if total else "?"
            lines.append(f"{emoji} <code>{_mask(user)}</code>")
            lines.append(
                f"    💾 {human_size(used)} / {human_size(total)} "
                f"({pct:.0f}%, {free} free)"
            )
            bits = [f"📁 {n_fold} folder(s)"]
            if n_tor:
                bits.append(f"⏬ {n_tor} transfer(s)")
            lines.append("    " + " · ".join(bits))
        except SeedrError as exc:
            account_infos.append((i, 0))
            lines.append(f"{emoji} <code>{_mask(user)}</code> — ⚠️ {exc}")
    lines += ["", "🧲 Send a magnet to auto-pick the emptiest account."]
    return "\n".join(lines), keyboards.seedr_menu(account_infos)


@Client.on_message(filters.command("seedr") & filters.private)
async def cmd_seedr(_: Client, message: Message) -> None:
    rt = get_runtime()
    if not message.from_user or message.from_user.id != rt.config.owner_id:
        return
    if not rt.config.seedr_enabled:
        await message.reply_text(
            "⚠️ Seedr is not configured. Set SEEDR_USERNAME / SEEDR_PASSWORD "
            "(comma-separate several accounts)."
        )
        return
    status = await message.reply_text("🌱 Checking Seedr accounts…")
    text, kb = await render_seedr(rt)
    await status.edit_text(text, reply_markup=kb, parse_mode=HTML,
                           disable_web_page_preview=True)
