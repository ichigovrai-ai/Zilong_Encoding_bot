"""Job queue manager.

Owns per-stage concurrency semaphores, running task registry, cancellation
tokens, restart recovery and periodic cleanup. Persistence lives in
JobRepository — the manager holds no job state of its own beyond live tasks.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from bot.jobs.models import Job
from bot.jobs.repository import JobRepository
from bot.jobs.states import ACTIVE, TERMINAL, JobStatus
from bot.utils.subprocesses import CancelToken

if TYPE_CHECKING:
    from bot.config import Config

log = logging.getLogger(__name__)

_WAITING_TTL_S = 4 * 3600  # abandon wizard sessions after 4 h


class JobManager:
    def __init__(self, config: "Config", repo: JobRepository):
        self._config = config
        self._repo = repo
        self.download_sem = asyncio.Semaphore(config.max_concurrent_downloads)
        self.encode_sem = asyncio.Semaphore(config.max_concurrent_encodes)
        self.upload_sem = asyncio.Semaphore(config.max_concurrent_uploads)
        self._tasks: dict[str, asyncio.Task] = {}
        self._tokens: dict[str, CancelToken] = {}
        self._wake = asyncio.Event()
        self._stopping = False
        self._loops: list[asyncio.Task] = []

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._recover()
        self._loops.append(asyncio.create_task(self._scheduler_loop(),
                                               name="job-scheduler"))
        self._loops.append(asyncio.create_task(self._janitor_loop(),
                                               name="job-janitor"))

    async def stop(self) -> None:
        """Graceful shutdown: stop scheduling, terminate running stages.
        Interrupted jobs are re-marked by recovery on the next start."""
        self._stopping = True
        for t in self._loops:
            t.cancel()
        for token in self._tokens.values():
            token.cancel()
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self._loops, return_exceptions=True)

    async def _recover(self) -> None:
        """Reconcile DB state after a restart."""
        from bot.utils.paths import remove_job_dir
        from pathlib import Path

        interrupted = await self._repo.by_status(*ACTIVE)
        for job in interrupted:
            log.warning("Recovery: job %s was %s — marking failed", job.id, job.status)
            try:
                await self._repo.transition(
                    job.id, JobStatus.FAILED,
                    error="The bot restarted while this job was running. "
                          "Please submit it again.",
                    detail="interrupted by restart",
                )
            except Exception:  # noqa: BLE001
                log.exception("Recovery transition failed for %s", job.id)
            if job.work_dir:
                remove_job_dir(self._config.work_dir, Path(job.work_dir))
                await self._repo.update_fields(job.id, cleaned=True)

        queued = await self._repo.by_status(JobStatus.QUEUED)
        if queued:
            log.info("Recovery: %d queued job(s) will be re-run", len(queued))
        stale_cutoff = time.time() - _WAITING_TTL_S
        for job in await self._repo.by_status(JobStatus.CREATED,
                                              JobStatus.WAITING_FOR_INPUT):
            if job.created_at < stale_cutoff:
                try:
                    await self._repo.transition(job.id, JobStatus.CANCELLED,
                                                detail="abandoned wizard")
                except Exception:  # noqa: BLE001
                    pass

    # ── Queueing ────────────────────────────────────────────────────────

    async def user_can_submit(self, user_id: int) -> str | None:
        """None if allowed, else a user-facing refusal reason."""
        open_jobs = await self._repo.count_open_for_user(user_id)
        if open_jobs >= self._config.max_jobs_per_user:
            return (f"You already have {open_jobs} unfinished jobs "
                    f"(limit {self._config.max_jobs_per_user}). "
                    f"Wait for them or /cancel some.")
        return None

    async def enqueue(self, job_id: str) -> None:
        await self._repo.transition(job_id, JobStatus.QUEUED, stage="queued")
        self._wake.set()

    def token_for(self, job_id: str) -> CancelToken:
        if job_id not in self._tokens:
            self._tokens[job_id] = CancelToken()
        return self._tokens[job_id]

    # ── Cancellation ────────────────────────────────────────────────────

    async def cancel_job(self, job_id: str) -> bool:
        """Request cancellation. Returns True if the job was cancellable."""
        job = await self._repo.get(job_id)
        if job is None or job.status in TERMINAL:
            return False
        await self._repo.request_cancel(job_id)
        token = self._tokens.get(job_id)
        if token:
            token.cancel()
        if job_id not in self._tasks:
            # Not running (wizard / queued) — finalise directly.
            try:
                await self._repo.transition(job_id, JobStatus.CANCELLED,
                                            detail="cancelled before start")
            except Exception:  # noqa: BLE001
                log.exception("Cancel transition failed for %s", job_id)
            await self._cleanup_dir(job_id)
        return True

    async def _cleanup_dir(self, job_id: str) -> None:
        from bot.services.cleanup import cleanup_job_dir
        try:
            await cleanup_job_dir(self._repo, self._config.work_dir, job_id)
        except Exception:  # noqa: BLE001
            log.exception("Cleanup failed for %s", job_id)

    # ── Loops ───────────────────────────────────────────────────────────

    async def _scheduler_loop(self) -> None:
        while not self._stopping:
            try:
                queued = await self._repo.by_status(JobStatus.QUEUED)
                for job in queued:
                    if job.id in self._tasks or len(self._tasks) >= 64:
                        continue
                    self._spawn(job)
            except Exception:  # noqa: BLE001
                log.exception("Scheduler tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    def _spawn(self, job: Job) -> None:
        from bot.jobs.worker import run_job
        token = self.token_for(job.id)
        task = asyncio.create_task(run_job(job.id, token), name=f"job-{job.id}")
        self._tasks[job.id] = task

        def _done(t: asyncio.Task) -> None:
            self._tasks.pop(job.id, None)
            self._tokens.pop(job.id, None)
            if not t.cancelled() and (exc := t.exception()):
                log.error("Job task %s crashed: %s", job.id, exc, exc_info=exc)
            self._wake.set()

        task.add_done_callback(_done)

    async def _janitor_loop(self) -> None:
        from bot.services.cleanup import sweep_stale
        while not self._stopping:
            try:
                await sweep_stale(self._repo, self._config.work_dir,
                                  self._config.job_retention_hours)
            except Exception:  # noqa: BLE001
                log.exception("Janitor sweep failed")
            await asyncio.sleep(1800)
