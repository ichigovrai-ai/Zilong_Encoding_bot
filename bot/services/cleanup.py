"""Working-directory hygiene: per-job cleanup and conservative stale sweeps."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from bot.jobs.repository import JobRepository
from bot.utils.paths import is_job_dir, remove_job_dir

log = logging.getLogger(__name__)


async def cleanup_job_dir(repo: JobRepository, work_root: Path, job_id: str) -> None:
    """Remove one job's working directory and mark it cleaned."""
    job = await repo.get(job_id)
    if job is None:
        return
    if job.work_dir:
        remove_job_dir(work_root, Path(job.work_dir))
    await repo.update_fields(job_id, cleaned=True)


async def sweep_stale(repo: JobRepository, work_root: Path,
                      retention_hours: float) -> int:
    """Clean terminal jobs older than the retention window, plus orphaned
    job-shaped directories that no live DB row references. Never touches
    directories belonging to active jobs and never deletes non-job paths."""
    removed = 0
    cutoff = time.time() - retention_hours * 3600

    for job in await repo.stale_terminal_jobs(cutoff):
        if job.work_dir:
            remove_job_dir(work_root, Path(job.work_dir))
        await repo.update_fields(job.id, cleaned=True)
        removed += 1

    # Orphan sweep: directories matching the job layout with no DB row.
    if work_root.exists():
        for user_dir in work_root.iterdir():
            if not user_dir.is_dir():
                continue
            for job_dir in user_dir.iterdir():
                if not is_job_dir(work_root, job_dir):
                    continue
                if await repo.get(job_dir.name) is None:
                    try:
                        age = time.time() - job_dir.stat().st_mtime
                    except OSError:
                        continue
                    if age > 6 * 3600:  # conservative: only clearly abandoned
                        remove_job_dir(work_root, job_dir)
                        removed += 1
    if removed:
        log.info("Cleanup sweep removed %d job directories", removed)
    return removed
