"""Owner commands to manage the dump channels from inside the bot:

    /adddump    <id|@channel|t.me link>   start copying every upload there
    /removedump <id|@channel|t.me link>   stop
    /dumps                                list the dump channels

Instead of typing an id you can also forward any post of the channel to the
bot and reply to it with /adddump. The bot must already be an admin of the
channel (with permission to post) — it is checked before the channel is saved.
Channels from the DUMP_CHANNELS setting always stay active; only the ones added
here can be removed here.
"""
from __future__ import annotations

import html
import logging
import re

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from bot.runtime import get_runtime
from bot.services import dump
from bot.ui.panels import SEP

log = logging.getLogger(__name__)

HTML = enums.ParseMode.HTML
_LINK_RE = re.compile(r"^(?:https?://)?t\.me/([A-Za-z]\w{3,})/?$")
_CHAT_TYPES = ("CHANNEL", "SUPERGROUP", "GROUP")


def _is_owner(message: Message) -> bool:
    rt = get_runtime()
    return bool(message.from_user and message.from_user.id == rt.config.owner_id)


def _name(value: object) -> str:
    """'CHANNEL' for ChatType.CHANNEL / 'channel' / 'ChatType.CHANNEL'."""
    return str(getattr(value, "name", value)).split(".")[-1].upper()


def _forwarded_chat(msg: Message | None):
    if msg is None:
        return None
    chat = getattr(msg, "forward_from_chat", None)
    if chat is None:
        chat = getattr(getattr(msg, "forward_origin", None), "chat", None)
    return chat


def _target(message: Message) -> int | str | None:
    """Channel id / username from the argument, a t.me link, or the forwarded
    channel post this command replies to."""
    args = message.command[1:]
    if args:
        raw = args[0].strip()
        if raw.lstrip("-").isdigit():
            return int(raw)
        link = _LINK_RE.match(raw)
        return link.group(1) if link else raw.lstrip("@")
    chat = _forwarded_chat(getattr(message, "reply_to_message", None))
    return chat.id if chat is not None else None


async def _bot_problem(client, chat) -> str | None:
    """Why the bot can't post in ``chat`` (None = it can)."""
    try:
        me = await client.get_chat_member(chat.id, "me")
    except Exception as exc:  # noqa: BLE001
        log.info("Membership check failed for %s: %s", chat.id, exc)
        return "I'm not a member of that chat. Add me as an admin first."
    if _name(getattr(me, "status", "")) not in ("ADMINISTRATOR", "OWNER"):
        return "I'm not an admin there. Make me an admin that can post messages."
    privileges = getattr(me, "privileges", None)
    if (_name(chat.type) == "CHANNEL" and privileges is not None
            and getattr(privileges, "can_post_messages", True) is False):
        return "I'm an admin there but can't post. Enable “Post messages”."
    return None


_USAGE = (
    "<b>Usage:</b> <code>/{cmd} &lt;id|@channel&gt;</code>\n\n"
    "Examples:\n<code>/{cmd} -1001234567890</code>\n"
    "<code>/{cmd} @mychannel</code>\n\n"
    "<i>You can also forward a post of the channel to me and reply to it with "
    "/{cmd}.</i>"
)


@Client.on_message(filters.command("adddump") & filters.private)
async def cmd_adddump(_: Client, message: Message) -> None:
    if not _is_owner(message):
        return
    rt = get_runtime()
    target = _target(message)
    if target is None:
        await message.reply_text(_USAGE.format(cmd="adddump"), parse_mode=HTML)
        return
    try:
        chat = await rt.client.get_chat(target)
    except Exception as exc:  # noqa: BLE001
        log.info("get_chat(%r) failed: %s", target, exc)
        await message.reply_text(
            "❌ I can't find that chat. Add me to the channel as an admin, then "
            "use its numeric id (like <code>-1001234567890</code>) — private "
            "channels can't be found by link.", parse_mode=HTML)
        return
    if _name(chat.type) not in _CHAT_TYPES:
        await message.reply_text("❌ That's not a channel or a group.")
        return
    problem = await _bot_problem(rt.client, chat)
    if problem:
        await message.reply_text(f"❌ {problem}")
        return

    title = getattr(chat, "title", None) or str(chat.id)
    added = await dump.add_channel(rt.db, chat.id, title, message.from_user.id)
    note = ""
    if chat.id in rt.config.dump_channels:
        note = "\n<i>It's also in DUMP_CHANNELS — it only counts once.</i>"
    scope = ("everyone's uploads" if rt.config.dump_all_users
             else "your uploads only")
    await message.reply_text(
        f"✅ <b>{'Added' if added else 'Already added'}:</b> "
        f"{html.escape(title)} (<code>{chat.id}</code>)\n"
        f"Mode: <b>{rt.config.dump_mode}</b> · {scope}{note}",
        parse_mode=HTML)


@Client.on_message(filters.command("removedump") & filters.private)
async def cmd_removedump(_: Client, message: Message) -> None:
    if not _is_owner(message):
        return
    rt = get_runtime()
    target = _target(message)
    if target is None:
        await message.reply_text(
            await _listing(rt) + "\n\n" + _USAGE.format(cmd="removedump"),
            parse_mode=HTML)
        return
    chat_id = target if isinstance(target, int) else None
    if chat_id is None:
        try:
            chat_id = (await rt.client.get_chat(target)).id
        except Exception as exc:  # noqa: BLE001
            log.info("get_chat(%r) failed: %s", target, exc)
            await message.reply_text("❌ I can't find that chat — use its "
                                     "numeric id (see /dumps).")
            return
    removed = await dump.remove_channel(rt.db, chat_id)
    in_env = chat_id in rt.config.dump_channels
    if removed and in_env:
        text = (f"🗑 Removed <code>{chat_id}</code> from the list, but it is "
                "also in DUMP_CHANNELS, so it stays active until you remove "
                "it from that setting.")
    elif removed:
        text = f"🗑 <b>Removed</b> <code>{chat_id}</code>. Uploads won't be copied there anymore."
    elif in_env:
        text = (f"⚠️ <code>{chat_id}</code> comes from the DUMP_CHANNELS "
                "setting — remove it there (launcher / .env).")
    else:
        text = f"❌ <code>{chat_id}</code> isn't a dump channel. See /dumps."
    await message.reply_text(text, parse_mode=HTML)


async def _listing(rt) -> str:
    stored = await dump.list_channels(rt.db)
    titles = {cid: t for cid, t in stored}
    rows = [(cid, titles.get(cid, ""), "setting") for cid in rt.config.dump_channels]
    rows += [(cid, t, "added") for cid, t in stored
             if cid not in rt.config.dump_channels]
    if not rows:
        return ("📦 <b>DUMP CHANNELS</b>\n" + SEP + "\nNone yet — add one with "
                "<code>/adddump &lt;id|@channel&gt;</code>.")
    lines = ["📦 <b>DUMP CHANNELS</b>", SEP]
    for cid, title, origin in rows:
        label = html.escape(title) if title else "—"
        tag = " <i>(setting)</i>" if origin == "setting" else ""
        lines.append(f"• {label} · <code>{cid}</code>{tag}")
    scope = "everyone's uploads" if rt.config.dump_all_users else "your uploads only"
    lines.append(f"\nMode: <b>{rt.config.dump_mode}</b> · {scope}")
    return "\n".join(lines)


@Client.on_message(filters.command("dumps") & filters.private)
async def cmd_dumps(_: Client, message: Message) -> None:
    if not _is_owner(message):
        return
    await message.reply_text(await _listing(get_runtime()), parse_mode=HTML)
