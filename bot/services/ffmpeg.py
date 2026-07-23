"""FFmpeg command builder and progress-aware runner.

Single place that knows how to compose FFmpeg argument lists. Handlers never
see raw FFmpeg flags. Everything runs through argument lists +
`asyncio.create_subprocess_exec`; there is no shell anywhere.

Filter-path safety: jobs copy inputs into a controlled working directory with
ASCII names (input/input.mkv, subtitles/sub.ass, fonts/) and FFmpeg runs with
cwd=<job dir>, so filter paths are short relative paths. `escape_filter_path`
is still applied as defence in depth.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from bot.services.encoding import EncodePlan
from bot.utils.subprocesses import CancelToken, _terminate

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    def __init__(self, message: str, stderr_tail: str = ""):
        super().__init__(message)
        self.stderr_tail = stderr_tail


def escape_filter_path(path: str) -> str:
    r"""Escape a path for use inside a filter option value.

    Order matters: backslashes first. Colons (Windows drive letters), quotes
    and brackets all confuse the filtergraph parser.
    """
    path = path.replace("\\", "/")
    for ch in ("\\", ":", "'", "[", "]", ",", ";"):
        path = path.replace(ch, "\\" + ch)
    return path


def build_video_filter(plan: EncodePlan, *, source_path: str = "") -> str:
    """Compose the -vf chain: deinterlace → scale → pad → subtitles → format.

    Subtitles are rendered AFTER scaling (crisp text at output resolution;
    libass maps ASS PlayRes coordinates onto the actual frame size, so
    positioning is preserved).
    """
    chain: list[str] = []
    if plan.deinterlace:
        chain.append("yadif=mode=send_frame")
    if plan.width and plan.height:
        chain.append(f"scale={plan.width}:{plan.height}:flags=lanczos")
    if plan.letterbox:
        bw, bh = plan.letterbox
        chain.append(f"pad={bw}:{bh}:(ow-iw)/2:(oh-ih)/2:color=black")
    if plan.subtitle_mode == "hard":
        if plan.subtitle_path:
            sub = f"subtitles=filename='{escape_filter_path(plan.subtitle_path)}'"
            if plan.fonts_dir:
                sub += f":fontsdir='{escape_filter_path(plan.fonts_dir)}'"
        elif plan.subtitle_stream is not None and source_path:
            sub = (
                f"subtitles=filename='{escape_filter_path(source_path)}'"
                f":si={plan.subtitle_stream}"
            )
            if plan.fonts_dir:
                sub += f":fontsdir='{escape_filter_path(plan.fonts_dir)}'"
        else:
            raise FFmpegError("Hardsub requested but no subtitle source resolved.")
        chain.append(sub)
    if plan.pix_fmt and plan.video_codec != "copy":
        chain.append(f"format={plan.pix_fmt}")
    return ",".join(chain)


def _rate_args(plan: EncodePlan, pass_num: int, passlog: str) -> list[str]:
    args: list[str] = []
    codec = plan.video_codec
    if plan.mode == "crf":
        if codec == "libsvtav1":
            args += ["-crf", str(plan.crf)]
        else:
            args += ["-crf", str(plan.crf)]
    else:  # size | bitrate → ABR with sane caps
        kbps = plan.video_bitrate_kbps
        args += ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k",
                 "-bufsize", f"{kbps * 2}k"]
    if plan.two_pass and pass_num:
        if codec == "libx265":
            args += ["-x265-params", f"pass={pass_num}:stats={passlog}.x265"]
        else:
            args += ["-pass", str(pass_num), "-passlogfile", passlog]
    return args


def build_encode_args(
    plan: EncodePlan,
    input_path: str,
    output_path: str,
    *,
    ffmpeg: str = "ffmpeg",
    threads: int = 0,
    pass_num: int = 0,          # 0 = single pass, 1/2 = two-pass stages
    passlog: str = "ffpass",
    extra_inputs: list[str] | None = None,
) -> list[str]:
    args = [ffmpeg, "-hide_banner", "-y", "-nostdin", "-i", input_path]
    for extra in extra_inputs or []:
        args += ["-i", extra]

    is_null_pass = plan.two_pass and pass_num == 1

    # Stream mapping: first video; selected (or first) audio; soft subs from
    # the extra input when requested.
    args += ["-map", "0:v:0"]
    if not is_null_pass:
        audio_sel = plan.audio_stream if plan.audio_stream is not None else 0
        args += ["-map", f"0:a:{audio_sel}?"]
        if plan.subtitle_mode == "soft" and extra_inputs:
            args += ["-map", "1:s:0?"]

    vf = build_video_filter(plan, source_path=input_path)
    if plan.video_codec == "copy":
        args += ["-c:v", "copy"]
    else:
        if vf:
            args += ["-vf", vf]
        args += ["-c:v", plan.video_codec]
        if plan.video_codec in ("libx264", "libx265"):
            args += ["-preset", plan.preset]
        elif plan.video_codec == "libsvtav1":
            args += ["-preset", _svtav1_preset(plan.preset)]
        args += _rate_args(plan, pass_num, passlog)
        if plan.video_codec == "libx265":
            args += ["-tag:v", "hvc1"]  # Apple/Telegram compatibility

    if is_null_pass:
        args += ["-an", "-sn", "-f", "null",
                 "NUL" if _is_windows() else "/dev/null",
                 "-progress", "pipe:1", "-nostats"]
        return args

    if plan.audio_codec == "copy":
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", plan.audio_codec, "-b:a", f"{plan.audio_bitrate_kbps}k"]
        if plan.audio_codec == "libopus":
            # Opus rejects some surround layouts without an explicit mapping.
            args += ["-af", "aformat=channel_layouts=7.1|5.1|stereo|mono"]

    if plan.subtitle_mode == "soft":
        args += ["-c:s", "mov_text" if plan.container == "mp4" else "copy"]
    elif plan.subtitle_mode in ("hard", "strip"):
        args += ["-sn"]

    if plan.container == "mp4" and plan.faststart:
        args += ["-movflags", "+faststart"]
    if threads > 0:
        args += ["-threads", str(threads)]
    args += ["-progress", "pipe:1", "-nostats", output_path]
    return args


def _svtav1_preset(x264_preset: str) -> str:
    """Map familiar x264 preset names onto SVT-AV1's 0-13 speed scale."""
    return {
        "veryslow": "2", "slower": "3", "slow": "4", "medium": "6",
        "fast": "8", "faster": "9", "veryfast": "10", "superfast": "11",
        "ultrafast": "12",
    }.get(x264_preset, "6")


