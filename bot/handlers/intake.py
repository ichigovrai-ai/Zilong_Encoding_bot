"""Shared intake logic: turning an incoming video/URL/magnet into a job with
its menu message. No pyrogram handlers here."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pyrogram import enums

from bot.jobs.models import InputType, Job, JobKind, new_job_id
from bot.runtime import Runtime
from bot.services.ffprobe import MediaInfo, ProbeError, probe_media
from bot.services.mediainfo import render_mediainfo
from bot.ui import keyboards, messages
from bot.utils.filenames import sanitize_filename
from bot.utils.matching import VIDEO_EXTS
from bot.utils.paths import create_job_dirs
from bot.utils.timefmt import human_size

log = logging.getLogger(__name__)


async def check_access(rt: Runtime, message: Any) -> str | None:
    """None if the user may proceed, else the refusal text."""
    from bot.services.access import access_denial_reason
    user = message.from_user
    if user is None:
        return "This bot only works in direct messages."
    denied = await access_denial_reason(rt, user.id)
    if denied:
        return denied
    return await rt.manager.user_can_submit(user.id)


def probe_to_options(media: MediaInfo | None, real_size: int) -> dict[str, Any]:
    """Compact probe summary stored in job.options for wizard menus."""
    if media is None:
        return {}
    v = media.main_video
    return {
        "probe": {
            "width": v.width if v else 0,
            "height": v.height if v else 0,
            "codec": v.codec if v else "",
            "fps": v.fps if v else 0,
            "duration": media.duration,
            "size": real_size or media.size,
            "n_audio": len(media.audio),
            "subs": [
                {"type_index": s.type_index, "language": s.language,
                 "codec": s.codec, "title": s.title[:30], "bitmap": s.is_bitmap}
                for s in media.subtitles
            ],
        }
    }


async def quick_probe_telegram(rt: Runtime, message: Any,
                               tmp_dir: Path) -> MediaInfo | None:
    """Stream the first ~12 MB of a Telegram file and probe the header.
    Works for MKV and most containers; MP4 with trailing moov just fails
    softly (full probe happens again after the real download)."""
    tmp = tmp_dir / "probe.bin"
    try:
        written = 0
        with tmp.open("wb") as fh:
            async for chunk in rt.client.stream_media(message, limit=12):
                fh.write(chunk)
                written += len(chunk)
                if written >= 12 * 1024**2:
                    break
        if written == 0:
            return None
        return await probe_media(str(tmp), ffprobe=rt.config.ffprobe_path,
                                 timeout=20)
    except (ProbeError, Exception) as exc:  # noqa: BLE001 — best-effort
        log.info("Quick probe failed (%s) — continuing without it", exc)
        return None
    finally:
        tmp.unlink(missing_ok=True)


async def intake_media(rt: Runtime, message: Any, *,
                       preset_kind: str | None = None) -> None:
    """A Telegram video/document arrived (or a command replied to one)."""
    refusal = await check_access(rt, message)
    if refusal:
        await message.reply_text(refusal, parse_mode=enums.ParseMode.HTML)
        return

    media_obj = message.video or message.document
    if media_obj is None:
        await message.reply_text("Send a video file or document.")
        return
    filename = sanitize_filename(
        getattr(media_obj, "file_name", None) or "video.mp4"
    )
    if message.document and Path(filename).suffix.lower() not in VIDEO_EXTS:
        await message.reply_text(
            "That doesn't look like a supported video "
            f"({', '.join(sorted(VIDEO_EXTS))})."
        )
        return
    size = getattr(media_obj, "file_size", 0) or 0
    if size > rt.config.max_input_size:
        await message.reply_text(
            f"⚠️ File is too large ({human_size(size)}). "
            f"Limit: {human_size(rt.config.max_input_size)}."
        )
        return

    job = Job(
        id=new_job_id(),
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        kind=preset_kind or JobKind.ENCODE,
        input_type=InputType.TG_VIDEO if message.video else InputType.TG_DOCUMENT,
        input_ref=str(getattr(media_obj, "file_id", "")),
        input_name=filename,
        input_size=size,
        options={"src_msg_id": message.id},
    )
    work = create_job_dirs(rt.config.work_dir, job.user_id, job.id)
    job.work_dir = str(work)

    media = await quick_probe_telegram(rt, message, work / "input")
    job.options.update(probe_to_options(media, size))
    if media is not None:
        info = render_mediainfo(media, filename)
        job.options["mediainfo_html"] = info.replace(
            f"{human_size(media.size)}", f"{human_size(size)}", 1
        )

    text = (messages.media_summary(media, filename) if media
            else (f"<b>🎞 Video received</b>\n\n<code>{filename}</code> · "
                  f"{human_size(size)}\n\nChoose an action:"))
    sent = await message.reply_text(
        text, reply_markup=keyboards.action_menu(job.id, bool(media and media.subtitles)),
        parse_mode=enums.ParseMode.HTML, quote=True,
    )
    job.status_msg_id = sent.id
    await rt.repo.insert(job)

    if preset_kind is None:
        return
    stored = await rt.repo.get(job.id)
    if stored is None:
        return
    if preset_kind == JobKind.MEDIAINFO:
        cached = stored.options.get("mediainfo_html")
        if cached:
            from bot.jobs.states import JobStatus
            await sent.edit_text(cached, parse_mode=enums.ParseMode.HTML)
            await rt.repo.transition(stored.id, JobStatus.COMPLETED)
        else:
            await sent.edit_text("🔎 Downloading for full analysis…",
                                 parse_mode=enums.ParseMode.HTML,
                                 reply_markup=keyboards.cancel_only(stored.id))
            await rt.manager.enqueue(stored.id)
        return
    if preset_kind == JobKind.EXTRACT:
        p = stored.options.get("probe") or {}
        n_subs = len(p.get("subs") or [])
        n_audio = int(p.get("n_audio") or 0)
        await sent.edit_text(
            "What should I extract?",
            reply_markup=keyboards.extract_menu(stored.id, n_subs or 1,
                                                n_audio or 1),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    if preset_kind in wizard_kinds():
        from bot.handlers import wizard
        wtext, kb = await wizard.enter_kind(rt, stored, preset_kind)
        await sent.edit_text(wtext, reply_markup=kb,
                             parse_mode=enums.ParseMode.HTML)


def wizard_kinds() -> tuple[str, ...]:
    return (JobKind.ENCODE, JobKind.COMPRESS, JobKind.CONVERT, JobKind.HARDSUB)


async def intake_url(rt: Runtime, message: Any, url: str) -> None:
    """A direct http(s) link arrived."""
    from bot.utils.validation import ValidationError, validate_download_url
    refusal = await check_access(rt, message)
    if refusal:
        await message.reply_text(refusal, parse_mode=enums.ParseMode.HTML)
        return
    try:
        url = validate_download_url(url)
    except ValidationError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return

    name = sanitize_filename(Path(url.split("?")[0]).name or "video.mp4")
    job = Job(
        id=new_job_id(),
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        kind=JobKind.ENCODE,
        input_type=InputType.URL,
        input_ref=url,
        input_name=name,
    )
    work = create_job_dirs(rt.config.work_dir, job.user_id, job.id)
    job.work_dir = str(work)

    media: MediaInfo | None = None
    try:
        media = await probe_media(url, ffprobe=rt.config.ffprobe_path, timeout=25)
    except ProbeError as exc:
        log.info("URL probe failed (%s) — continuing without it", exc)
    job.options.update(probe_to_options(media, 0))
    if media is not None:
        job.options["mediainfo_html"] = render_mediainfo(media, name)

    text = (messages.media_summary(media, name) if media
            else (f"<b>🔗 Link received</b>\n\n<code>{name}</code>\n\n"
                  "Choose an action:"))
    sent = await message.reply_text(
        text,
        reply_markup=keyboards.action_menu(job.id, bool(media and media.subtitles)),
        parse_mode=enums.ParseMode.HTML, quote=True,
        disable_web_page_preview=True,
    )
    job.status_msg_id = sent.id
    await rt.repo.insert(job)


async def intake_magnet(rt: Runtime, message: Any, magnet: str) -> None:
    """A magnet link arrived — start the Seedr flow immediately."""
    from urllib.parse import parse_qs, urlparse
    from bot.utils.validation import ValidationError, validate_magnet

    refusal = await check_access(rt, message)
    if refusal:
        await message.reply_text(refusal, parse_mode=enums.ParseMode.HTML)
        return
    if not rt.config.seedr_enabled:
        await message.reply_text(
            "⚠️ Seedr is not configured — set SEEDR_USERNAME and "
            "SEEDR_PASSWORD to use magnet links."
        )
        return
    try:
        infohash = validate_magnet(magnet)
    except ValidationError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return

    dn = ""
    try:
        dn = (parse_qs(urlparse(magnet).query).get("dn") or [""])[0]
    except Exception:  # noqa: BLE001
        pass
    name = sanitize_filename(dn or f"torrent_{infohash[:12]}")

    job = Job(
        id=new_job_id(),
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        kind=JobKind.SEEDR_HARDSUB,
        input_type=InputType.MAGNET,
        input_ref=magnet,
        input_name=name,
        options={"magnet_name": dn, "infohash": infohash},
    )
    work = create_job_dirs(rt.config.work_dir, job.user_id, job.id)
    job.work_dir = str(work)
    sent = await message.reply_text(
        f"🧲 <b>Magnet accepted</b>\n<code>{name[:60]}</code>\n\n"
        "Sending to Seedr…",
        parse_mode=enums.ParseMode.HTML, quote=True,
        reply_markup=keyboards.cancel_only(job.id),
    )
    job.status_msg_id = sent.id
    await rt.repo.insert(job)
    await rt.manager.enqueue(job.id)
