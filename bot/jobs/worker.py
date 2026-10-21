"""Job execution pipeline.

One `run_job` task per job. Stages transition through the state machine and
check the cancel token between steps. Every failure path produces a
user-facing message; full details go to the log only.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from bot.jobs.models import InputType, Job, JobKind
from bot.jobs.states import TERMINAL, JobStatus
from bot.runtime import Runtime, get_runtime
from bot.services.download import (
    DownloadError, download_telegram_media, download_url,
)
from bot.services.encoding import EncodePlan
from bot.services.extraction import ExtractionError, extract_text_subtitle
from bot.services.ffmpeg import (
    FFmpegError, build_encode_args, run_ffmpeg_with_progress,
)
from bot.services.ffprobe import MediaInfo, ProbeError, probe_media
from bot.services.fonts import extract_fonts, missing_font_warnings
from bot.services.mediainfo import render_mediainfo
from bot.services.planner import plan_from_options
from bot.services.seedr import SeedrClient, SeedrError
from bot.services.subtitles import (
    SubtitleError, UntimedSubtitleError, ass_referenced_fonts,
    prepare_subtitle_file,
)
from bot.services.thumbnail import make_thumbnails
from bot.services.upload import (
    UploadError, build_caption, should_send_as_video, upload_file,
)
from bot.services.user_settings import get_user_settings
from bot.ui import keyboards, panels
from bot.utils.filenames import build_output_name, unique_path
from bot.utils.matching import (
    SUBTITLE_EXTS, VIDEO_EXTS, MatchConfidence, match_subtitles,
)
from bot.utils.paths import free_disk_bytes
from bot.utils.progress import ProgressReporter
from bot.utils.subprocesses import CancelToken
from bot.utils.validation import ValidationError

log = logging.getLogger(__name__)

# Errors whose message is safe and useful to show the user directly.
USER_ERRORS = (ValidationError, SubtitleError, DownloadError, SeedrError,
               ProbeError, ExtractionError, UploadError)


async def run_job(job_id: str, token: CancelToken) -> None:
    rt = get_runtime()
    job = await rt.repo.get(job_id)
    if job is None or job.status in TERMINAL:
        return
    reporter = ProgressReporter(
        rt.client, job.chat_id, job.status_msg_id,
        rt.config.progress_update_interval,
    )
    try:
        settings = await get_user_settings(rt.db, job.user_id)
    except Exception:  # noqa: BLE001 — preferences must never block a job
        settings = {}
    style = panels.norm_style(settings.get("panel_style"))
    started = time.monotonic()
    try:
        if job.cancel_requested:
            raise asyncio.CancelledError
        timeout = rt.config.job_timeout_hours * 3600
        if job.kind == JobKind.SEEDR_HARDSUB and job.input_type == InputType.MAGNET:
            timeout = max(timeout, rt.config.seedr_timeout_hours * 3600 + 3600)
            await asyncio.wait_for(
                _run_seedr_parent(rt, job, token, reporter, style),
                timeout=timeout,
            )
        elif job.kind == JobKind.FC_HARDSUB:
            from bot.jobs.fc_pipeline import run_fc_job
            await asyncio.wait_for(
                run_fc_job(rt, job, token, reporter, started, settings, style),
                timeout=timeout,
            )
        else:
            await asyncio.wait_for(
                _run_pipeline(rt, job, token, reporter, started, settings),
                timeout=timeout,
            )
    except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
        cancelled = token.cancelled or job.cancel_requested
        if isinstance(exc, asyncio.TimeoutError) and not cancelled:
            await _finalise(rt, job_id, reporter, JobStatus.FAILED,
                            "The job exceeded the maximum allowed runtime.")
        elif cancelled:
            await _finalise(rt, job_id, reporter, JobStatus.CANCELLED, "")
        else:
            await _finalise(rt, job_id, reporter, JobStatus.FAILED,
                            "The job was interrupted by a bot shutdown.")
    except FFmpegError as exc:
        log.error("Job %s FFmpeg failure: %s\n%s", job_id, exc, exc.stderr_tail)
        await _finalise(rt, job_id, reporter, JobStatus.FAILED,
                        f"FFmpeg failed: {exc}\n\nLast output:\n{exc.stderr_tail[-600:]}")
    except UntimedSubtitleError:
        await _finalise(rt, job_id, reporter, JobStatus.FAILED,
                        "The subtitle file has no usable timestamps "
                        "(plain text). Send a timed subtitle and try again.")
    except USER_ERRORS as exc:
        log.warning("Job %s failed: %s", job_id, exc)
        await _finalise(rt, job_id, reporter, JobStatus.FAILED, str(exc))
    except Exception:  # noqa: BLE001
        log.exception("Job %s crashed", job_id)
        await _finalise(rt, job_id, reporter, JobStatus.FAILED,
                        "An internal error occurred. The details were logged.")


async def _finalise(rt: Runtime, job_id: str, reporter: ProgressReporter,
                    status: JobStatus, error: str) -> None:
    """Terminal bookkeeping: transition, panel update, cleanup, Seedr cleanup."""
    from bot.services.cleanup import cleanup_job_dir
    try:
        job = await rt.repo.get(job_id)
        if job and job.status not in TERMINAL:
            job = await rt.repo.transition(job_id, status, error=error)
        if job:
            if status == JobStatus.FAILED:
                await reporter.update(
                    panels.error_panel(filename=job.input_name,
                                       error=job.error or error,
                                       job_id=job.id),
                    reply_markup=keyboards.retry_menu(job.id),
                    force=True,
                )
            elif status == JobStatus.CANCELLED:
                await reporter.update(
                    panels.cancelled_panel(filename=job.input_name,
                                           job_id=job.id),
                    force=True,
                )
    except Exception:  # noqa: BLE001
        log.exception("Finalise transition failed for %s", job_id)
    try:
        await cleanup_job_dir(rt.repo, rt.config.work_dir, job_id)
    except Exception:  # noqa: BLE001
        log.exception("Finalise cleanup failed for %s", job_id)
    try:
        await _maybe_delete_seedr_folder(rt, job_id)
    except Exception:  # noqa: BLE001
        log.exception("Seedr folder cleanup failed for %s", job_id)


# ═════════════════════════════════════════════════════════════════════════
# Standard pipeline: download → probe → subtitles → encode → upload
# ═════════════════════════════════════════════════════════════════════════

async def _run_pipeline(rt: Runtime, job: Job, token: CancelToken,
                        reporter: ProgressReporter, started: float,
                        settings: dict | None = None) -> None:
    from bot.services.user_settings import value_of
    cfg = rt.config
    settings = settings or {}
    style = panels.norm_style(settings.get("panel_style"))
    upload_pref = value_of(settings, "upload_as")
    work = Path(job.work_dir)
    if not work.exists():
        from bot.utils.paths import create_job_dirs
        work = create_job_dirs(cfg.work_dir, job.user_id, job.id)
        await rt.repo.update_fields(job.id, work_dir=str(work))

    if free_disk_bytes(cfg.work_dir) < cfg.min_free_disk_gb * 1024**3:
        raise ValidationError(
            "Not enough free disk space on the server — try again later."
        )
    with_subs = str(job.options.get("subtitle_mode", "none")) in ("hard", "soft")

    # ── 1. Acquire input ────────────────────────────────────────────────
    await rt.repo.transition(job.id, JobStatus.DOWNLOADING, stage="downloading")
    async with rt.manager.download_sem:
        token.raise_if_cancelled()
        input_path = await _acquire_input(rt, job, work, token, reporter,
                                          style, with_subs)
    token.raise_if_cancelled()

    # ── 2. Probe ────────────────────────────────────────────────────────
    await rt.repo.transition(job.id, JobStatus.PROBING, stage="probing")
    analyse_stages = panels.stage_checklist(
        panels.pipeline_stages("analyse", with_subtitles=with_subs)
    )
    await reporter.update(
        "🔎 <b>ANALYSING</b>\n" + panels.SEP + "\n"
        f"📄 <code>{panels.short_name(job.input_name)}</code>\n\n"
        + "\n".join(analyse_stages),
        reply_markup=keyboards.cancel_only(job.id),
    )
    media = await probe_media(str(input_path), ffprobe=cfg.ffprobe_path)

    if job.kind == JobKind.MEDIAINFO:
        from bot.services.mediainfo import mediainfo_cli_text, render_card
        cli = await mediainfo_cli_text(str(input_path),
                                       binary=cfg.mediainfo_path)
        card = render_card(media, job.input_name)
        # Stash the card + full CLI report so the card/full buttons work after
        # cleanup (the job row persists; the work dir does not).
        job.options["mi_card"] = card
        if cli:
            job.options["mi_full"] = cli
        await rt.repo.update_fields(job.id, options=job.options)
        await reporter.update(
            card,
            reply_markup=keyboards.mediainfo_menu(job.id, has_full=bool(cli)),
            force=True,
        )
        await rt.repo.transition(job.id, JobStatus.COMPLETED)
        return

    if job.kind == JobKind.EXTRACT:
        await _run_extract(rt, job, media, input_path, work, token, reporter)
        return

    # ── 3. Subtitles ────────────────────────────────────────────────────
    _codec_map = {"x264": "libx264", "x265": "libx265", "av1": "libsvtav1"}
    _def_res = value_of(settings, "def_resolution")
    plan_result = plan_from_options(
        job.options, media,
        default_codec=_codec_map.get(value_of(settings, "def_codec"),
                                     cfg.default_video_codec),
        default_preset=value_of(settings, "def_preset"),
        default_crf=int(value_of(settings, "def_crf")),
        default_resolution="orig" if _def_res == "original" else _def_res,
        default_audio=value_of(settings, "def_audio"),
        capabilities=rt.capabilities,
    )
    plan = plan_result.plan
    warnings = list(plan_result.warnings)

    if plan.subtitle_mode in ("hard", "soft"):
        await rt.repo.transition(job.id, JobStatus.PREPARING_SUBTITLES,
                                 stage="subtitles")
        sub_stages = panels.stage_checklist(
            panels.pipeline_stages("subtitles", with_subtitles=True)
        )
        await reporter.update(
            "💬 <b>PREPARING SUBTITLES</b>\n" + panels.SEP + "\n"
            f"📄 <code>{panels.short_name(job.input_name)}</code>\n\n"
            + "\n".join(sub_stages),
            reply_markup=keyboards.cancel_only(job.id),
        )
        sub_lang = value_of(settings, "sub_lang")
        warnings += await _prepare_subtitles(
            rt, job, media, input_path, work, plan, token,
            preferred_lang="" if sub_lang == "off" else sub_lang,
        )
    token.raise_if_cancelled()

    # ── 4. Encode ───────────────────────────────────────────────────────
    await rt.repo.transition(job.id, JobStatus.ENCODING, stage="encoding")
    out_name = build_output_name(
        job.input_name, container=plan.container,
        hardsub=plan.subtitle_mode == "hard",
        sub_lang=plan.subtitle_lang,
        height=plan.target_height_label,
    )
    output_path = unique_path(work / "output" / out_name)
    async with rt.manager.encode_sem:
        token.raise_if_cancelled()
        await _run_encode(rt, job, plan, media, input_path, output_path,
                          work, token, reporter, started, style, with_subs)
    await rt.repo.update_fields(job.id, output_path=str(output_path))

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise FFmpegError("Encoding produced no output file.")
    out_size = output_path.stat().st_size
    if out_size > cfg.max_output_size:
        raise ValidationError(
            f"The encoded file ({out_size // 1024**2} MB) exceeds the "
            f"configured output limit ({cfg.max_output_size // 1024**2} MB)."
        )

    # ── 5. Upload ───────────────────────────────────────────────────────
    await rt.repo.transition(job.id, JobStatus.UPLOADING, stage="uploading")
    out_media: MediaInfo | None = None
    try:
        out_media = await probe_media(str(output_path), ffprobe=cfg.ffprobe_path)
    except ProbeError:
        log.warning("Output probe failed for %s — uploading anyway", job.id)
    thumbs = await make_thumbnails(
        str(output_path), work / "output",
        out_media.duration if out_media else media.duration,
        media=out_media, ffmpeg=cfg.ffmpeg_path, cancel_token=token,
    )

    upload_started = time.monotonic()
    upload_stages = panels.pipeline_stages("upload", with_subtitles=with_subs)

    async def _upl_progress(done: int, total: int) -> None:
        await rt.repo.update_fields(job.id, progress=done / total * 100 if total else 0)
        elapsed_ul = time.monotonic() - upload_started
        speed = done / elapsed_ul if elapsed_ul > 1 else 0.0
        await reporter.update(
            panels.transfer_panel(
                title="Uploading", icon="📤", filename=output_path.name,
                done=done, total=total, speed=speed, elapsed=elapsed_ul,
                source="Telegram 📲", stages=upload_stages, style=style,
            ),
            reply_markup=keyboards.cancel_only(job.id),
        )

    elapsed = time.monotonic() - started
    caption = build_caption(
        original_name=job.input_name, out_path=output_path,
        media=out_media, plan_summary=plan.summary(), encode_seconds=elapsed,
        template=str(job.options.get("caption_template", "")),
    )
    if warnings:
        caption = caption[:900] + "\n⚠️ " + "; ".join(warnings)[:120]

    async with rt.manager.upload_sem:
        token.raise_if_cancelled()
        await upload_file(
            rt.client, job.chat_id, output_path,
            media=out_media, caption=caption,
            thumb=thumbs.thumb, cover=thumbs.cover,
            as_video=should_send_as_video(
                output_path, out_media,
                str(job.options.get("upload_as") or upload_pref or "auto"),
            ),
            progress_cb=_upl_progress,
            cancelled=lambda: token.cancelled,
        )

    await rt.repo.transition(job.id, JobStatus.COMPLETED, stage="done")
    await reporter.update(
        panels.done_panel(
            filename=output_path.name, out_size=out_size,
            elapsed=time.monotonic() - started,
            settings_line=plan.summary(), job_id=job.id,
        ),
        force=True,
    )
    from bot.services.cleanup import cleanup_job_dir
    await cleanup_job_dir(rt.repo, cfg.work_dir, job.id)
    await _maybe_delete_seedr_folder(rt, job.id)


async def _seedr_file_url(sc, file_id: int, folder_id, expected_name: str,
                          *, prefer_video: bool) -> str:
    """Get a Seedr download URL, re-resolving the file id from its folder if
    the stored id has gone stale (fetch_file 404 — common when a busy free
    account churns files between listing and download)."""
    try:
        return await sc.fetch_file_url(file_id)
    except SeedrError as exc:
        if not folder_id:
            raise
        from bot.utils.matching import VIDEO_EXTS, normalize_name
        try:
            files = await sc.walk_folder(int(folder_id))
        except SeedrError:
            files = []
        want = normalize_name(expected_name)
        match = next((f for f in files if normalize_name(f.name) == want), None)
        if match is None and prefer_video:
            vids = [f for f in files
                    if Path(f.name).suffix.lower() in VIDEO_EXTS]
            match = max(vids, key=lambda f: f.size) if vids else None
        if match is None:
            raise SeedrError(
                "This file is no longer available on Seedr (it may have been "
                "evicted while other transfers ran). Please re-send the magnet."
            ) from exc
        log.info("Re-resolved stale Seedr file id %s -> %s", file_id, match.id)
        return await sc.fetch_file_url(match.id)


_SOURCE_LABELS = {
    InputType.TG_VIDEO: "Telegram 📲",
    InputType.TG_DOCUMENT: "Telegram 📲",
    InputType.URL: "Direct 🔗",
    InputType.SEEDR_FILE: "Seedr ☁️",
}


async def _acquire_input(rt: Runtime, job: Job, work: Path,
                         token: CancelToken, reporter: ProgressReporter,
                         style: str = "A", with_subs: bool = False) -> Path:
    cfg = rt.config
    suffix = Path(job.input_name).suffix.lower()
    if suffix not in VIDEO_EXTS:
        suffix = ".mkv" if job.input_type != InputType.URL else (suffix or ".mp4")
    dest = work / "input" / f"input{suffix}"
    dl_started = time.monotonic()
    dl_stages = panels.pipeline_stages("download", with_subtitles=with_subs)
    source = _SOURCE_LABELS.get(job.input_type, "Source 🔗")

    async def _dl_progress(done: int, total: int) -> None:
        pct = done / total * 100 if total else 0.0
        await rt.repo.update_fields(job.id, progress=pct)
        elapsed = time.monotonic() - dl_started
        speed = done / elapsed if elapsed > 1 else 0.0
        await reporter.update(
            panels.transfer_panel(
                title="Downloading", icon="📥", filename=job.input_name,
                done=done, total=total, speed=speed, elapsed=elapsed,
                source=source, stages=dl_stages, style=style,
            ),
            reply_markup=keyboards.cancel_only(job.id),
        )

    if job.input_type in (InputType.TG_VIDEO, InputType.TG_DOCUMENT):
        src_msg_id = int(job.options.get("src_msg_id", 0))
        if not src_msg_id:
            raise DownloadError("The original message reference was lost.")
        message = await rt.client.get_messages(job.chat_id, src_msg_id)
        if message is None or getattr(message, "empty", False):
            raise DownloadError("The original file message was deleted.")
        return await download_telegram_media(
            rt.client, message, dest, max_bytes=cfg.max_input_size,
            progress_cb=_dl_progress, cancel_token=token,
        )

    if job.input_type == InputType.URL:
        return await download_url(
            job.input_ref, dest, max_bytes=cfg.max_input_size,
            progress_cb=_dl_progress, cancel_token=token,
        )

    if job.input_type == InputType.SEEDR_FILE:
        folder_id = job.seedr.get("folder_id")
        acct_user, acct_pwd = cfg.seedr_credentials(job.seedr.get("account", ""))
        async with SeedrClient(acct_user, acct_pwd,
                               proxy=cfg.seedr_proxy) as sc:
            url = await _seedr_file_url(sc, int(job.input_ref), folder_id,
                                        job.input_name, prefer_video=True)
            path = await download_url(
                url, dest, max_bytes=cfg.max_input_size,
                progress_cb=_dl_progress, cancel_token=token,
            )
            sub_id = job.seedr.get("sub_id")
            if sub_id:
                sub_name = str(job.seedr.get("sub_name") or "sub.ass")
                sub_suffix = Path(sub_name).suffix.lower() or ".ass"
                sub_url = await _seedr_file_url(sc, int(sub_id), folder_id,
                                                sub_name, prefer_video=False)
                await download_url(
                    sub_url, work / "subtitles" / f"raw_sub{sub_suffix}",
                    max_bytes=64 * 1024**2, cancel_token=token,
                )
                job.options["subtitle_file"] = str(
                    work / "subtitles" / f"raw_sub{sub_suffix}"
                )
                await rt.repo.update_fields(job.id, options=job.options)
        return path

    raise DownloadError(f"Unsupported input type: {job.input_type}")


async def _prepare_subtitles(rt: Runtime, job: Job, media: MediaInfo,
                             input_path: Path, work: Path, plan: EncodePlan,
                             token: CancelToken,
                             preferred_lang: str = "") -> list[str]:
    """Resolve the subtitle source into plan.subtitle_path (+fonts).

    subtitle_source values:
      embedded       — a specific embedded stream chosen in the wizard
      embedded_auto  — pick the best embedded text stream (Seedr default);
                       if none exists, degrade to a plain encode
      upload/seedr/…  — an external subtitle file already on disk
    """
    cfg = rt.config
    warnings: list[str] = []
    fps = media.main_video.fps if media.main_video else 23.976
    sub_source = str(job.options.get("subtitle_source", ""))
    raw_path: str = ""

    if sub_source == "embedded_auto":
        stream = _pick_embedded_sub(media, preferred_lang or cfg.preferred_sub_lang)
        if stream is None:
            # Nothing burnable (no text subtitle) — encode without subs
            # instead of failing the whole job.
            plan.subtitle_mode = "none"
            log.info("Job %s: no embedded text subtitle to burn — plain encode",
                     job.id)
            return ["No embedded text subtitle found — encoded without subtitles."]
        log.info("Job %s: hardsubbing embedded stream #%d (%s)",
                 job.id, stream.type_index, stream.language)
        plan.subtitle_lang = stream.language
        raw_path = await extract_text_subtitle(
            str(input_path), stream, work / "subtitles",
            ffmpeg=cfg.ffmpeg_path, cancel_token=token,
        )
    elif sub_source == "embedded":
        idx = int(job.options.get("subtitle_stream", 0))
        stream = next((s for s in media.subtitles if s.type_index == idx), None)
        if stream is None:
            raise SubtitleError(f"Embedded subtitle stream #{idx} was not found.")
        plan.subtitle_lang = stream.language
        raw_path = await extract_text_subtitle(
            str(input_path), stream, work / "subtitles",
            ffmpeg=cfg.ffmpeg_path, cancel_token=token,
        )
    else:
        raw_path = str(job.options.get("subtitle_file", ""))
        if not raw_path or not Path(raw_path).exists():
            raise SubtitleError("The subtitle file is missing — upload it again.")
        # External subtitle (uploaded or matched from Seedr) — its language
        # isn't probed, so use the user's preferred language for naming.
        plan.subtitle_lang = preferred_lang or cfg.preferred_sub_lang

    prepared = prepare_subtitle_file(raw_path, work / "subtitles", fps=fps)
    warnings += prepared.report.warnings
    plan.subtitle_path = _relpath_for_filter(Path(prepared.path), work)

    # Fonts: only relevant for ASS burns from containers with attachments.
    if plan.subtitle_mode == "hard" and prepared.format.value == "ass":
        extracted = await extract_fonts(
            str(input_path), work / "fonts", media,
            ffmpeg=cfg.ffmpeg_path, cancel_token=token,
        )
        if extracted:
            plan.fonts_dir = "fonts"
        referenced = ass_referenced_fonts(
            Path(prepared.path).read_text(encoding="utf-8", errors="replace")
        )
        warnings += missing_font_warnings(referenced, extracted, cfg.fallback_font)
    return warnings


def _pick_embedded_sub(media: MediaInfo, preferred_lang: str = ""):
    """Choose which embedded subtitle to burn: preferred language first, then
    the default-disposition track, then the first text (non-bitmap) track.
    Returns None if the file has no burnable text subtitle."""
    text_subs = [s for s in media.subtitles if not s.is_bitmap]
    if not text_subs:
        return None
    if preferred_lang:
        want = preferred_lang.strip().lower()[:2]  # fr matches fr/fra/fre
        if want:
            for s in text_subs:
                if s.language[:2] == want:
                    return s
    for s in text_subs:
        if s.default:
            return s
    return text_subs[0]


def _relpath_for_filter(path: Path, work: Path) -> str:
    """FFmpeg runs with cwd=work, so filter paths are short relative POSIX
    paths — immune to Windows drive colons and Unicode filename issues."""
    try:
        return path.relative_to(work).as_posix()
    except ValueError:
        return str(path)


async def _run_encode(rt: Runtime, job: Job, plan: EncodePlan, media: MediaInfo,
                      input_path: Path, output_path: Path, work: Path,
                      token: CancelToken, reporter: ProgressReporter,
                      started: float, style: str = "A",
                      with_subs: bool = False) -> None:
    cfg = rt.config
    duration = media.duration or 1.0
    rel_input = _relpath_for_filter(input_path, work)
    rel_output = _relpath_for_filter(output_path, work)
    extra_inputs: list[str] = []
    if plan.subtitle_mode == "soft" and plan.subtitle_path:
        extra_inputs.append(plan.subtitle_path)

    encode_stages = panels.pipeline_stages("encode", with_subtitles=with_subs)
    settings_line = plan.summary()
    passes: list[int] = [1, 2] if plan.two_pass else [0]
    for i, pass_num in enumerate(passes):
        base_pct = 50.0 * i if plan.two_pass else 0.0
        scale = 50.0 if plan.two_pass else 100.0
        pass_label = f"pass {pass_num}/2" if plan.two_pass else ""

        async def _on_progress(p) -> None:
            pct = base_pct + p.percent * scale / 100
            await rt.repo.update_fields(job.id, progress=pct)
            await reporter.update(
                panels.encode_panel(
                    filename=job.input_name, settings_line=settings_line,
                    pct=pct, done_s=p.out_time_s, total_s=duration,
                    speed=p.speed, fps=p.fps, out_size=p.total_size,
                    elapsed=time.monotonic() - started, eta=p.eta_s,
                    stages=encode_stages, pass_label=pass_label, style=style,
                ),
                reply_markup=keyboards.cancel_only(job.id),
            )

        args = build_encode_args(
            plan, rel_input, rel_output,
            ffmpeg=cfg.ffmpeg_path, pass_num=pass_num,
            passlog="logs/ffpass", extra_inputs=extra_inputs,
        )
        await run_ffmpeg_with_progress(
            args, duration_s=duration, on_progress=_on_progress,
            cancel_token=token, cwd=str(work),
            timeout=cfg.job_timeout_hours * 3600,
        )


async def _run_extract(rt: Runtime, job: Job, media: MediaInfo,
                       input_path: Path, work: Path, token: CancelToken,
                       reporter: ProgressReporter) -> None:
    """Extract subtitle/audio streams and send them as documents."""
    from bot.services.extraction import extract_audio
    cfg = rt.config
    target = str(job.options.get("extract_target", "subtitles"))
    sent = 0
    if target == "subtitles":
        from bot.services.extraction import select_subtitle_streams
        if not [s for s in media.subtitles if not s.is_bitmap]:
            raise ExtractionError(
                "No text subtitle streams found (bitmap PGS/VobSub streams "
                "cannot be converted without OCR)."
            )
        streams = select_subtitle_streams(
            media.subtitles, job.options.get("extract_streams"))
        if not streams:
            raise ExtractionError(
                "The selected subtitle stream was not found in this file."
            )
        from bot.services.extraction import subtitle_display
        used: dict[str, int] = {}
        for stream in streams[:10]:
            token.raise_if_cancelled()
            path = await extract_text_subtitle(
                str(input_path), stream, work / "output",
                ffmpeg=cfg.ffmpeg_path, cancel_token=token,
            )
            code, lang_name, flag = subtitle_display(stream.language,
                                                     stream.title)
            used[code] = used.get(code, 0) + 1
            suffix = f"_{used[code]}" if used[code] > 1 else ""
            label = f"Subtitle_{code}{suffix}{Path(path).suffix}"
            caption = f"{flag} {lang_name} · {stream.codec}"
            if stream.title:
                caption += f" · {stream.title[:40]}"
            await rt.client.send_document(
                job.chat_id, path, file_name=label, caption=caption,
            )
            sent += 1
    else:
        for i, _a in enumerate(media.audio[:6]):
            token.raise_if_cancelled()
            path = await extract_audio(
                str(input_path), i, work / "output",
                ffmpeg=cfg.ffmpeg_path, cancel_token=token,
            )
            await rt.client.send_document(job.chat_id, path)
            sent += 1
        if not sent:
            raise ExtractionError("No audio streams found.")
    await rt.repo.transition(job.id, JobStatus.COMPLETED)
    await reporter.update(f"✅ Extracted {sent} stream(s).", force=True)
    from bot.services.cleanup import cleanup_job_dir
    await cleanup_job_dir(rt.repo, cfg.work_dir, job.id)


# ═════════════════════════════════════════════════════════════════════════
# Seedr parent flow: magnet → poll → browse → match → wait for selection
# ═════════════════════════════════════════════════════════════════════════

async def _run_seedr_parent(rt: Runtime, job: Job, token: CancelToken,
                            reporter: ProgressReporter,
                            style: str = "A") -> None:
    cfg = rt.config
    if not cfg.seedr_enabled:
        raise SeedrError("Seedr is not configured on this bot "
                         "(SEEDR_USERNAME / SEEDR_PASSWORD).")
    seedr_started = time.monotonic()
    name_hint = str(job.options.get("magnet_name", "") or job.input_name)
    await rt.repo.transition(job.id, JobStatus.DOWNLOADING, stage="seedr_add")
    await reporter.update(
        panels.seedr_panel(torrent_name=name_hint, pct=None, current="magnet",
                           note="Sending magnet to Seedr…", style=style),
        reply_markup=keyboards.cancel_only(job.id), force=True,
    )

    # Pick the account with the most free space, and remember it on the job so
    # every child fetches/deletes from the same account.
    from bot.services.seedr import pick_best_account
    acct_user, acct_pwd, _free = await pick_best_account(
        cfg.seedr_accounts, proxy=cfg.seedr_proxy)
    job.seedr["account"] = acct_user
    await rt.repo.update_fields(job.id, seedr=job.seedr)

    async with SeedrClient(acct_user, acct_pwd,
                               proxy=cfg.seedr_proxy) as sc:
        known = {f.id for f in (await sc.list_folder(0)).folders}
        torrent_id = await sc.add_magnet(job.input_ref)

        last_edit = {"t": 0.0}

        async def _poll_progress(pct: float, name: str) -> None:
            token.raise_if_cancelled()
            await rt.repo.update_fields(job.id, progress=pct)
            now = time.monotonic()
            if now - last_edit["t"] >= cfg.progress_update_interval:
                last_edit["t"] = now
                await reporter.update(
                    panels.seedr_panel(
                        torrent_name=name or name_hint, pct=pct,
                        current="cloud",
                        elapsed=time.monotonic() - seedr_started, style=style,
                    ),
                    reply_markup=keyboards.cancel_only(job.id),
                )

        folder = await sc.wait_ready(
            torrent_id=torrent_id, name_hint=name_hint,
            known_folder_ids=known,
            timeout_s=cfg.seedr_timeout_hours * 3600,
            interval_s=cfg.seedr_poll_interval,
            progress_cb=_poll_progress, cancel_token=token,
        )

        await reporter.update(
            panels.seedr_panel(
                torrent_name=folder.name, pct=100.0, current="match",
                elapsed=time.monotonic() - seedr_started,
                note="Reading folder and matching subtitles…", style=style,
            ),
            force=True,
        )
        files = await sc.walk_folder(folder.id)

    videos = [f for f in files if Path(f.name).suffix.lower() in VIDEO_EXTS
              and f.size > 1024 * 1024]
    subs = [f for f in files if Path(f.name).suffix.lower() in SUBTITLE_EXTS]
    if not videos:
        raise SeedrError("No video files were found in this torrent.")

    matches = match_subtitles([v.path for v in videos], [s.path for s in subs])
    by_path = {f.path: f for f in files}
    match_dicts = []
    for m in matches:
        vfile = by_path[m.video]
        entry = {
            "video": m.video, "video_id": vfile.id, "video_size": vfile.size,
            "video_label": Path(vfile.name).name,
            "subtitle": m.subtitle,
            "sub_id": by_path[m.subtitle].id if m.subtitle else None,
            "confidence": m.confidence.value, "score": m.score,
        }
        match_dicts.append(entry)

    job.seedr = {
        "account": acct_user,
        "folder_id": folder.id, "folder_name": folder.name,
        "torrent_id": torrent_id,
        "subs": [{"path": s.path, "id": s.id} for s in subs],
        "matches": match_dicts,
        "selected": list(range(len(match_dicts))),
    }
    await rt.repo.update_fields(job.id, seedr=job.seedr)
    await rt.repo.transition(job.id, JobStatus.WAITING_FOR_INPUT,
                             stage="select_episodes")

    n_uncertain = sum(1 for m in match_dicts
                      if m["confidence"] == MatchConfidence.UNCERTAIN.value)
    await reporter.update(
        panels.episodes_panel(
            folder_name=folder.name, n_videos=len(videos), n_subs=len(subs),
            n_selected=len(job.seedr["selected"]), n_uncertain=n_uncertain,
        ),
        reply_markup=keyboards.episodes_menu(
            job.id, match_dicts, job.seedr["selected"],
            fc=rt.config.fc_enabled,
        ),
        force=True,
    )


async def _maybe_delete_seedr_folder(rt: Runtime, job_id: str) -> None:
    """Delete the Seedr folder once no open job references it."""
    cfg = rt.config
    if not (cfg.seedr_enabled and cfg.seedr_auto_delete):
        return
    job = await rt.repo.get(job_id)
    if not job or not job.seedr.get("folder_id"):
        return
    folder_id = int(job.seedr["folder_id"])
    terminal = tuple(str(s) for s in TERMINAL)
    marks = ", ".join("?" for _ in terminal)
    row = await rt.db.fetchone(
        f"SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ({marks}) "
        f"AND json_extract(seedr, '$.folder_id') = ?",
        (*terminal, folder_id),
    )
    if row and row["n"] == 0:
        acct_user, acct_pwd = cfg.seedr_credentials(job.seedr.get("account", ""))
        try:
            async with SeedrClient(acct_user, acct_pwd,
                               proxy=cfg.seedr_proxy) as sc:
                await sc.delete_folder(folder_id)
            log.info("Deleted Seedr folder %d after job %s", folder_id, job_id)
        except SeedrError as exc:
            log.warning("Could not delete Seedr folder %d: %s", folder_id, exc)
