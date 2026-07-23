"""Streaming downloads: HTTP(S) via aiohttp (chunked to disk, never in RAM)
and Telegram media via pyrogram, both with size limits, progress callbacks
and cancellation."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from bot.utils.subprocesses import CancelToken

log = logging.getLogger(__name__)

_CHUNK = 256 * 1024
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ProgressCB = Callable[[int, int], Awaitable[None]]  # (done_bytes, total_bytes)


class DownloadError(RuntimeError):
    pass


class FileTooLarge(DownloadError):
    def __init__(self, size: int, limit: int):
        super().__init__(
            f"File is too large ({size // 1024**2} MB; limit "
            f"{limit // 1024**2} MB)."
        )


async def download_url(
    url: str, dest: str | Path, *,
    max_bytes: int,
    progress_cb: ProgressCB | None = None,
    cancel_token: CancelToken | None = None,
    timeout_s: float = 3600.0,
    progress_interval: float = 2.0,
) -> Path:
    """Stream a URL to `dest`. Enforces `max_bytes` from headers AND while
    streaming (servers lie). Removes partial files on failure/cancel."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_read=120)
    done = 0
    last_report = 0.0
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": _UA}, timeout=timeout
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    raise DownloadError(f"Server answered HTTP {resp.status}.")
                total = int(resp.headers.get("Content-Length") or 0)
                if total > max_bytes:
                    raise FileTooLarge(total, max_bytes)
                with dest.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(_CHUNK):
                        if cancel_token:
                            cancel_token.raise_if_cancelled()
                        fh.write(chunk)
                        done += len(chunk)
                        if done > max_bytes:
                            raise FileTooLarge(done, max_bytes)
                        now = time.monotonic()
                        if progress_cb and now - last_report >= progress_interval:
                            last_report = now
                            await progress_cb(done, total)
        if done == 0:
            raise DownloadError("The server sent an empty file.")
        if progress_cb:
            await progress_cb(done, total or done)
        return dest
    except (DownloadError, asyncio.CancelledError):
        dest.unlink(missing_ok=True)
        raise
    except aiohttp.ClientError as exc:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"Download failed: {exc}") from exc
    except asyncio.TimeoutError:
        dest.unlink(missing_ok=True)
        raise DownloadError("Download timed out.") from None


async def download_telegram_media(
    client: Any, message: Any, dest: str | Path, *,
    max_bytes: int,
    progress_cb: ProgressCB | None = None,
    cancel_token: CancelToken | None = None,
) -> Path:
    """Download a Telegram video/document with progress + cancellation."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    media = message.video or message.document
    if media and getattr(media, "file_size", 0) and media.file_size > max_bytes:
        raise FileTooLarge(media.file_size, max_bytes)

    loop = asyncio.get_running_loop()
    last: dict[str, float] = {"t": 0.0}

    def _sync_progress(current: int, total: int) -> None:
        # pyrogram calls this from its own executor thread
        if cancel_token and cancel_token.cancelled:
            raise asyncio.CancelledError("job cancelled")
        now = time.monotonic()
        if progress_cb and now - last["t"] >= 2.0:
            last["t"] = now
            asyncio.run_coroutine_threadsafe(
                progress_cb(current, total), loop
            )

    try:
        result = await client.download_media(
            message, file_name=str(dest), progress=_sync_progress
        )
    except asyncio.CancelledError:
        dest.unlink(missing_ok=True)
        raise
    if not result:
        raise DownloadError("Telegram download failed — no file was produced.")
    path = Path(result)
    if not path.exists() or path.stat().st_size == 0:
        raise DownloadError("Telegram download produced an empty file.")
    return path
