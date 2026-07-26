"""Wizard state machine (no pyrogram handlers here — pure helpers shared by
the callback and text-input handlers).

State lives in job.options:
    step      — current wizard step name
    history   — previous steps (for Back)
    await     — "crf" | "size" | "res" | "subtitle" when typed/uploaded input
                is expected
plus the selections themselves (resolution, codec, quality, subtitle_mode …).
"""
from __future__ import annotations

import logging
from typing import Any

from bot.jobs.models import InputType, Job, JobKind, new_job_id
from bot.jobs.states import JobStatus
from bot.runtime import Runtime
from bot.ui import keyboards, messages
from bot.utils.paths import create_job_dirs

log = logging.getLogger(__name__)

# Verbs handled by handle_selection(). The callback router dispatches exactly
# these here — keep the two in sync via this single source of truth.
WIZARD_VERBS = frozenset({"r", "c", "q", "s", "ss", "u", "au", "bk"})

STEPS: dict[str, list[str]] = {
    JobKind.ENCODE: ["res", "codec", "qual", "subsrc", "confirm"],
    JobKind.COMPRESS: ["qual", "confirm"],
    JobKind.CONVERT: ["res", "codec", "qual", "aud", "confirm"],
    JobKind.HARDSUB: ["subsrc", "res", "codec", "qual", "confirm"],
    # Seedr parent continues into this after episode selection:
    JobKind.SEEDR_HARDSUB: ["res", "codec", "qual", "confirm"],
}

_RES_ARGS = {"360", "480", "720", "1080", "1440", "2160"}


def probe_of(job: Job) -> dict[str, Any]:
    return job.options.get("probe") or {}


def src_short_side(job: Job) -> int:
    p = probe_of(job)
    w, h = int(p.get("width") or 0), int(p.get("height") or 0)
    return min(w, h) if w and h else 0


def has_embedded_subs(job: Job) -> bool:
    return bool(probe_of(job).get("subs"))


async def enter_kind(rt: Runtime, job: Job, kind: str) -> tuple[str, Any]:
    """User picked an action from the main menu."""
    job.kind = kind
    steps = STEPS[kind]
    job.options["step"] = steps[0]
    job.options["history"] = []
    if kind == JobKind.HARDSUB:
        job.options["subtitle_mode"] = "hard"
    await rt.repo.update_fields(job.id, kind=kind, options=job.options)
    return render(job)


def _next_step(job: Job) -> str:
    steps = STEPS.get(job.kind, STEPS[JobKind.ENCODE])
    cur = job.options.get("step", steps[0])
    # sub-steps not present in the main list
    if cur in ("substream", "subdisp", "upscale"):
        cur = job.options.get("main_step", steps[0])
    try:
        idx = steps.index(cur)
    except ValueError:
        idx = 0
    return steps[min(idx + 1, len(steps) - 1)]


def advance(job: Job) -> None:
    hist = job.options.setdefault("history", [])
    hist.append(job.options.get("step"))
    job.options["step"] = _next_step(job)
    job.options["main_step"] = job.options["step"]
    job.options.pop("await", None)


def go_back(job: Job) -> None:
    hist = job.options.setdefault("history", [])
    if hist:
        job.options["step"] = hist.pop()
    job.options.pop("await", None)


