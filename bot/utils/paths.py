"""Per-job working directories.

Layout:  <work_dir>/<user_id>/<job_id>/{input,subtitles,fonts,output,logs}
Cleanup only ever deletes directories that match this exact layout, so a bug
can never wipe an unrelated path.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_SUBDIRS = ("input", "subtitles", "fonts", "output", "logs")
_JOB_ID_RE = re.compile(r"^[a-z0-9]{8}$")


def create_job_dirs(work_root: Path, user_id: int, job_id: str) -> Path:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError(f"Bad job id: {job_id!r}")
    job_dir = work_root / str(user_id) / job_id
    for sub in _SUBDIRS:
        (job_dir / sub).mkdir(parents=True, exist_ok=True)
    return job_dir


def is_job_dir(work_root: Path, path: Path) -> bool:
    """True only for <work_root>/<numeric uid>/<8-char job id>."""
    try:
        rel = path.resolve().relative_to(work_root.resolve())
    except (ValueError, OSError):
        return False
    parts = rel.parts
    return (
        len(parts) == 2 and parts[0].lstrip("-").isdigit()
        and bool(_JOB_ID_RE.match(parts[1]))
    )


def remove_job_dir(work_root: Path, path: Path) -> bool:
    """Delete a job directory — refuses anything that isn't one."""
    if not is_job_dir(work_root, path):
        log.warning("Refusing to delete non-job directory: %s", path)
        return False
    shutil.rmtree(path, ignore_errors=True)
    # Drop the parent user dir too if it is now empty.
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return True


def free_disk_bytes(path: Path) -> int:
    usage = shutil.disk_usage(path if path.exists() else path.parent)
    return usage.free
