"""Thumbnail generation for Telegram uploads (320px thumb + optional preview)."""
from __future__ import annotations

import logging
from pathlib import Path

from bot.utils.subprocesses import CancelToken, run_process

log = logging.getLogger(__name__)


async def make_thumbnail(
    video_path: str, out_dir: str | Path, duration_s: float, *,
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
) -> str | None:
    """Grab a frame ~10% in (past intros/black frames), 320px wide JPEG,
    as required for Telegram's thumb parameter. Best-effort."""
    out = Path(out_dir) / "thumb.jpg"
    ts = max(1.0, duration_s * 0.1) if duration_s > 3 else 0.0
    args = [
        ffmpeg, "-hide_banner", "-y",
        "-ss", f"{ts:.2f}", "-i", video_path,
        "-frames:v", "1",
        "-vf", "scale=320:-2:flags=lanczos",
        "-q:v", "4", str(out),
    ]
    try:
        code, _, _ = await run_process(args, timeout=60, cancel_token=cancel_token)
        if code == 0 and out.exists() and out.stat().st_size > 0:
            return str(out)
    except Exception as exc:  # noqa: BLE001 — thumbnails are optional
        log.warning("Thumbnail generation failed: %s", exc)
    return None