def render(job: Job) -> tuple[str, Any]:
    """(text, keyboard) for the current wizard step."""
    step = job.options.get("step", "res")
    header = messages.wizard_header(job)
    jid = job.id
    if step == "res":
        return header + "Choose the output resolution:", \
            keyboards.resolution_menu(jid, src_short_side(job))
    if step == "upscale":
        p = probe_of(job)
        src = f"{p.get('width', '?')}×{p.get('height', '?')}"
        return (header + f"⚠️ The selected resolution is larger than the "
                f"source ({src}). Upscaling adds file size without adding "
                f"detail.", keyboards.upscale_confirm_menu(jid))
    if step == "codec":
        return header + "Choose the video codec:", \
            keyboards.codec_menu(jid, bool(job.options.get("av1_ok")))
    if step == "qual":
        return header + "Choose the quality mode:", keyboards.quality_menu(jid)
    if step == "aud":
        return header + "Choose the audio codec:", keyboards.audio_menu(jid)
    if step == "subsrc":
        return header + "Subtitles:", \
            keyboards.subtitle_menu(jid, has_embedded_subs(job))
    if step == "substream":
        return header + "Pick the embedded subtitle stream:", \
            keyboards.embedded_subtitle_menu(jid, probe_of(job).get("subs") or [])
    if step == "subdisp":
        return header + "Burn the subtitle in, or mux it as a separate track?", \
            keyboards.subtitle_disposition_menu(jid)
    if step == "confirm":
        return header + "Ready to start?", keyboards.confirm_menu(jid)
    return header + "Choose an option:", keyboards.confirm_menu(jid)


async def handle_selection(
    rt: Runtime, job: Job, verb: str, arg: str
) -> tuple[str, Any, str | None]:
    """Apply one button press. Returns (text, keyboard, alert_or_None)."""
    o = job.options
    alert: str | None = None

    if verb == "r":
        if arg == "orig":
            o["resolution"] = "orig"
            advance(job)
        elif arg == "cust":
            o["await"] = "res"
            await _save(rt, job)
            return ("Send the resolution as <code>1280x720</code> or just "
                    "<code>720</code>.", keyboards.cancel_only(job.id), None)
        elif arg in _RES_ARGS:
            o["resolution"] = int(arg)
            short = src_short_side(job)
            if short and int(arg) > short:
                o["history"] = o.get("history", []) + [o.get("step")]
                o["step"] = "upscale"
            else:
                advance(job)
        else:
            alert = "Unknown resolution."

    elif verb == "u":
        if arg == "yes":
            o["allow_upscale"] = True
        else:
            o["resolution"] = "orig"
            o["allow_upscale"] = False
        o["step"] = o.get("main_step", "res")
        advance(job)

    elif verb == "c":
        if arg in ("x264", "x265", "av1", "copy"):
            o["codec"] = arg
            if arg == "copy" and (str(o.get("resolution", "orig")) != "orig"
                                  or o.get("subtitle_mode") == "hard"):
                alert = ("Stream copy can't change resolution or burn "
                         "subtitles — pick a real codec.")
            else:
                advance(job)
                if arg == "copy" and o.get("step") == "qual":
                    advance(job)  # copy has no quality settings
        else:
            alert = "Unknown codec."

    elif verb == "q":
        if arg in ("small", "bal", "high"):
            o["quality"] = arg
            advance(job)
        elif arg == "crf":
            o["await"] = "crf"
            await _save(rt, job)
            return ("Send the CRF value (0–51, lower = better quality; "
                    "18–28 is typical).", keyboards.cancel_only(job.id), None)
        elif arg == "size":
            o["await"] = "size"
            await _save(rt, job)
            return ("Send the target size in MB, e.g. <code>250</code>.",
                    keyboards.cancel_only(job.id), None)
        elif arg == "br":
            o["await"] = "bitrate"
            await _save(rt, job)
            return ("Send the video bitrate in kbps, e.g. <code>2500</code>.",
                    keyboards.cancel_only(job.id), None)
        else:
            alert = "Unknown quality option."

    elif verb == "au":
        if arg in ("aac", "opus", "copy"):
            o["audio"] = arg
            advance(job)
        else:
            alert = "Unknown audio option."

    elif verb == "s":
        if arg == "none":
            o["subtitle_mode"] = "none"
            o.pop("subtitle_source", None)
            o["step"] = "confirm"
        elif arg == "emb":
            if not has_embedded_subs(job):
                alert = "This file has no embedded subtitles."
            else:
                o["history"] = o.get("history", []) + [o.get("step")]
                o["step"] = "substream"
        elif arg == "upl":
            o["await"] = "subtitle"
            await _save(rt, job)
            return ("Send the subtitle file now (.ass, .ssa, .srt, .vtt, "
                    ".sub or .txt).", keyboards.cancel_only(job.id), None)
        elif arg in ("hard", "soft"):
            o["subtitle_mode"] = arg
            o["step"] = o.get("main_step", "subsrc")
            advance(job)
        else:
            alert = "Unknown subtitle option."

    elif verb == "ss":
        subs = probe_of(job).get("subs") or []
        stream = next((s for s in subs if str(s.get("type_index")) == arg), None)
        if stream is None:
            alert = "Stream not found."
        elif stream.get("bitmap"):
            alert = ("That is a bitmap subtitle (PGS/VobSub) — it cannot be "
                     "hardsubbed without OCR, which isn't supported.")
        else:
            o["subtitle_source"] = "embedded"
            o["subtitle_stream"] = int(arg)
            if job.kind == JobKind.HARDSUB:
                o["subtitle_mode"] = "hard"
                o["step"] = o.get("main_step", "subsrc")
                advance(job)
            else:
                o["step"] = "subdisp"

    elif verb == "bk":
        go_back(job)

    await _save(rt, job)
    if alert:
        text, kb = render(job)
        return text, kb, alert
    text, kb = render(job)
    return text, kb, None


