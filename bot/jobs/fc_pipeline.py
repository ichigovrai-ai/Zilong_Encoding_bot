"""FreeConvert (cloud) hardsub pipeline.

    resolve video URL → resolve subtitle → FreeConvert hardsub → upload

The video never touches the bot's disk on the way in: FreeConvert imports it
straight from the URL (a direct link, or a signed Seedr CDN link). Only the
small subtitle file is handled locally. Subtitles come from one of:

    upload         — sent by the user in the wizard (direct-link flow)
    seedr          — external subtitle matched in the torrent
    embedded_auto  — the video's own French text track (stream extractor)

If no French text track exists the job pauses and asks for a subtitle file
instead of failing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from bot.jobs.models import InputType, Job
from bot.jobs.states import JobStatus
from bot.runtime import Runtime
from bot.services import freeconvert
from bot.services.codec_profiles import RESIZE_CHOICES
from bot.services.extraction import extract_text_subtitle
from bot.services.ffprobe import MediaInfo, ProbeError, probe_media
from bot.services.house_style import NO_CHANGE_KEY, STYLE_PRESET_LABELS
from bot.services.seedr import SeedrClient
from bot.services.subtitles import SubtitleError, prepare_subtitle_file
from bot.services.thumbnail import make_thumbnails
from bot.services.upload import (
    build_caption, should_send_as_video, upload_file,
)
from bot.ui import keyboards, panels
from bot.utils.paths import create_job_dirs, free_disk_bytes
from bot.utils.progress import ProgressReporter
from bot.utils.subprocesses import CancelToken
from bot.utils.validation import ValidationError

log = logging.getLogger(__name__)

# Reading a subtitle track out of a remote MKV means streaming the whole file
# (tracks are interleaved with the video), so allow plenty of time.
_REMOTE_EXTRACT_TIMEOUT = 3600

FC_SUB_LANG = "fr"


class NeedSubtitle(Exception):
    """No usable subtitle yet — the job pauses and asks the user for a file."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def pick_french_stream(media: MediaInfo):
    """Best French TEXT subtitle track, or None.

    Bitmap tracks (PGS/VobSub) can't be burned as text, so they are skipped.
    Among French tracks a full (non-forced) one wins; then the default one.
    """
    french = [s for s in media.subtitles
              if not s.is_bitmap and (s.language or "").lower()[:2] == FC_SUB_LANG]
    if not french:
        return None
    french.sort(key=lambda s: (s.forced, not s.default, s.type_index))
    return french[0]


def target_size(res: int, src_w: int = 0, src_h: int = 0) -> tuple[int, int] | None:
    """Output size for a chosen height. 0 = keep the original.

    With a known source ratio the width follows it (even number), so a 4:3 or
    scope source isn't squashed into 16:9; otherwise the standard 16:9 size.
    """
    fixed = RESIZE_CHOICES.get(int(res))
    if fixed is None:
        return None
    if src_w > 0 and src_h > 0:
        width = round(int(res) * src_w / src_h / 2) * 2
        return max(width, 2), int(res)
    return fixed


def fc_res_list(options: dict) -> list[int]:
    """Heights chosen for the run, in the order picked (0 = original).
    Falls back to the single legacy ``fc_res`` option."""
    out: list[int] = []
    raw = options.get("fc_res_list")
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v not in out:
                out.append(v)
    if not out:
        try:
            out = [int(options.get("fc_res", 0) or 0)]
        except (TypeError, ValueError):
            out = [0]
    return out[:keyboards.FC_MAX_QUALITIES]


def _res_label(res: int) -> str:
    return "Original" if res == 0 else f"{res}p"


def _settings_line(job: Job, res: int | None = None) -> str:
    """One-line summary. ``res`` pins the line to a single quality (used for
    each output's caption in a multi-quality run)."""
    choices = [res] if res is not None else fc_res_list(job.options)
    if len(choices) == 1:
        q = dict(keyboards.FC_QUALITIES).get(str(choices[0]),
                                             "Original quality")
    else:
        q = " + ".join(_res_label(r) for r in choices)
    st = STYLE_PRESET_LABELS.get(str(job.options.get("fc_style", "a")), "Style A")
    return f"FreeConvert · {q} · {st}"


