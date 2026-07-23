"""Telegram uploads: video vs document decision, thumbnails, captions,
FloodWait tolerance and progress reporting."""
from __future__ import annotations

import asyncio
import logging
import time
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable

from bot.services.ffprobe import MediaInfo
from bot.utils.timefmt import fmt_hms, human_size

log = logging.getLogger(__name__)

ProgressCB = Callable[[int, int], Awaitable[None]]


class UploadError(RuntimeError):
    pass


def should_send_as_video(path: Path, media: MediaInfo | None,
                         user_pref: str = "auto") -> bool:
    """Telegram streams H.264/HEVC MP4 nicely; everything else goes as a
    document so the container/codec survive byte-for-byte."""
    if user_pref == "video":
        return True
    if user_pref == "document":
        return False
    if path.suffix.lower() not in (".mp4", ".mov"):
        return False
    if media and media.main_video and media.main_video.codec not in ("h264", "hevc"):
        return False
    return True


def build_caption(*, original_name: str, out_path: Path,
                  media: MediaInfo | None, plan_summary: str,
                  encode_seconds: float, template: str = "") -> str:
    size = out_path.stat().st_size if out_path.exists() else 0
    if template:
        # User templates use {name}, {size}, {resolution}, {codec}, {duration}
        res = ""
        codec = ""
        if media and media.main_video:
            res = f"{media.main_video.width}×{media.main_video.height}"
            codec = media.main_video.codec
        return template.format(
            name=escape(original_name), size=human_size(size),
            resolution=res, codec=codec,
            duration=fmt_hms(media.duration) if media else "",
        )[:1024]
    lines = [f"<b>{escape(out_path.name)}</b>"]
    if plan_summary:
        lines.append(escape(plan_summary))
    lines.append(f"{human_size(size)} · encoded in {fmt_hms(encode_seconds)}")
    return "\n".join(lines)[:1024]


async def upload_file(
    client: Any, chat_id: int, path: Path, *,
    media: MediaInfo | None,
    caption: str,
    thumb: str | None = None,
    as_video: bool = True,
    progress_cb: ProgressCB | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Any:
    """Send the encoded file. Retries FloodWaits, falls back video→document."""
    loop = asyncio.get_running_loop()
    last: dict[str, float] = {"t": 0.0}

    def _sync_progress(current: int, total: int) -> None:
        if cancelled and cancelled():
            raise asyncio.CancelledError("job cancelled")
        now = time.monotonic()
        if progress_cb and now - last["t"] >= 2.0:
            last["t"] = now
            asyncio.run_coroutine_threadsafe(progress_cb(current, total), loop)

    from pyrogram import enums
    from pyrogram.errors import FloodWait

    kwargs: dict[str, Any] = dict(
        caption=caption, parse_mode=enums.ParseMode.HTML,
        progress=_sync_progress,
    )
    if thumb:
        kwargs["thumb"] = thumb

    for attempt in range(3):
        try:
            if as_video:
                duration = int(media.duration) if media else 0
                v = media.main_video if media else None
                return await client.send_video(
                    chat_id, str(path), duration=duration,
                    width=v.width if v else 0, height=v.height if v else 0,
                    supports_streaming=True, **kwargs,
                )
            return await client.send_document(
                chat_id, str(path), force_document=True, **kwargs
            )
        except FloodWait as fw:
            wait = float(getattr(fw, "value", 30) or 30)
            if attempt == 2:
                raise UploadError(f"Telegram flood limit — retry in {wait:.0f}s") from fw
            log.warning("Upload FloodWait %.0fs (attempt %d)", wait, attempt + 1)
            await asyncio.sleep(wait + 2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if as_video and attempt == 0:
                log.warning("send_video failed (%s) — retrying as document", exc)
                as_video = False
                continue
            raise UploadError(f"Telegram upload failed: {exc}") from exc
    raise UploadError("Telegram upload failed after retries.")
