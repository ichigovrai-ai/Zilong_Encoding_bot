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

    if job.status in TERMINAL:
        await cb.answer("This job is already finished.", show_alert=True)
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
            job.options["step"] = "res"
            job.options["history"] = []
            await rt.repo.update_fields(job.id, options=job.options)
            text, kb = wizard.render(job)
            await _edit(cb, text, kb)
            await cb.answer()
            return
        if step == "confirm":
            await cb.answer("Starting…")
            text = await wizard.start_confirmed(rt, job)
            await _edit(cb, text, keyboards.cancel_only(job.id))
            return
        await cb.answer()
        return

    # ── Wizard steps ────────────────────────────────────────────────────
    if data.verb in ("r", "c", "q", "s", "ss", "u", "bk"):
        text, kb, alert = await wizard.handle_selection(rt, job, data.verb,
                                                        data.arg)
        if alert:
            await cb.answer(alert, show_alert=True)
        else:
            await cb.answer()
        await _edit(cb, text, kb)
        return

    await cb.answer()


async def _handle_action(rt: Runtime, cb: CallbackQuery, job: Job,
                         action: str) -> None:
    if action == "minfo":
        cached = job.options.get("mediainfo_html")
        if cached:
            await cb.answer()
            await _edit(cb, cached)
            if job.status == JobStatus.CREATED:
                await rt.repo.transition(job.id, JobStatus.COMPLETED)
            return
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
    await _edit(cb, cb.message.text.html if cb.message and cb.message.text
                else "Select the episodes to process:",
                keyboards.episodes_menu(job.id, s.get("matches") or [],
                                        s.get("selected") or []))
