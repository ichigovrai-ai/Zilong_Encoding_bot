"""Central CallbackQuery router for wizard buttons, episode selection and
cancellation."""
from __future__ import annotations

import logging

from pyrogram import Client, enums
from pyrogram.types import CallbackQuery

from bot.handlers import wizard
from bot.jobs.models import Job, JobKind
from bot.jobs.states import TERMINAL, JobStatus
from bot.runtime import Runtime, get_runtime
from bot.ui import keyboards
from bot.ui.callbacks import parse

log = logging.getLogger(__name__)


async def _edit(cb: CallbackQuery, text: str, kb=None) -> None:
    try:
        await cb.message.edit_text(
            text, reply_markup=kb, parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 — MessageNotModified and friends
        if "MESSAGE_NOT_MODIFIED" not in str(exc):
            log.warning("Callback edit failed: %s", exc)


@Client.on_callback_query()
async def on_callback(_: Client, cb: CallbackQuery) -> None:
    rt = get_runtime()
    data = parse(cb.data or "")
    if data is None:
        await cb.answer("Unknown button.", show_alert=False)
        return
    if data.verb == "n":
        await cb.answer()
        return
    if data.verb == "st":
        await _handle_setting(rt, cb, data.arg)
        return

    job = await rt.repo.get(data.job_id)
    if job is None:
        await cb.answer("This job no longer exists.", show_alert=True)
        return
    uid = cb.from_user.id if cb.from_user else 0
    if job.user_id != uid and uid != rt.config.owner_id:
        await cb.answer("This isn't your job.", show_alert=True)
        return

    # ── Cancel ──────────────────────────────────────────────────────────
    if data.verb == "x":
        ok = await rt.manager.cancel_job(job.id)
        await cb.answer("Cancelling…" if ok else "Already finished.")
        if ok and job.status in (JobStatus.CREATED, JobStatus.WAITING_FOR_INPUT):
            await _edit(cb, f"🚫 <b>Cancelled</b> — "
                            f"<code>{job.input_name[:50]}</code>")
        return

    # ── MediaInfo card/full toggle (valid on the completed mediainfo job) ─
    if data.verb == "mi":
        await _handle_mediainfo(rt, cb, job, data.arg)
        return

    # ── Retry (only valid on a terminal FAILED job) ─────────────────────
    if data.verb == "rt":
        if job.status != JobStatus.FAILED:
            await cb.answer("Only failed jobs can be retried.", show_alert=True)
            return
        if job.kind == JobKind.CONVERTSUB:
            await cb.answer("Send the subtitle file again to retry.",
                            show_alert=True)
            return
        await rt.repo.update_fields(job.id, error="", progress=0.0,
                                    cleaned=False, cancel_requested=False,
                                    output_path="")
        await rt.manager.enqueue(job.id)
        await cb.answer("Retrying…")
        await _edit(cb, "🔁 <b>QUEUED AGAIN</b>\n"
                        f"📄 <code>{job.input_name[:50]}</code>",
                    keyboards.cancel_only(job.id))
        return

    if job.status in TERMINAL:
        await cb.answer("This job is already finished.", show_alert=True)
        return

    # ── Standalone subtitle conversion ──────────────────────────────────
    if data.verb == "cv":
        await _handle_convertsub(rt, cb, job, data.arg)
        return

    # ── Seedr episode selection ─────────────────────────────────────────
    if data.verb in ("e", "ea", "fx", "fs"):
        await _handle_episode_verb(rt, cb, job, data.verb, data.arg)
        return

    # ── Action menu ─────────────────────────────────────────────────────
    if data.verb == "a":
        await _handle_action(rt, cb, job, data.arg)
        return

    # ── Start ───────────────────────────────────────────────────────────
    if data.verb == "go":
        step = job.options.get("step")
        if (job.kind == JobKind.SEEDR_HARDSUB and step is None
                and job.stage == "select_episodes"):
            if not (job.seedr.get("selected") or []):
                await cb.answer("Select at least one episode.", show_alert=True)
                return
            # Start directly using the user's saved /settings — no per-magnet
            # resolution/codec/quality wizard.
            await cb.answer("Starting…")
            text = await wizard.start_confirmed(rt, job)
            await _edit(cb, text, keyboards.cancel_only(job.id))
            return
        if step == "confirm":
            await cb.answer("Starting…")
            text = await wizard.start_confirmed(rt, job)
            await _edit(cb, text, keyboards.cancel_only(job.id))
            return
        await cb.answer()
        return

    # ── Wizard steps ────────────────────────────────────────────────────
    if data.verb in wizard.WIZARD_VERBS:
        text, kb, alert = await wizard.handle_selection(rt, job, data.verb,
                                                        data.arg)
        if alert:
            await cb.answer(alert, show_alert=True)
        else:
            await cb.answer()
        await _edit(cb, text, kb)
        return

    await cb.answer()


async def _handle_mediainfo(rt: Runtime, cb: CallbackQuery, job: Job,
                            arg: str) -> None:
    """Toggle between the compact MediaInfo card and the full CLI report."""
    from bot.services.mediainfo import render_cli
    full = str(job.options.get("mi_full", ""))
    card = str(job.options.get("mi_card", ""))
    if arg == "full" and full:
        await cb.answer()
        await _edit(cb, render_cli(full, job.input_name),
                    keyboards.mediainfo_full_menu(job.id))
    elif arg == "card" and card:
        await cb.answer()
        await _edit(cb, card,
                    keyboards.mediainfo_menu(job.id, has_full=bool(full)))
    else:
        await cb.answer("That view is no longer available.", show_alert=True)


async def _handle_convertsub(rt: Runtime, cb: CallbackQuery, job: Job,
                             target: str) -> None:
    """Convert an uploaded subtitle file and send it back as a document."""
    from pathlib import Path

    from bot.services.extraction import ExtractionError, convert_subtitle_format
    from bot.services.subtitles import SubtitleError

    if target not in ("srt", "ass", "vtt") or job.kind != JobKind.CONVERTSUB:
        await cb.answer("Unknown conversion.", show_alert=True)
        return
    raw = str(job.options.get("subtitle_file", ""))
    if not raw or not Path(raw).exists():
        await cb.answer("The file is gone — send it again.", show_alert=True)
        return
    await cb.answer("Converting…")
    try:
        out = await convert_subtitle_format(
            raw, target, Path(job.work_dir) / "output",
            output_stem=Path(job.input_name).stem,
            ffmpeg=rt.config.ffmpeg_path,
        )
        await rt.client.send_document(
            job.chat_id, out,
            caption=f"💬 {Path(job.input_name).name} → <b>{target.upper()}</b>",
            parse_mode=enums.ParseMode.HTML,
        )
        await rt.repo.transition(job.id, JobStatus.COMPLETED)
        await _edit(cb, f"✅ <b>CONVERTED</b> → {target.upper()}\n"
                        f"📄 <code>{job.input_name[:50]}</code>")
    except (SubtitleError, ExtractionError) as exc:
        await rt.repo.transition(job.id, JobStatus.FAILED, error=str(exc))
        await _edit(cb, f"❌ <b>Conversion failed</b>\n⚠️ {exc}")
    finally:
        from bot.services.cleanup import cleanup_job_dir
        await cleanup_job_dir(rt.repo, rt.config.work_dir, job.id)


async def _handle_setting(rt: Runtime, cb: CallbackQuery, arg: str) -> None:
    """Cycle one preference to its next value and refresh the /settings menu."""
    from bot.services.user_settings import (
        button_label, cycle_setting, get_user_settings, is_setting,
    )
    uid = cb.from_user.id if cb.from_user else 0
    if arg == "done":
        await cb.answer("Saved ✅")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return
    if not is_setting(arg):
        await cb.answer()
        return
    settings = await cycle_setting(rt.db, uid, arg)
    await cb.answer(button_label(settings, arg).split(": ", 1)[-1])
    from bot.handlers.start import settings_text
    try:
        await cb.message.edit_text(
            settings_text(rt, settings),
            reply_markup=keyboards.settings_menu(settings),
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001
        if "MESSAGE_NOT_MODIFIED" not in str(exc):
            log.warning("Settings edit failed: %s", exc)


async def _handle_action(rt: Runtime, cb: CallbackQuery, job: Job,
                         action: str) -> None:
    if action == "minfo":
        # Full analysis on the real file (MediaInfo CLI needs the whole file).
        await cb.answer("Downloading for full analysis…")
        await rt.repo.update_fields(job.id, kind=JobKind.MEDIAINFO)
        await _edit(cb, "🔎 Downloading for full analysis…",
                    keyboards.cancel_only(job.id))
        await rt.manager.enqueue(job.id)
        return

    if action == "extract":
        p = job.options.get("probe") or {}
        n_subs = len(p.get("subs") or [])
        n_audio = int(p.get("n_audio") or 0)
        await cb.answer()
        await _edit(cb, "What should I extract?",
                    keyboards.extract_menu(job.id, n_subs or 1, n_audio or 1))
        return

    if action in ("exsub", "exaud"):
        job.options["extract_target"] = ("subtitles" if action == "exsub"
                                         else "audio")
        await rt.repo.update_fields(job.id, kind=JobKind.EXTRACT,
                                    options=job.options)
        await cb.answer("Queued.")
        await _edit(cb, "📤 Extracting…", keyboards.cancel_only(job.id))
        await rt.manager.enqueue(job.id)
        return

    if action in (JobKind.ENCODE, JobKind.COMPRESS, JobKind.CONVERT,
                  JobKind.HARDSUB):
        job.options["av1_ok"] = bool(rt.capabilities.get("libsvtav1"))
        text, kb = await wizard.enter_kind(rt, job, action)
        await cb.answer()
        await _edit(cb, text, kb)
        return

    await cb.answer("Unknown action.", show_alert=True)


async def _handle_episode_verb(rt: Runtime, cb: CallbackQuery, job: Job,
                               verb: str, arg: str) -> None:
    s = job.seedr
    matches = s.get("matches") or []
    selected: list[int] = list(s.get("selected") or [])

    if verb == "e":
        try:
            idx = int(arg)
        except ValueError:
            await cb.answer()
            return
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)
        s["selected"] = sorted(selected)

    elif verb == "ea":
        s["selected"] = [] if len(selected) == len(matches) \
            else list(range(len(matches)))

    elif verb == "fx":
        subs = [x["path"] for x in (s.get("subs") or [])]
        if not subs:
            await cb.answer("No subtitle files in this torrent.",
                            show_alert=True)
            return
        try:
            idx = int(arg)
        except ValueError:
            await cb.answer()
            return
        await cb.answer()
        await _edit(cb, f"Pick the subtitle for "
                        f"<code>{matches[idx]['video_label'][:50]}</code>:",
                    keyboards.subtitle_pick_menu(job.id, idx, subs))
        return

    elif verb == "fs":
        ep_raw, _, sub_raw = arg.partition(":")
        try:
            ep_idx, sub_idx = int(ep_raw), int(sub_raw)
        except ValueError:
            await cb.answer()
            return
        if not 0 <= ep_idx < len(matches):
            await cb.answer()
            return
        subs = s.get("subs") or []
        if sub_idx < 0:
            matches[ep_idx].update(subtitle=None, sub_id=None,
                                   confidence="none", score=0.0)
        elif sub_idx < len(subs):
            matches[ep_idx].update(
                subtitle=subs[sub_idx]["path"],
                sub_id=subs[sub_idx]["id"],
                confidence="exact", score=1.0,
            )
        s["matches"] = matches

    await rt.repo.update_fields(job.id, seedr=s)
    await cb.answer()
    from bot.ui.panels import episodes_panel
    from bot.utils.matching import MatchConfidence
    fresh_matches = s.get("matches") or []
    fresh_selected = s.get("selected") or []
    await _edit(
        cb,
        episodes_panel(
            folder_name=str(s.get("folder_name") or "torrent"),
            n_videos=len(fresh_matches),
            n_subs=len(s.get("subs") or []),
            n_selected=len(fresh_selected),
            n_uncertain=sum(
                1 for m in fresh_matches
                if m.get("confidence") == MatchConfidence.UNCERTAIN.value
            ),
        ),
        keyboards.episodes_menu(job.id, fresh_matches, fresh_selected),
    )
