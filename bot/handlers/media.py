"""Incoming media routing: videos, video documents, subtitle uploads,
.torrent files, and reply-commands (/encode … /mediainfo on a video)."""
from __future__ import annotations

import logging
from pathlib import Path

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from bot.handlers import intake, wizard
from bot.jobs.models import JobKind
from bot.runtime import get_runtime
from bot.utils.matching import SUBTITLE_EXTS, VIDEO_EXTS

log = logging.getLogger(__name__)

_MAX_SUB_BYTES = 64 * 1024**2
_MAX_TORRENT_BYTES = 10 * 1024**2


def _caption_is_command(message: Message) -> bool:
    cap = (message.caption or "").strip().lower()
    return cap.startswith("/") and cap.lstrip("/").split()[0].split("@")[0] \
        in set(_KIND_COMMANDS) | {"extractsub", "extractaudio", "convertsub"}


@Client.on_message(filters.video & filters.private, group=1)
async def on_video(_: Client, message: Message) -> None:
    if _caption_is_command(message):
        return  # on_kind_command handles captioned videos
    await intake.intake_media(get_runtime(), message)


@Client.on_message(filters.document & filters.private, group=1)
async def on_document(_: Client, message: Message) -> None:
    rt = get_runtime()
    if _caption_is_command(message):
        return
    doc = message.document
    name = (doc.file_name or "").lower()
    suffix = Path(name).suffix

    if suffix == ".torrent":
        if (doc.file_size or 0) > _MAX_TORRENT_BYTES:
            await message.reply_text("⚠️ That .torrent file is suspiciously large.")
            return
        from bot.utils.torrent import TorrentParseError, torrent_to_magnet
        data = await _download_small(rt, message, _MAX_TORRENT_BYTES)
        if data is None:
            return
        try:
            magnet = torrent_to_magnet(data)
        except TorrentParseError as exc:
            await message.reply_text(f"⚠️ {exc}")
            return
        await intake.intake_magnet(rt, message, magnet)
        return

    if suffix in SUBTITLE_EXTS:
        await _on_subtitle_upload(rt, message, suffix)
        return

    if suffix in VIDEO_EXTS:
        await intake.intake_media(rt, message)
        return
    # Anything else is ignored silently unless the user is mid-wizard.


