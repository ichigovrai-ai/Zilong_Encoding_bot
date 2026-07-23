"""Owner admin commands for paid, time-limited access:

    /grant  <user_id|@username> <days> [note]   grant or extend access
    /revoke <user_id|@username>                 remove access
    /subs                                        list active subscriptions
    /access                                      any user checks their status

Grants stack: /grant on an already-active user adds to their remaining time.
Users are resolved by numeric id or public @username; the numeric id is what
is stored, so a later username change does not lose the grant.
"""
from __future__ import annotations

import logging

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from bot.runtime import get_runtime
from bot.services import access
from bot.ui.panels import SEP
from bot.utils.timefmt import fmt_date, fmt_days

log = logging.getLogger(__name__)

HTML = enums.ParseMode.HTML


def _is_owner(message: Message) -> bool:
    rt = get_runtime()
    return bool(message.from_user and message.from_user.id == rt.config.owner_id)


async def _resolve(rt, identifier: str) -> tuple[int, str] | None:
    """Resolve '@username' or a numeric id to (user_id, username).

    A numeric id that Telegram can't look up is still accepted (the owner may
    grant access before the user has ever contacted the bot). An unresolvable
    username returns None — we need a real id to store."""
    identifier = identifier.strip()
    numeric = identifier.lstrip("-").isdigit()
    try:
        user = await rt.client.get_users(int(identifier) if numeric
                                         else identifier.lstrip("@"))
        return user.id, (user.username or "")
    except Exception as exc:  # noqa: BLE001
        log.info("User resolve failed for %r: %s", identifier, exc)
        if numeric:
            return int(identifier), ""
        return None


@Client.on_message(filters.command("grant") & filters.private)
async def cmd_grant(_: Client, message: Message) -> None:
    if not _is_owner(message):
        return
    rt = get_runtime()
    parts = message.command
    if len(parts) < 3:
        await message.reply_text(
            "<b>Usage:</b> <code>/grant &lt;user_id|@username&gt; &lt;days&gt; "
            "[note]</code>\n\n"
            "Examples:\n"
            "<code>/grant @someone 30 paid $5</code>\n"
            "<code>/grant 123456789 7</code>\n\n"
            "<i>Granting an already-active user adds to their remaining time.</i>",
            parse_mode=HTML,
        )
        return
    try:
        days = int(parts[2])
        if not 1 <= days <= 3650:
            raise ValueError
    except ValueError:
        await message.reply_text("Days must be a whole number between 1 and 3650.")
        return
    note = " ".join(parts[3:])[:200]

    resolved = await _resolve(rt, parts[1])
    if resolved is None:
        await message.reply_text(
            "Couldn't resolve that user. Use their numeric id, or a valid "
            "public @username."
        )
        return
    uid, uname = resolved
    prev = await access.get_subscription(rt.db, uid)
    sub = await access.grant(rt.db, uid, uname, days, message.from_user.id, note)
    extended = bool(prev and prev.active)

    who = f"@{uname}" if uname else f"<code>{uid}</code>"
    await message.reply_text(
        f"✅ <b>Access {'extended' if extended else 'granted'}</b>\n"
        f"{SEP}\n"
        f"👤 {who} (<code>{uid}</code>)\n"
        f"➕ {days} day(s){' added' if extended else ''}\n"
        f"📅 Active until <b>{fmt_date(sub.expires_at)}</b> "
        f"({fmt_days(sub.seconds_left)} left)\n"
        + (f"📝 {note}\n" if note else ""),
        parse_mode=HTML,
    )
    # Best-effort DM to the user (works if they've started the bot).
    try:
        await rt.client.send_message(
            uid,
            f"🎉 <b>You've been granted access!</b>\n"
            f"{SEP}\n"
            f"⏳ {days} day(s){' added to your subscription' if extended else ''}\n"
            f"📅 Active until <b>{fmt_date(sub.expires_at)}</b>\n\n"
            "Send /start to begin. 🎬",
            parse_mode=HTML,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("Could not DM granted user %s: %s", uid, exc)
        await message.reply_text(
            "ℹ️ Couldn't notify them directly — they need to have started the "
            "bot first. Their access is active regardless."
        )


@Client.on_message(filters.command("revoke") & filters.private)
async def cmd_revoke(_: Client, message: Message) -> None:
    if not _is_owner(message):
        return
    rt = get_runtime()
    if len(message.command) < 2:
        await message.reply_text(
            "<b>Usage:</b> <code>/revoke &lt;user_id|@username&gt;</code>",
            parse_mode=HTML,
        )
        return
    resolved = await _resolve(rt, message.command[1])
    if resolved is None:
        await message.reply_text("Couldn't resolve that user.")
        return
    uid, uname = resolved
    ok = await access.revoke(rt.db, uid)
    who = f"@{uname}" if uname else f"<code>{uid}</code>"
    await message.reply_text(
        f"🚫 Access revoked for {who}." if ok
        else f"{who} had no active grant.",
        parse_mode=HTML,
    )


@Client.on_message(filters.command(["subs", "subscriptions"]) & filters.private)
async def cmd_subs(_: Client, message: Message) -> None:
    if not _is_owner(message):
        return
    rt = get_runtime()
    subs = await access.list_active(rt.db)
    lines = ["💳 <b>ACTIVE SUBSCRIPTIONS</b>", SEP]
    if not subs:
        lines.append("<i>None active.</i>")
    else:
        for s in subs:
            who = f"@{s.username}" if s.username else f"<code>{s.user_id}</code>"
            note = f" · {s.note}" if s.note else ""
            lines.append(
                f"👤 {who} — <b>{fmt_days(s.seconds_left)}</b> left "
                f"(until {fmt_date(s.expires_at)}){note}"
            )
    lines += ["", "<code>/grant</code> to add · <code>/revoke</code> to remove"]
    await message.reply_text("\n".join(lines), parse_mode=HTML)


@Client.on_message(filters.command(["access", "myaccess"]) & filters.private)
async def cmd_access(_: Client, message: Message) -> None:
    rt = get_runtime()
    if message.from_user is None:
        return
    uid = message.from_user.id
    sub = await access.get_subscription(rt.db, uid)
    await message.reply_text(
        "🔑 <b>YOUR ACCESS</b>\n" + SEP + "\n"
        + access.status_text(rt, uid, sub),
        parse_mode=HTML,
    )
