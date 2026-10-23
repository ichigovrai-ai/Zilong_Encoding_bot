"""Dump channels: copy (or forward) every file the bot uploaded to Telegram.

The bot first uploads the result to the user as usual, then re-sends that very
message to each configured channel. ``copy`` (default) is a server-side copy —
no re-upload and no "Forwarded from" header; ``forward`` keeps the header.

Best-effort by design: a broken channel (bot not admin, wrong id…) is logged
and reported, but never fails the job the user is waiting on.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Exception class name → what the owner should check.
_HINTS = {
    "ChatAdminRequired": "make the bot an admin of that channel",
    "ChatWriteForbidden": "give the bot permission to post in that channel",
    "ChannelPrivate": "add the bot to that channel (it can't see it)",
    "PeerIdInvalid": "wrong channel id, or the bot never joined that channel",
    "ChannelInvalid": "wrong channel id, or the bot never joined that channel",
    "UsernameInvalid": "wrong channel id",
}


# ── channels added at runtime (/adddump) live in the database ──────────────
async def list_channels(db: Any) -> list[tuple[int, str]]:
    rows = await db.fetchall(
        "SELECT chat_id, title FROM dump_channels ORDER BY added_at, chat_id")
    return [(int(r["chat_id"]), str(r["title"])) for r in rows]


async def add_channel(db: Any, chat_id: int, title: str, added_by: int) -> bool:
    """Store a dump channel. Returns False if it was already there."""
    if await db.fetchone("SELECT 1 FROM dump_channels WHERE chat_id = ?",
                         (chat_id,)):
        await db.execute("UPDATE dump_channels SET title = ? WHERE chat_id = ?",
                         (title, chat_id))
        return False
    await db.execute(
        "INSERT INTO dump_channels (chat_id, title, added_by, added_at) "
        "VALUES (?, ?, ?, ?)", (chat_id, title, added_by, time.time()))
    return True


async def remove_channel(db: Any, chat_id: int) -> bool:
    """Delete a stored dump channel. Returns False if it wasn't stored."""
    if not await db.fetchone("SELECT 1 FROM dump_channels WHERE chat_id = ?",
                             (chat_id,)):
        return False
    await db.execute("DELETE FROM dump_channels WHERE chat_id = ?", (chat_id,))
    return True


async def _stored_ids(db: Any) -> tuple[int, ...]:
    """Ids of the /adddump channels; a database hiccup never breaks a job."""
    if db is None:
        return ()
    try:
        return tuple(cid for cid, _ in await list_channels(db))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read dump channels from the database: %s", exc)
        return ()


def dump_targets(cfg: Any, user_id: int,
                 extra: tuple[int, ...] = ()) -> tuple[int, ...]:
    """Channels this user's uploads should be copied to (possibly none).

    ``DUMP_CHANNELS`` (env) plus ``extra`` (added with /adddump), without
    duplicates. Only the owner's uploads are dumped unless ``DUMP_ALL_USERS``
    is on, so other people's files are never copied to the owner's channels by
    surprise.
    """
    channels = tuple(dict.fromkeys((*cfg.dump_channels, *extra)))
    if not channels:
        return ()
    if user_id != cfg.owner_id and not cfg.dump_all_users:
        return ()
    return channels


def _hint(exc: Exception) -> str:
    return _HINTS.get(type(exc).__name__) or f"{type(exc).__name__}: {exc}"[:120]


async def send_to_dumps(client: Any, cfg: Any, user_id: int, sent: Any,
                        *, db: Any = None) -> list[str]:
    """Copy/forward ``sent`` (the Message returned by the upload) to the dump
    channels (env + /adddump ones when ``db`` is given). Returns a list of
    human-readable problems (empty = all good)."""
    targets = dump_targets(cfg, user_id, await _stored_ids(db))
    if not targets or sent is None:
        return []
    msg_id = getattr(sent, "id", None)
    src_chat = getattr(getattr(sent, "chat", None), "id", None)
    if msg_id is None or src_chat is None:
        log.warning("Dump skipped: the upload returned no message to copy")
        return []

    from pyrogram.errors import FloodWait

    problems: list[str] = []
    for chat_id in targets:
        for attempt in range(2):
            try:
                if cfg.dump_mode == "forward":
                    await client.forward_messages(chat_id, src_chat, msg_id)
                else:
                    await client.copy_message(chat_id, src_chat, msg_id)
                break
            except FloodWait as fw:
                wait = float(getattr(fw, "value", 10) or 10)
                if attempt == 1:
                    problems.append(f"{chat_id}: flood limit, retry later")
                    break
                log.warning("Dump FloodWait %.0fs on %s", wait, chat_id)
                await asyncio.sleep(min(wait, 60) + 1)
            except Exception as exc:  # noqa: BLE001 — never fail the job
                log.warning("Dump to %s failed: %s", chat_id, exc)
                problems.append(f"{chat_id}: {_hint(exc)}")
                break
    return problems


async def check_dump_channels(client: Any, cfg: Any, db: Any = None) -> None:
    """Startup sanity check: log which dump channels the bot can reach."""
    ids = tuple(dict.fromkeys((*cfg.dump_channels, *await _stored_ids(db))))
    for chat_id in ids:
        try:
            chat = await client.get_chat(chat_id)
            log.info("Dump channel OK: %s (%s)",
                     getattr(chat, "title", None) or chat_id, chat_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Dump channel %s NOT reachable — %s", chat_id, _hint(exc))