async def after_subtitle_upload(rt: Runtime, job: Job) -> tuple[str, Any]:
    """Called by the media handler once a subtitle file is saved."""
    o = job.options
    o["subtitle_source"] = "upload"
    o.pop("await", None)
    if job.kind == JobKind.HARDSUB or job.kind == JobKind.SEEDR_HARDSUB:
        o["subtitle_mode"] = "hard"
        o["step"] = o.get("main_step", "subsrc")
        advance(job)
    else:
        o["step"] = "subdisp"
    await _save(rt, job)
    return render(job)


async def apply_awaited_text(rt: Runtime, job: Job, text: str) -> tuple[str, Any]:
    """Custom CRF / target size / resolution typed by the user.
    Raises ValidationError with a user-facing message on bad input."""
    from bot.utils.validation import (
        validate_bitrate_kbps, validate_crf, validate_custom_resolution,
        validate_target_size_mb,
    )
    o = job.options
    awaited = o.get("await")
    if awaited == "crf":
        o["crf"] = validate_crf(text.strip())
        o["quality"] = "crf"
        advance(job)
    elif awaited == "size":
        o["target_size_mb"] = validate_target_size_mb(text.strip())
        o["quality"] = "size"
        advance(job)
    elif awaited == "bitrate":
        o["video_bitrate"] = validate_bitrate_kbps(text.strip())
        o["quality"] = "bitrate"
        advance(job)
    elif awaited == "res":
        result = validate_custom_resolution(text.strip())
        if isinstance(result, tuple):
            o["resolution"] = list(result)
        else:
            o["resolution"] = result
        short = src_short_side(job)
        wanted = min(result) if isinstance(result, tuple) else result
        if short and wanted > short:
            o["history"] = o.get("history", []) + [o.get("step")]
            o["step"] = "upscale"
        else:
            advance(job)
    else:
        raise ValueError("nothing awaited")
    o.pop("await", None)
    await _save(rt, job)
    return render(job)


async def start_confirmed(rt: Runtime, job: Job) -> str:
    """Final Go pressed. Returns the text to show. Seedr parents spawn child
    jobs; everything else is enqueued."""
    if job.kind == JobKind.SEEDR_HARDSUB and job.input_type == InputType.MAGNET:
        n, summary = await spawn_seedr_children(rt, job)
        await rt.repo.transition(job.id, JobStatus.COMPLETED,
                                 detail=f"spawned {n} episode job(s)")
        from bot.ui.panels import SEP
        return (f"🚀 <b>Queued {n} episode(s)</b>\n{SEP}\n"
                f"{summary}\n\n<i>Using your /settings — track with /queue.</i>")
    await rt.manager.enqueue(job.id)
    return "🚀 Queued. Progress will appear here."


