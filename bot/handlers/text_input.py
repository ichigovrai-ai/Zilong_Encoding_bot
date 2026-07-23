"""Plain-text routing: awaited wizard values, magnet links, and direct URLs."""
from __future__ import annotations

import logging
import re

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from bot.handlers import intake, wizard
from bot.runtime import get_runtime
from bot.utils.validation import MAGNET_IN_TEXT, ValidationError

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+", re.I)


@Client.on_message(filters.text & filters.private & ~filters.command([
    "start", "help", "encode", "compress", "convert", "hardsub", "seedr",
    "mediainfo", "extract", "extractsub", "extractaudio", "convertsub",
    "presets", "settings", "queue", "status", "cancel", "history",
    "grant", "revoke", "subs", "subscriptions", "access", "myaccess",
]), group=2)
async def on_text(_: Client, message: Message) -> None:
    rt = get_runtime()
    text = (message.text or "").strip()
    if not text or message.from_user is None:
        return

    # 1) Wizard waiting for a typed value?
    job = await rt.repo.awaiting_job_for_user(message.from_user.id)
    if job is not None and job.options.get("await") in ("crf", "size", "res",
                                                        "bitrate"):
        try:
            wtext, kb = await wizard.apply_awaited_text(rt, job, text)
        except ValidationError as exc:
            await message.reply_text(f"⚠️ {exc}")
            return
        except ValueError:
            job = None  # fall through to link detection
        else:
            try:
                await rt.client.edit_message_text(
                    job.chat_id, job.status_msg_id, wtext,
                    reply_markup=kb, parse_mode=enums.ParseMode.HTML,
                )
            except Exception:  # noqa: BLE001
                await message.reply_text(wtext, reply_markup=kb,
                                         parse_mode=enums.ParseMode.HTML)
            return

    # 2) Magnet link → Seedr flow (works without /seedr)
    if m := MAGNET_IN_TEXT.search(text):
        await intake.intake_magnet(rt, message, m.group(0))
        return

    # 3) Direct link → media intake
    if m := _URL_RE.search(text):
        await intake.intake_url(rt, message, m.group(0))
        return
