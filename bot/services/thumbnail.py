"""Smart HD thumbnails for Telegram uploads.

Produces two images from one intelligently-chosen frame:
  • cover  — an HD poster (up to 1280px wide, high quality) shown as the
             video's cover via PyroFork's send_video(cover=…)
  • thumb  — a Telegram-compliant ≤320px JPEG for send_video(thumb=…)

The frame comes from a RANDOM moment of the video (a different one on every
upload). Around that moment FFmpeg's `thumbnail` filter scores a window of
frames and picks the most representative one — this skips black frames,
fades and blurry motion without needing Pillow. The first and last few
seconds are never picked, so logos / credits are avoided.
"""
from __future__ import annotations

import logging
import random
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


def fixed_seek(duration_s: float) -> float:
    """The old deterministic choice: ~12% in (min 3s) to skip logos/black."""
    return max(3.0, duration_s * 0.12) if duration_s > 8 else 0.0


def pick_seek(duration_s: float, rng: random.Random | None = None) -> float:
    """A random timestamp (seconds) anywhere in the video.

    The first and last few seconds are excluded (logos, black fades, credits),
    and the end keeps ~6s free so the 60-frame `thumbnail` window fits.
    Videos of 8s or less simply use the start.
    """
    if duration_s <= 8:
        return 0.0
    margin = max(6.0, duration_s * 0.04)
    lo = max(3.0, duration_s * 0.04)
    hi = max(lo, duration_s - margin)
    return round((rng or random).uniform(lo, hi), 2)


def seek_chain(duration_s: float, randomize: bool = True,
               rng: random.Random | None = None) -> list[float]:
    """Positions to try for the HD cover, best first.

    A random moment first, then ever safer ones. The reported duration can be
    longer than the video track itself (audio running past the video, a
    container header that overstates the length): seeking past the end of the
    video makes FFmpeg write nothing, so each fallback is closer to the start,
    and 0 — always valid — closes the chain.
    """
    candidates = [pick_seek(duration_s, rng)] if randomize else []
    candidates += [fixed_seek(duration_s), round(duration_s * 0.05, 2), 3.0, 0.0]
    chain: list[float] = []
    for s in candidates:
        if s < 0 or (s > 0 and duration_s > 0 and s >= duration_s - 1):
            continue
        if all(abs(s - c) >= 0.1 for c in chain):
            chain.append(s)
    return chain or [0.0]


async def make_thumbnails(
    video_path: str, out_dir: str | Path, duration_s: float, *,
    media: MediaInfo | None = None,
    ffmpeg: str = "ffmpeg", cancel_token: CancelToken | None = None,
    randomize: bool = True, rng: random.Random | None = None,
) -> ThumbSet:
    """Generate an HD cover + a 320px thumbnail from a random frame of the
    video (``randomize=False`` keeps the old fixed ~12% position).
    Best-effort — returns an empty ThumbSet on any failure so uploads never
    break over a missing preview."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cover = out_dir / "cover.jpg"
    thumb = out_dir / "thumb.jpg"

    chain = seek_chain(duration_s, randomize, rng)

    # thumbnail=n=60 → pick the best of ~60 frames; scale caps width at 1280
    # without upscaling (min(1280,iw)); HDR gets tone-mapped to SDR so the
    # poster isn't washed out.
    vf = "thumbnail=n=60,"
    if _is_hdr(media):
        vf += ("zscale=t=linear:npl=100,tonemap=hable,"
               "zscale=t=bt709:m=bt709:r=tv,")
    vf += "scale='min(1280,iw)':-2:flags=lanczos,format=yuv420p"

    async def _make_cover(at: float) -> bool:
        cover.unlink(missing_ok=True)
        cover_args = [
            ffmpeg, "-hide_banner", "-y", "-nostdin",
            "-ss", f"{at:.2f}", "-i", video_path,
            "-an", "-sn", "-dn",
            "-frames:v", "1", "-vf", vf, "-q:v", "2", str(cover),
        ]
        try:
            code, _, err = await run_process(cover_args, timeout=90,
                                             cancel_token=cancel_token)
            if code == 0 and cover.exists() and cover.stat().st_size > 0:
                return True
            log.info("Cover generation failed at %.1fs (%s)", at,
                     err[-200:] if err else "")
        except Exception as exc:  # noqa: BLE001 — previews are optional
            log.warning("Cover generation error: %s", exc)
        return False

    result = ThumbSet()
    seek = chain[-1]
    for attempt, at in enumerate(chain):
        if await _make_cover(at):
            result.cover = str(cover)
            seek = at
            if attempt:
                log.info("HD cover made at %.1fs after %d failed position(s)",
                         at, attempt)
            break
    else:
        log.warning("No HD cover for %s: all %d positions failed (duration "
                    "reported %.1fs) — falling back to the small thumbnail",
                    Path(video_path).name, len(chain), duration_s)

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
