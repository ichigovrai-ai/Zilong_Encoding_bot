"""Encode planning: resolution math, bitrate math, and the EncodePlan model.

The "Np" notion applies to the SHORTER side of the frame, so 720p on a
portrait 1080×1920 source gives 720×1280, on a square source 720×720, and on
a 16:9 source the classic 1280×720. Aspect ratio is always preserved; output
dimensions are always even; upscaling never happens unless explicitly allowed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bot.utils.validation import ValidationError

# name → shorter-side target
RESOLUTIONS: dict[str, int] = {
    "360p": 360, "480p": 480, "720p": 720,
    "1080p": 1080, "1440p": 1440, "2160p": 2160,
}

VIDEO_CODECS = {
    "libx264": "H.264", "libx265": "H.265 / HEVC",
    "libsvtav1": "AV1", "copy": "Copy",
}
AUDIO_CODECS = {"aac": "AAC", "libopus": "Opus", "copy": "Copy"}

# Containers that can hold each video codec without drama.
CODEC_CONTAINERS = {
    "libx264": ("mp4", "mkv"),
    "libx265": ("mp4", "mkv"),
    "libsvtav1": ("mkv", "mp4"),
    "copy": ("mkv",),
}

MIN_VIDEO_KBPS = 50
MAX_REASONABLE_KBPS = 200_000


@dataclass(slots=True)
class ScaleDecision:
    width: int | None          # None → keep original
    height: int | None
    upscale_needed: bool = False   # requested > source; needs user confirmation
    letterbox: tuple[int, int] | None = None  # pad to exact WxH if requested


def even(n: int) -> int:
    return max(2, n - (n % 2))


def compute_dimensions(
    src_w: int, src_h: int, target_short: int, *, allow_upscale: bool = False
) -> ScaleDecision:
    """Scale so the shorter side hits `target_short`, preserving aspect."""
    if src_w <= 0 or src_h <= 0:
        raise ValidationError("Source dimensions are unknown — cannot scale.")
    short = min(src_w, src_h)
    if target_short >= short:
        if target_short == short or not allow_upscale:
            return ScaleDecision(None, None, upscale_needed=target_short > short)
    factor = target_short / short
    return ScaleDecision(even(round(src_w * factor)), even(round(src_h * factor)))


def compute_exact_dimensions(
    src_w: int, src_h: int, out_w: int, out_h: int, *,
    allow_upscale: bool = False, letterbox: bool = False,
) -> ScaleDecision:
    """Fit into an exact WxH box. Without letterbox, the box is a bounding box
    (aspect preserved, one dimension may shrink); with letterbox, the frame is
    padded to exactly WxH."""
    if src_w <= 0 or src_h <= 0:
        raise ValidationError("Source dimensions are unknown — cannot scale.")
    factor = min(out_w / src_w, out_h / src_h)
    upscale_needed = factor > 1.0
    if upscale_needed and not allow_upscale:
        return ScaleDecision(None, None, upscale_needed=True)
    factor = min(factor, 1.0) if not allow_upscale else factor
    w, h = even(round(src_w * factor)), even(round(src_h * factor))
    return ScaleDecision(
        w, h, upscale_needed=upscale_needed,
        letterbox=(even(out_w), even(out_h)) if letterbox else None,
    )


def calc_video_bitrate_kbps(
    target_bytes: int, duration_s: float, audio_kbps: int, *, overhead: float = 0.03
) -> int:
    """Video bitrate that fits `target_bytes` including audio and container
    overhead. Raises ValidationError for impossible targets."""
    if duration_s <= 0:
        raise ValidationError("Cannot compute a target size: media duration is unknown.")
    total_kbps = (target_bytes * 8 / 1000) / duration_s
    video_kbps = int(total_kbps * (1 - overhead)) - audio_kbps
    if video_kbps < MIN_VIDEO_KBPS:
        need_mb = int((MIN_VIDEO_KBPS + audio_kbps) * duration_s * 1000 / 8 / 1024**2) + 1
        raise ValidationError(
            f"Target size is too small for this duration — "
            f"needs at least ~{need_mb} MB."
        )
    if video_kbps > MAX_REASONABLE_KBPS:
        raise ValidationError("Target size is unreasonably large for this video.")
    return video_kbps


def estimate_size_bytes(video_kbps: int, audio_kbps: int, duration_s: float) -> int:
    return int((video_kbps + audio_kbps) * 1000 / 8 * duration_s * 1.03)


@dataclass(slots=True)
class EncodePlan:
    """Everything the FFmpeg builder needs, resolved and validated."""
    video_codec: str = "libx264"
    mode: str = "crf"                # crf | size | bitrate | copy
    crf: int = 23
    preset: str = "medium"
    video_bitrate_kbps: int = 0
    two_pass: bool = False
    width: int | None = None
    height: int | None = None
    letterbox: tuple[int, int] | None = None
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 128
    audio_stream: int | None = None  # type-index; None → first
    container: str = "mp4"
    faststart: bool = True
    # Subtitles
    subtitle_mode: str = "none"      # none | hard | soft | strip
    subtitle_path: str = ""          # prepared local file (hard/soft)
    subtitle_stream: int | None = None  # embedded subtitle type-index for hard burn
    fonts_dir: str = ""
    deinterlace: bool = False

    @property
    def target_height_label(self) -> int | None:
        if self.height and self.width:
            return min(self.width, self.height)
        return None

    def summary(self) -> str:
        res = f"{self.width}×{self.height}" if self.width else "original"
        if self.mode == "crf":
            rate = f"CRF {self.crf}"
        elif self.mode == "size":
            rate = f"{self.video_bitrate_kbps} kbps (target size)"
        elif self.mode == "bitrate":
            rate = f"{self.video_bitrate_kbps} kbps"
        else:
            rate = "stream copy"
        return f"{self.video_codec} · {res} · {rate} · preset {self.preset}"
