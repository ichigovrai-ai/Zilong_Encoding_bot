"""Rate-limited, failure-tolerant progress messages.

One `ProgressReporter` per job edits a single Telegram message. Edits are
throttled, FloodWaits push the next allowed edit forward, and a deleted
message disables further edits without ever failing the job.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class ProgressReporter:
    def __init__(self, client: Any, chat_id: int, message_id: int,
                 min_interval: float = 4.0):
        self._client = client
        self._chat_id = chat_id
        self._message_id = message_id
        self._min_interval = min_interval
        self._next_allowed = 0.0
        self._last_text = ""
        self._dead = False

    @property
    def message_id(self) -> int:
        return self._message_id

    async def update(self, text: str, reply_markup: Any = None,
                     force: bool = False) -> None:
        """Best-effort edit. Never raises."""
        if self._dead or not self._message_id:
            return
        now = time.monotonic()
        if not force and (now < self._next_allowed or text == self._last_text):
            return
        try:
            from pyrogram import enums
            await self._client.edit_message_text(
                self._chat_id, self._message_id, text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            self._last_text = text
            self._next_allowed = time.monotonic() + self._min_interval
        except Exception as exc:  # noqa: BLE001 — progress must never kill a job
            name = type(exc).__name__
            if name == "FloodWait":
                wait = float(getattr(exc, "value", 15) or 15)
                self._next_allowed = time.monotonic() + wait + 1
                log.warning("Progress edit FloodWait %.0fs (chat %d)", wait, self._chat_id)
            elif name == "MessageNotModified":
                self._last_text = text
                self._next_allowed = time.monotonic() + self._min_interval
            elif name in ("MessageIdInvalid", "MessageDeleteForbidden", "ChannelInvalid"):
                log.info("Progress message gone — disabling edits (chat %d)", self._chat_id)
                self._dead = True
            else:
                log.warning("Progress edit failed (%s): %s", name, exc)
                self._next_allowed = time.monotonic() + self._min_interval * 2


def progress_bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100))
    return "▰" * filled + "▱" * (width - filled)
