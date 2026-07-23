"""Structured logging with secret redaction.

Every log record passes through `SecretRedactingFilter`, which masks the bot
token, API hash, Seedr password and signed Seedr download URLs so they can
never leak into console output or the log file.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

_SIGNED_URL_RE = re.compile(r"(https?://[^\s\"']*seedr[^\s\"']*?)\?[^\s\"']+", re.I)


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...]):
        super().__init__()
        # Longest first so partial overlaps redact fully.
        self._secrets = sorted((s for s in secrets if len(s) >= 6), key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for s in self._secrets:
            if s in redacted:
                redacted = redacted.replace(s, "[REDACTED]")
        redacted = _SIGNED_URL_RE.sub(r"\1?[SIGNED]", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(secrets: tuple[str, ...] = (), log_file: str = "bot.log",
                  level: int = logging.INFO) -> None:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    redactor = SecretRedactingFilter(secrets)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redactor)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_h = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024**2, backupCount=3, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    file_h.addFilter(redactor)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_h)

    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