async def _resolve_video_url(rt: Runtime, job: Job, work: Path,
                             token: CancelToken) -> str:
    """URL FreeConvert will import. For Seedr episodes this also downloads
    the matched external subtitle, if any (small file)."""
    if job.input_type == InputType.URL:
        return job.input_ref
    if job.input_type != InputType.SEEDR_FILE:
        raise ValidationError("FreeConvert hardsub needs a link or a Seedr episode.")

    from bot.jobs.worker import _seedr_file_url
    from bot.services.download import download_url

    cfg = rt.config
    folder_id = job.seedr.get("folder_id")
    user, pwd = cfg.seedr_credentials(job.seedr.get("account", ""))
    async with SeedrClient(user, pwd, proxy=cfg.seedr_proxy) as sc:
        url = await _seedr_file_url(sc, int(job.input_ref), folder_id,
                                    job.input_name, prefer_video=True)
        sub_id = job.seedr.get("sub_id")
        have_sub = job.options.get("subtitle_file") and Path(
            str(job.options["subtitle_file"])).exists()
        if sub_id and not have_sub:
            sub_name = str(job.seedr.get("sub_name") or "sub.ass")
            suffix = Path(sub_name).suffix.lower() or ".ass"
            sub_url = await _seedr_file_url(sc, int(sub_id), folder_id,
                                            sub_name, prefer_video=False)
            dest = work / "subtitles" / f"raw_sub{suffix}"
            await download_url(sub_url, dest, max_bytes=64 * 1024**2,
                               cancel_token=token)
            job.options["subtitle_file"] = str(dest)
            await rt.repo.update_fields(job.id, options=job.options)
    return url


async def _resolve_subtitle(rt: Runtime, job: Job, video_url: str,
                            work: Path, token: CancelToken) -> tuple[str, MediaInfo | None]:
    """→ (path of a clean .ass/.srt, probe of the video if we made one)."""
    cfg = rt.config
    source = str(job.options.get("subtitle_source", ""))
    media: MediaInfo | None = None

    if source == "embedded_auto":
        try:
            media = await probe_media(video_url, ffprobe=cfg.ffprobe_path, timeout=60)
        except ProbeError as exc:
            raise SubtitleError(f"Could not read the video's tracks: {exc}") from exc
        stream = pick_french_stream(media)
        if stream is None:
            raise NeedSubtitle(
                "This video has no French text track to extract.")
        log.info("Job %s: extracting French subtitle stream #%d (%s)",
                 job.id, stream.type_index, stream.codec)
        raw = await extract_text_subtitle(
            video_url, stream, work / "subtitles", ffmpeg=cfg.ffmpeg_path,
            cancel_token=token, timeout=_REMOTE_EXTRACT_TIMEOUT,
        )
    else:
        raw = str(job.options.get("subtitle_file", ""))
        if not raw or not Path(raw).exists():
            # e.g. a retry after the work dir was cleaned up.
            raise NeedSubtitle("The subtitle file is no longer on the server.")

    fps = media.main_video.fps if media and media.main_video else 23.976
    prepared = prepare_subtitle_file(raw, work / "subtitles", fps=fps)
    return str(prepared.path), media


async def _ask_for_subtitle(rt: Runtime, job: Job, reporter: ProgressReporter,
                            reason: str) -> None:
    """No usable subtitle: pause the job and wait for a subtitle file."""
    job.options["await"] = "subtitle"
    job.options["step"] = "fcsub"
    job.options["subtitle_source"] = "upload"
    await rt.repo.update_fields(job.id, options=job.options)
    await rt.repo.transition(job.id, JobStatus.WAITING_FOR_INPUT,
                             stage="needs_subtitle")
    await reporter.update(
        "💬 <b>SUBTITLE NEEDED</b>\n" + panels.SEP + "\n"
        f"📄 <code>{panels.short_name(job.input_name)}</code>\n\n"
        f"{reason} Send the subtitle file (.ass, .srt…) and the cloud "
        "encode will start right away.",
        reply_markup=keyboards.cancel_only(job.id), force=True,
    )


async def _cancellable(coro, token: CancelToken):
    """Run `coro`, cancelling it as soon as the job's token is cancelled.

    FreeConvert's wait loop only stops on task cancellation, and it deletes
    the cloud job when that happens — so Cancel really stops the encode.
    """
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            if token.cancelled:
                task.cancel()
                break
            await asyncio.wait({task}, timeout=1.0)
        return await task
    except asyncio.CancelledError:
        task.cancel()
        raise


