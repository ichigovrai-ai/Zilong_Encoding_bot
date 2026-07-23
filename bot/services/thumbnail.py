"""Smart HD thumbnails for Telegram uploads.

Produces two images from one intelligently-chosen frame:
  • cover  — an HD poster (up to 1280px wide, high quality) shown as the
             video's cover via PyroFork's send_video(cover=…)
  • thumb  — a Telegram-compliant ≤320px JPEG for send_video(thumb=…)

Frame selection uses FFmpeg's `thumbnail` filter, which scores a window of
frames and picks the most representative one — this skips black intros,
fades and blurry motion frames without needing Pillow. The seek starts a
little into the file so title cards / black frames at 0:00 are avoided.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from bot.services.ffprobe import MediaInfo
from bot.utils.subprocesses import CancelToken, run_process

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ThumbSet:
    thumb: str | None = None   # ≤320px JPEG for Telegram's thumb param
    cover: str | None = None   # HD poster for send_video(cover=…)


def _is_hdr(media: MediaInfo | None) -> bool:
    if media is None or not media.video:
        return False
    # 10-bit pixel formats are the common HDR signal we can see from ffprobe.
    return "p10" in (media.video[0].pix_fmt or "").lower()


async def make_thumbnails(
    video_path: str, out_dir: str | Path, duration_s: float, *,
    media: MediaInfo | None = None,
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
) -> ThumbSet:
    """Generate an HD cover + a 320px thumbnail. Best-effort — returns an empty
    ThumbSet on any failure so uploads never break over a missing preview."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cover = out_dir / "cover.jpg"
    thumb = out_dir / "thumb.jpg"

    # Seek ~12% in (min 3s) so we skip logos/black at the very start.
    seek = max(3.0, duration_s * 0.12) if duration_s > 8 else 0.0

    # thumbnail=n=60 → pick the best of ~60 frames; scale caps width at 1280
    # without upscaling (min(1280,iw)); HDR gets tone-mapped to SDR so the
    # poster isn't washed out.
    vf = "thumbnail=n=60,"
    if _is_hdr(media):
        vf += ("zscale=t=linear:npl=100,tonemap=hable,"
               "zscale=t=bt709:m=bt709:r=tv,")
    vf += "scale='min(1280,iw)':-2:flags=lanczos,format=yuv420p"

    cover_args = [
        ffmpeg, "-hide_banner", "-y", "-nostdin",
        "-ss", f"{seek:.2f}", "-i", video_path,
        "-frames:v", "1", "-vf", vf, "-q:v", "2", str(cover),
    ]
    result = ThumbSet()
    try:
        code, _, err = await run_process(cover_args, timeout=90,
                                         cancel_token=cancel_token)
        if code == 0 and cover.exists() and cover.stat().st_size > 0:
            result.cover = str(cover)
        else:
            log.info("Cover generation failed (%s)", err[-200:] if err else "")
    except Exception as exc:  # noqa: BLE001 — previews are optional
        log.warning("Cover generation error: %s", exc)

    # Telegram thumb: derive the ≤320px version from the cover we just made
    # (fast, and guarantees the same frame). Fall back to the source.
    thumb_src = result.cover or video_path
    thumb_args = [
        ffmpeg, "-hide_banner", "-y", "-nostdin",
    ]
    if not result.cover:
        thumb_args += ["-ss", f"{seek:.2f}"]
    thumb_args += ["-i", thumb_src, "-frames:v", "1",
                   "-vf", "scale=320:-2:flags=lanczos", "-q:v", "4", str(thumb)]
    try:
        code, _, _ = await run_process(thumb_args, timeout=45,
                                       cancel_token=cancel_token)
        if code == 0 and thumb.exists() and 0 < thumb.stat().st_size < 200_000:
            result.thumb = str(thumb)
    except Exception as exc:  # noqa: BLE001
        log.warning("Thumbnail generation error: %s", exc)
    return result
