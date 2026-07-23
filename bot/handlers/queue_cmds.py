"""/queue, /cancel, /history."""
from __future__ import annotations

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from bot.jobs.states import ACTIVE, TERMINAL, JobStatus
from bot.runtime import get_runtime
from bot.ui import messages


@Client.on_message(filters.command("queue") & filters.private)
async def cmd_queue(_: Client, message: Message) -> None:
    rt = get_runtime()
    uid = message.from_user.id
    jobs = await rt.repo.user_jobs(uid, limit=30)
    if uid == rt.config.owner_id:
        # The owner sees everything currently moving.
        moving = await rt.repo.by_status(JobStatus.QUEUED, *ACTIVE)
        seen = {j.id for j in jobs}
        jobs += [j for j in moving if j.id not in seen]

    open_jobs = [j for j in jobs if j.status not in TERMINAL]
    recent_done = [j for j in jobs if j.status in TERMINAL][:8]
    lines = ["<b>📋 Queue</b>", ""]
    if open_jobs:
        lines += [messages.job_line(j) for j in open_jobs]
    else:
        lines.append("<i>No active or waiting jobs.</i>")
    if recent_done:
        lines += ["", "<b>Recently finished</b>"]
        lines += [messages.job_line(j) for j in recent_done]
    lines += ["", "Cancel with <code>/cancel &lt;job id&gt;</code>."]
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(_: Client, message: Message) -> None:
    rt = get_runtime()
    uid = message.from_user.id
    args = message.command[1:]

    if args:
        job_id = args[0].strip().lower()
        job = await rt.repo.get(job_id)
        if job is None or (job.user_id != uid and uid != rt.config.owner_id):
            await message.reply_text("Job not found.")
            return
        ok = await rt.manager.cancel_job(job_id)
        await message.reply_text(
            f"🚫 Cancellation requested for <code>{job_id}</code>."
            if ok else "That job is already finished.",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    open_jobs = [j for j in await rt.repo.user_jobs(uid, limit=30)
                 if j.status not in TERMINAL]
    if not open_jobs:
        await message.reply_text("You have no running jobs.")
        return
    if len(open_jobs) == 1:
        await rt.manager.cancel_job(open_jobs[0].id)
        await message.reply_text(
            f"🚫 Cancelled <code>{open_jobs[0].id}</code>.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    lines = ["You have several jobs — pick one:", ""]
    lines += [messages.job_line(j) for j in open_jobs]
    lines += ["", "Use <code>/cancel &lt;job id&gt;</code>."]
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("history") & filters.private)
async def cmd_history(_: Client, message: Message) -> None:
    rt = get_runtime()
    jobs = await rt.repo.user_jobs(message.from_user.id, limit=15)
    if not jobs:
        await message.reply_text("No jobs yet.")
        return
    lines = ["<b>🗂 Your recent jobs</b>", ""]
    lines += [messages.job_line(j) for j in jobs]
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)