async def _on_subtitle_upload(rt, message: Message, suffix: str) -> None:
    job = await rt.repo.awaiting_job_for_user(message.from_user.id)
    if job is None or job.options.get("await") != "subtitle":
        # No wizard waiting for this file — offer standalone conversion.
        await start_convertsub(rt, message, suffix)
        return
    if (message.document.file_size or 0) > _MAX_SUB_BYTES:
        await message.reply_text("⚠️ Subtitle file is too large.")
        return
    dest = Path(job.work_dir) / "subtitles" / f"upload{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = await rt.client.download_media(message, file_name=str(dest))
    if not result:
        await message.reply_text("⚠️ Could not download the subtitle — try again.")
        return

    # Sniff the content NOW so untimed TXT is rejected before encoding starts.
    from bot.services.subtitles import (
        SubtitleError, UntimedSubtitleError, decode_subtitle, sniff_format,
    )
    from bot.ui import keyboards, messages as msg_texts
    try:
        text = decode_subtitle(Path(result).read_bytes())
        fmt = sniff_format(text)
        from bot.services.subtitles import SubtitleFormat
        if fmt == SubtitleFormat.UNTIMED:
            raise UntimedSubtitleError()
        if fmt == SubtitleFormat.UNKNOWN:
            raise SubtitleError("Could not recognise the subtitle format.")
    except UntimedSubtitleError:
        await message.reply_text(
            msg_texts.UNTIMED_TXT,
            reply_markup=keyboards.untimed_txt_menu(job.id),
            parse_mode=enums.ParseMode.HTML,
        )
        return
    except SubtitleError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return

    job.options["subtitle_file"] = str(dest)
    text_out, kb = await wizard.after_subtitle_upload(rt, job)
    await message.reply_text(
        f"✅ Subtitle accepted ({fmt.value.upper()}).",
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        await rt.client.edit_message_text(
            job.chat_id, job.status_msg_id, text_out,
            reply_markup=kb, parse_mode=enums.ParseMode.HTML,
        )
    except Exception:  # noqa: BLE001
        log.warning("Could not refresh wizard message for %s", job.id)


async def start_convertsub(rt, message: Message, suffix: str) -> None:
    """A subtitle file arrived on its own — offer format conversion."""
    from bot.handlers import intake
    from bot.jobs.models import InputType, Job, new_job_id
    from bot.services.subtitles import (
        SubtitleFormat, decode_subtitle, sniff_format,
    )
    from bot.ui import keyboards, messages as msg_texts
    from bot.ui.panels import SEP
    from bot.utils.filenames import sanitize_filename
    from bot.utils.paths import create_job_dirs

    refusal = await intake.check_access(rt, message)
    if refusal:
        await message.reply_text(refusal)
        return
    if (message.document.file_size or 0) > _MAX_SUB_BYTES:
        await message.reply_text("⚠️ Subtitle file is too large.")
        return

    name = sanitize_filename(message.document.file_name or f"subtitle{suffix}")
    job = Job(
        id=new_job_id(),
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        kind=JobKind.CONVERTSUB,
        input_type=InputType.TG_DOCUMENT,
        input_ref=str(message.document.file_id),
        input_name=name,
        input_size=message.document.file_size or 0,
        options={"src_msg_id": message.id},
    )
    work = create_job_dirs(rt.config.work_dir, job.user_id, job.id)
    job.work_dir = str(work)
    dest = work / "subtitles" / f"original{suffix}"
    result = await rt.client.download_media(message, file_name=str(dest))
    if not result:
        await message.reply_text("⚠️ Could not download the file — try again.")
        return

    fmt = sniff_format(decode_subtitle(Path(result).read_bytes()))
    if fmt == SubtitleFormat.UNTIMED:
        await message.reply_text(
            msg_texts.UNTIMED_TXT, parse_mode=enums.ParseMode.HTML,
        )
        return
    if fmt == SubtitleFormat.UNKNOWN:
        await message.reply_text("⚠️ Could not recognise the subtitle format.")
        return
    job.options["subtitle_file"] = str(dest)
    job.options["detected_format"] = fmt.value
    sent = await message.reply_text(
        "💬 <b>SUBTITLE RECEIVED</b>\n"
        f"{SEP}\n"
        f"📄 <code>{name[:50]}</code>\n"
        f"🔎 Detected format: <b>{fmt.value.upper()}</b>\n\n"
        "Convert it to:\n"
        "<i>(or send a video and choose Hardsub to burn it in)</i>",
        reply_markup=keyboards.convertsub_menu(job.id),
        parse_mode=enums.ParseMode.HTML, quote=True,
    )
    job.status_msg_id = sent.id
    await rt.repo.insert(job)


async def _download_small(rt, message: Message, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    async for chunk in rt.client.stream_media(message):
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            await message.reply_text("⚠️ File too large.")
            return None
    return b"".join(chunks)


# ── Reply-commands: /encode etc. replying to a video ─────────────────────

_KIND_COMMANDS = {
    "encode": JobKind.ENCODE,
    "compress": JobKind.COMPRESS,
    "convert": JobKind.CONVERT,
    "hardsub": JobKind.HARDSUB,
    "mediainfo": JobKind.MEDIAINFO,
    "extract": JobKind.EXTRACT,
}


@Client.on_message(
    filters.command(list(_KIND_COMMANDS) + ["extractsub", "extractaudio",
                                            "convertsub"])
    & filters.private
)
async def on_kind_command(_: Client, message: Message) -> None:
    rt = get_runtime()
    cmd = message.command[0].lower()
    target = message.reply_to_message
    if target is None and (message.video or message.document):
        target = message  # command sent as the video's caption
    if target is None or not (target.video or target.document):
        await message.reply_text(
            "Reply to a video with this command, or just send the video and "
            "use the buttons."
        )
        return
    if cmd == "convertsub" and target.document:
        sfx = Path(target.document.file_name or "").suffix.lower()
        if sfx in SUBTITLE_EXTS:
            # Replying to a subtitle file → standalone format conversion.
            await start_convertsub(rt, target, sfx)
            return
    if cmd in ("extractsub", "convertsub"):
        kind, extract_target = JobKind.EXTRACT, "subtitles"
    elif cmd == "extractaudio":
        kind, extract_target = JobKind.EXTRACT, "audio"
    else:
        kind, extract_target = _KIND_COMMANDS[cmd], None

    if kind == JobKind.MEDIAINFO:
        await intake.intake_media(rt, target, preset_kind=JobKind.MEDIAINFO)
        return
    if kind == JobKind.EXTRACT:
        await intake.intake_media(rt, target, preset_kind=JobKind.EXTRACT)
        # extract target chosen via menu unless implied by the command
        if extract_target:
            job = (await rt.repo.user_jobs(message.from_user.id, limit=1) or [None])[0]
            if job and job.kind == JobKind.EXTRACT:
                job.options["extract_target"] = extract_target
                await rt.repo.update_fields(job.id, options=job.options)
                await rt.manager.enqueue(job.id)
        return
    await intake.intake_media(rt, target, preset_kind=kind)