def _is_windows() -> bool:
    import sys
    return sys.platform == "win32"


@dataclass(slots=True)
class EncodeProgress:
    percent: float
    out_time_s: float
    fps: float
    speed: float
    total_size: int
    eta_s: float | None    # None when speed is unknown — never fake an ETA


async def run_ffmpeg_with_progress(
    args: list[str],
    *,
    duration_s: float,
    on_progress: Callable[[EncodeProgress], Awaitable[None]] | None = None,
    cancel_token: CancelToken | None = None,
    cwd: str | None = None,
    timeout: float = 6 * 3600,
) -> None:
    """Run an encode, streaming `-progress pipe:1` updates. Raises FFmpegError
    with the stderr tail on failure."""
    log.info("ffmpeg: %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    if cancel_token:
        cancel_token.register(proc)
    stderr_tail: deque[bytes] = deque(maxlen=80)

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while line := await proc.stderr.readline():
            stderr_tail.append(line)

    async def _read_progress() -> None:
        assert proc.stdout is not None
        current: dict[str, str] = {}
        while line := await proc.stdout.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if "=" not in text:
                continue
            key, _, value = text.partition("=")
            current[key] = value
            if key != "progress":
                continue
            out_us = current.get("out_time_us") or current.get("out_time_ms") or "0"
            try:
                out_s = max(0.0, int(out_us) / 1_000_000)
            except ValueError:
                out_s = 0.0
            try:
                speed = float(current.get("speed", "0x").rstrip("x") or 0)
            except ValueError:
                speed = 0.0
            try:
                fps = float(current.get("fps", "0") or 0)
            except ValueError:
                fps = 0.0
            try:
                size = int(current.get("total_size", "0") or 0)
            except ValueError:
                size = 0
            pct = min(99.9, out_s / duration_s * 100) if duration_s > 0 else 0.0
            eta = None
            if speed > 0.01 and duration_s > 0:
                eta = max(0.0, (duration_s - out_s) / speed)
            if on_progress:
                try:
                    await on_progress(EncodeProgress(pct, out_s, fps, speed, size, eta))
                except Exception:  # noqa: BLE001 — progress must not kill encode
                    log.exception("Progress callback failed")
            current.clear()

    stderr_task = asyncio.create_task(_drain_stderr())
    progress_task = asyncio.create_task(_read_progress())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _terminate(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            await proc.wait()
        raise
    finally:
        if cancel_token:
            cancel_token.unregister(proc)
        for t in (stderr_task, progress_task):
            t.cancel()
        await asyncio.gather(stderr_task, progress_task, return_exceptions=True)

    if cancel_token and cancel_token.cancelled:
        raise asyncio.CancelledError("job cancelled")
    if proc.returncode != 0:
        tail = b"".join(stderr_tail).decode("utf-8", errors="replace")[-2000:]
        raise FFmpegError(f"FFmpeg exited with code {proc.returncode}", tail)


async def check_ffmpeg_capabilities(ffmpeg: str = "ffmpeg") -> dict[str, bool]:
    """Report which optional encoders/filters this FFmpeg build provides."""
    from bot.utils.subprocesses import run_process
    caps = {"libx264": False, "libx265": False, "libsvtav1": False,
            "libopus": False, "aac": False, "libass": False}
    try:
        _, out, _ = await run_process([ffmpeg, "-hide_banner", "-encoders"], timeout=15)
        text = out.decode("utf-8", errors="replace")
        for enc in ("libx264", "libx265", "libsvtav1", "libopus", "aac"):
            caps[enc] = f" {enc} " in text or f" {enc}\n" in text
        _, out, _ = await run_process([ffmpeg, "-hide_banner", "-filters"], timeout=15)
        caps["libass"] = " subtitles " in out.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("Capability check failed: %s", exc)
    return caps
