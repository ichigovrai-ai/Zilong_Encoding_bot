"""Turn wizard selections (job.options) into a validated EncodePlan.

Pure logic — fully unit-testable. The worker calls this after probing, when
real dimensions/duration are known.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bot.services.encoding import (
    EncodePlan,
    calc_video_bitrate_kbps,
    compute_dimensions,
    compute_exact_dimensions,
)
from bot.services.ffprobe import MediaInfo
from bot.utils.validation import ValidationError

_CODEC_MAP = {
    "x264": "libx264", "x265": "libx265", "av1": "libsvtav1", "copy": "copy",
}
# Balanced CRF baselines per codec; small/high shift them.
_BASE_CRF = {"libx264": 23, "libx265": 24, "libsvtav1": 30}
_QUALITY_SHIFT = {"small": +5, "bal": 0, "high": -4}


@dataclass(slots=True)
class PlanResult:
    plan: EncodePlan
    warnings: list[str] = field(default_factory=list)


def plan_from_options(
    options: dict, media: MediaInfo, *,
    default_codec: str = "libx264",
    default_preset: str = "medium",
    default_crf: int = 23,
    capabilities: dict[str, bool] | None = None,
) -> PlanResult:
    warnings: list[str] = []
    caps = capabilities or {}
    src = media.main_video
    if src is None:
        raise ValidationError("The input has no video stream.")

    codec = _CODEC_MAP.get(str(options.get("codec", "")), default_codec)
    if codec != "copy" and caps and not caps.get(codec, True):
        raise ValidationError(
            f"This FFmpeg build has no {codec} encoder — pick another codec."
        )

    plan = EncodePlan(video_codec=codec, preset=str(options.get("preset", default_preset)))

    # ── Resolution ──────────────────────────────────────────────────────
    res = options.get("resolution", "orig")
    allow_upscale = bool(options.get("allow_upscale"))
    letterbox = bool(options.get("letterbox"))
    if isinstance(res, list) and len(res) == 2:  # custom exact WxH
        decision = compute_exact_dimensions(
            src.width, src.height, int(res[0]), int(res[1]),
            allow_upscale=allow_upscale, letterbox=letterbox,
        )
        if decision.upscale_needed and not allow_upscale:
            warnings.append("Requested size exceeds the source — keeping original.")
        plan.width, plan.height = decision.width, decision.height
        plan.letterbox = decision.letterbox
    elif str(res).isdigit() and int(res) > 0:
        decision = compute_dimensions(
            src.width, src.height, int(res), allow_upscale=allow_upscale
        )
        if decision.upscale_needed and not allow_upscale:
            warnings.append(
                f"{res}p exceeds the source resolution "
                f"({src.width}×{src.height}) — keeping original size."
            )
        plan.width, plan.height = decision.width, decision.height

    if codec == "copy" and (plan.width or options.get("subtitle_mode") == "hard"):
        raise ValidationError(
            "Stream copy cannot change resolution or burn subtitles — "
            "pick a real codec."
        )

    # ── Audio ───────────────────────────────────────────────────────────
    audio = str(options.get("audio", "aac"))
    if audio == "copy":
        plan.audio_codec = "copy"
        probed = media.audio[0].bitrate // 1000 if media.audio else 0
        plan.audio_bitrate_kbps = probed or 128
    else:
        plan.audio_codec = "libopus" if audio == "opus" else "aac"
        plan.audio_bitrate_kbps = int(options.get("audio_bitrate", 128))
    if options.get("audio_stream") is not None:
        plan.audio_stream = int(options["audio_stream"])

    # ── Rate control ────────────────────────────────────────────────────
    quality = str(options.get("quality", "bal"))
    if codec == "copy":
        plan.mode = "copy"
    elif quality == "size":
        target_mb = int(options.get("target_size_mb", 0))
        if target_mb <= 0:
            raise ValidationError("No target size was chosen.")
        plan.mode = "size"
        plan.two_pass = codec in ("libx264", "libx265")
        plan.video_bitrate_kbps = calc_video_bitrate_kbps(
            target_mb * 1024**2, media.duration, plan.audio_bitrate_kbps
        )
    elif quality == "bitrate":
        plan.mode = "bitrate"
        plan.video_bitrate_kbps = int(options.get("video_bitrate", 0))
        if plan.video_bitrate_kbps <= 0:
            raise ValidationError("No video bitrate was chosen.")
    else:
        plan.mode = "crf"
        if quality == "crf":
            crf = options.get("crf")
            if crf is None:
                raise ValidationError("No CRF value was chosen.")
            plan.crf = int(crf)
        else:
            base = _BASE_CRF.get(codec, default_crf)
            plan.crf = base + _QUALITY_SHIFT.get(quality, 0)

    # ── Container ───────────────────────────────────────────────────────
    container = str(options.get("container", ""))
    if not container:
        container = "mp4" if codec == "libx264" else "mkv"
    if codec == "copy":
        container = "mkv"  # holds anything
    plan.container = container
    plan.faststart = container == "mp4"

    # ── Subtitles (paths are filled in by the worker) ───────────────────
    plan.subtitle_mode = str(options.get("subtitle_mode", "none"))
    if plan.subtitle_mode == "soft" and container == "mp4":
        warnings.append("MP4 soft subtitles are stored as mov_text (styling is lost).")
    if options.get("deinterlace"):
        plan.deinterlace = True
    return PlanResult(plan, warnings)