async def run_fc_job(rt: Runtime, job: Job, token: CancelToken,
                     reporter: ProgressReporter, started: float,
                     settings: dict | None, style: str) -> None:
    from bot.jobs.worker import _maybe_delete_seedr_folder
    from bot.services.cleanup import cleanup_job_dir

    cfg = rt.config
    if not cfg.fc_enabled:
        raise ValidationError("FreeConvert isn't configured (set FC_API_KEYS).")
    work = Path(job.work_dir) if job.work_dir else Path()
    if not job.work_dir or not work.exists():
        work = create_job_dirs(cfg.work_dir, job.user_id, job.id)
        await rt.repo.update_fields(job.id, work_dir=str(work))
    if free_disk_bytes(cfg.work_dir) < cfg.min_free_disk_gb * 1024**3:
        raise ValidationError(
            "Not enough free disk space on the server — try again later.")

    o = job.options
    resolutions = fc_res_list(o)
    style_key = str(o.get("fc_style", "a"))
    settings_line = _settings_line(job)
    stages = panels.pipeline_stages("analyse", with_subtitles=True)

    # ── 1. Resolve the video URL (+ external subtitle) ──────────────────
    await rt.repo.transition(job.id, JobStatus.DOWNLOADING, stage="preparing")
    await reporter.update(
        "🔎 <b>PREPARING</b>\n" + panels.SEP + "\n"
        f"📄 <code>{panels.short_name(job.input_name)}</code>\n"
        f"🎯 <i>{settings_line}</i>\n\n"
        + "\n".join(panels.stage_checklist(stages)),
        reply_markup=keyboards.cancel_only(job.id),
    )
    video_url = await _resolve_video_url(rt, job, work, token)
    token.raise_if_cancelled()

    # ── 2. Subtitle (upload / matched / extracted French track) ─────────
    await rt.repo.transition(job.id, JobStatus.PREPARING_SUBTITLES,
                             stage="subtitles")
    await reporter.update(
        "💬 <b>PREPARING SUBTITLES</b>\n" + panels.SEP + "\n"
        f"📄 <code>{panels.short_name(job.input_name)}</code>\n"
        + ("🧩 <i>Extracting the French track…</i>"
           if o.get("subtitle_source") == "embedded_auto" else ""),
        reply_markup=keyboards.cancel_only(job.id),
    )
    try:
        sub_path, media = await _resolve_subtitle(rt, job, video_url, work, token)
    except NeedSubtitle as exc:
        await _ask_for_subtitle(rt, job, reporter, exc.reason)
        return
    token.raise_if_cancelled()

    # Source size: from the probe we just made, else the link-intake probe.
    src_w = src_h = 0
    if media and media.main_video:
        src_w, src_h = media.main_video.width, media.main_video.height
    else:
        probe = o.get("probe") or {}
        src_w, src_h = int(probe.get("width") or 0), int(probe.get("height") or 0)
    sizes = {r: target_size(r, src_w, src_h) for r in resolutions}
    multi = len(resolutions) > 1

    # ── 3. FreeConvert ──────────────────────────────────────────────────
    await rt.repo.transition(job.id, JobStatus.ENCODING, stage="encoding")
    out_dir = work / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    fc_started = time.monotonic()
    last: dict[str, Any] = {"phase": "encode"}

    async def _on_process(pct: float, detail: str) -> None:
        await rt.repo.update_fields(job.id, progress=pct)
        await reporter.update(
            "☁️ <b>FREECONVERT</b>\n" + panels.SEP + "\n"
            f"📄 <code>{panels.short_name(job.input_name)}</code>\n"
            f"🎯 <i>{settings_line}</i>\n\n"
            f"{panels.bar(pct, style)}  <b>{pct:.0f}%</b>\n"
            f"⚙️ {detail}\n⏱ {int(time.monotonic() - fc_started)}s",
            reply_markup=keyboards.cancel_only(job.id),
        )

    async def _on_download(pct: float, detail: str) -> None:
        last["phase"] = "download"
        await rt.repo.update_fields(job.id, progress=pct)
        await reporter.update(
            "📥 <b>FETCHING RESULT</b>\n" + panels.SEP + "\n"
            f"📄 <code>{panels.short_name(job.input_name)}</code>\n\n"
            f"{panels.bar(pct, style)}  <b>{pct:.0f}%</b>\n{detail}",
            reply_markup=keyboards.cancel_only(job.id),
        )

    # (quality height, file) for each produced output, in the order picked.
    outputs: list[tuple[int, Path]] = []
    if multi:
        # One FreeConvert job: the video is imported once and the subtitle is
        # sent once; every selected quality is its own convert + export task.
        keyed = {(f"{r}p" if r else "orig"): r for r in resolutions}
        produced = await _cancellable(
            freeconvert.hardsub_remote_url_multi(
                cfg.fc_keys_csv, video_url, job.input_name, sub_path,
                str(out_dir),
                qualities={k: sizes[r] for k, r in keyed.items()},
                quality_profile="balanced", style_key=style_key,
                codec="h264", process_cb=_on_process,
                download_cb=_on_download,
            ),
            token,
        )
        outputs = [(keyed[k], Path(p)) for k, p in produced.items()]
    else:
        res = resolutions[0]
        output = await _cancellable(
            freeconvert.hardsub_remote_url(
                cfg.fc_keys_csv, video_url, job.input_name, sub_path,
                str(out_dir),
                quality_profile="balanced", resize=sizes[res],
                style_key=style_key, codec="h264",
                process_cb=_on_process, download_cb=_on_download,
            ),
            token,
        )
        outputs = [(res, Path(output))]
    token.raise_if_cancelled()
    await rt.repo.update_fields(job.id, output_path=str(outputs[0][1]))

    # Validate every output; in a multi-quality run a bad one is skipped (and
    # reported) so the good ones are still delivered.
    ready: list[tuple[int, Path, int]] = []
    skipped: list[str] = []
    limit_mb = cfg.max_output_size // 1024**2
    for res_h, path in outputs:
        size = path.stat().st_size if path.exists() else 0
        if size == 0:
            if not multi:
                raise ValidationError("FreeConvert returned an empty file.")
            skipped.append(f"{_res_label(res_h)}: empty file")
        elif size > cfg.max_output_size:
            if not multi:
                raise ValidationError(
                    f"The encoded file ({size // 1024**2} MB) exceeds the "
                    f"configured output limit ({limit_mb} MB). "
                    "Pick a lower quality and try again.")
            skipped.append(f"{_res_label(res_h)}: {size // 1024**2} MB "
                           f"is over the {limit_mb} MB limit")
        else:
            ready.append((res_h, path, size))
    if not ready:
        raise ValidationError("No output could be delivered: "
                              + "; ".join(skipped))

    # ── 4. Upload ───────────────────────────────────────────────────────
    from bot.services.user_settings import value_of
    await rt.repo.transition(job.id, JobStatus.UPLOADING, stage="uploading")
    upload_pref = value_of(settings or {}, "upload_as")
    upload_stages = panels.pipeline_stages("upload", with_subtitles=True)

    for idx, (res_h, output_path, out_size) in enumerate(ready, start=1):
        token.raise_if_cancelled()
        out_media: MediaInfo | None = None
        try:
            out_media = await probe_media(str(output_path),
                                          ffprobe=cfg.ffprobe_path)
        except ProbeError:
            log.warning("Output probe failed for %s — uploading anyway", job.id)
        thumbs = await make_thumbnails(
            str(output_path), out_dir,
            out_media.duration if out_media else (media.duration if media else 0.0),
            media=out_media, ffmpeg=cfg.ffmpeg_path, cancel_token=token,
        )
        upload_started = time.monotonic()
        title = ("Uploading" if len(ready) == 1
                 else f"Uploading {idx}/{len(ready)}")

        async def _upl_progress(done: int, total: int, _p=output_path,
                                _t=title, _u=upload_started) -> None:
            await rt.repo.update_fields(
                job.id, progress=done / total * 100 if total else 0)
            elapsed_ul = time.monotonic() - _u
            speed = done / elapsed_ul if elapsed_ul > 1 else 0.0
            await reporter.update(
                panels.transfer_panel(
                    title=_t, icon="📤", filename=_p.name,
                    done=done, total=total, speed=speed, elapsed=elapsed_ul,
                    source="Telegram 📲", stages=upload_stages, style=style,
                ),
                reply_markup=keyboards.cancel_only(job.id),
            )

        caption = build_caption(
            original_name=job.input_name, out_path=output_path,
            media=out_media,
            plan_summary=(_settings_line(job, res_h) if multi
                          else settings_line),
            encode_seconds=time.monotonic() - started,
            template=str(o.get("caption_template", "")),
        )
        async with rt.manager.upload_sem:
            token.raise_if_cancelled()
            await upload_file(
                rt.client, job.chat_id, output_path,
                media=out_media, caption=caption,
                thumb=thumbs.thumb, cover=thumbs.cover,
                as_video=should_send_as_video(
                    output_path, out_media,
                    str(o.get("upload_as") or upload_pref or "auto")),
                progress_cb=_upl_progress,
                cancelled=lambda: token.cancelled,
            )

    await rt.repo.transition(job.id, JobStatus.COMPLETED, stage="done")
    shown = (ready[0][1].name if len(ready) == 1 else
             f"{len(ready)} files · " + " + ".join(_res_label(r)
                                                   for r, _, _ in ready))
    done_text = panels.done_panel(
        filename=shown, out_size=sum(sz for _, _, sz in ready),
        elapsed=time.monotonic() - started,
        settings_line=settings_line, job_id=job.id)
    if skipped:
        done_text += "\n⚠️ Skipped: " + "; ".join(skipped)
    await reporter.update(done_text, force=True)
    await cleanup_job_dir(rt.repo, cfg.work_dir, job.id)
    await _maybe_delete_seedr_folder(rt, job.id)