async def spawn_seedr_children(rt: Runtime, parent: Job) -> tuple[int, str]:
    """Create one child job per selected episode using the user's /settings.
    Returns (count, human summary of the settings applied)."""
    from bot.services.user_settings import get_user_settings, value_of
    try:
        settings = await get_user_settings(rt.db, parent.user_id)
    except Exception:  # noqa: BLE001
        settings = {}
    seedr_hardsub_enabled = value_of(settings, "seedr_hardsub") == "on"

    # Encode options come from the user's saved settings — magnets start
    # directly, no per-magnet wizard.
    _res = value_of(settings, "def_resolution")
    inherited = {
        "resolution": "orig" if _res == "original" else _res,
        "codec": value_of(settings, "def_codec"),
        "quality": "crf",
        "crf": int(value_of(settings, "def_crf")),
        "audio": value_of(settings, "def_audio"),
    }
    _lang = value_of(settings, "sub_lang")
    summary = (
        f"💬 {'🇫🇷' if _lang == 'fr' else _lang.upper()} hardsub · "
        f"🖥 {_res} · 🎯 {value_of(settings, 'def_codec')} · "
        f"🎛 CRF {inherited['crf']} · ⏩ {value_of(settings, 'def_preset')}"
        if seedr_hardsub_enabled else
        f"🚫 no subs · 🖥 {_res} · 🎯 {value_of(settings, 'def_codec')}"
    )

    s = parent.seedr
    matches: list[dict] = s.get("matches") or []
    selected: list[int] = s.get("selected") or []
    count = 0
    for idx in selected:
        if not 0 <= idx < len(matches):
            continue
        m = matches[idx]
        has_sub = bool(m.get("sub_id"))
        # Seedr episodes are hardsubbed by default: use the matched external
        # subtitle if there is one, otherwise burn the video's own embedded
        # subtitle (embedded_auto). The worker degrades to a plain encode only
        # if the file genuinely has no text subtitle. The user's "Seedr
        # hardsub" setting turns this off.
        want_subs = seedr_hardsub_enabled
        if not want_subs:
            sub_mode, sub_source = "none", ""
        elif has_sub:
            sub_mode, sub_source = "hard", "seedr"
        else:
            sub_mode, sub_source = "hard", "embedded_auto"
        child = Job(
            id=new_job_id(),
            user_id=parent.user_id,
            chat_id=parent.chat_id,
            kind=JobKind.HARDSUB if sub_mode == "hard" else JobKind.ENCODE,
            input_type=InputType.SEEDR_FILE,
            input_ref=str(m["video_id"]),
            input_name=str(m.get("video_label") or "episode.mkv"),
            input_size=int(m.get("video_size") or 0),
            options={
                **inherited,
                "subtitle_mode": sub_mode,
                "subtitle_source": sub_source,
            },
            seedr={
                "account": s.get("account", ""),
                "folder_id": s.get("folder_id"),
                "sub_id": m.get("sub_id"),
                "sub_name": (m.get("subtitle") or "").rsplit("/", 1)[-1],
                "parent": parent.id,
            },
        )
        work = create_job_dirs(rt.config.work_dir, child.user_id, child.id)
        child.work_dir = str(work)
        try:
            status_msg = await rt.client.send_message(
                parent.chat_id,
                f"📥 Queued: <code>{child.input_name[:60]}</code>",
            )
            child.status_msg_id = status_msg.id
        except Exception:  # noqa: BLE001 — job must exist even if msg fails
            log.exception("Could not send child status message")
        await rt.repo.insert(child)
        await rt.manager.enqueue(child.id)
        count += 1
    return count, summary


async def _save(rt: Runtime, job: Job) -> None:
    await rt.repo.update_fields(job.id, options=job.options)
