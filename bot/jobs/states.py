"""Job status state machine.

Transitions are validated centrally — a job can only move along edges declared
in `ALLOWED`. `cancelled` is reachable from every non-terminal state because
the user may cancel at any stage.
"""
from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    CREATED = "created"
    WAITING_FOR_INPUT = "waiting_for_input"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROBING = "probing"
    PREPARING_SUBTITLES = "preparing_subtitles"
    ENCODING = "encoding"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLEANING = "cleaning"


TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})

ACTIVE = frozenset({
    JobStatus.DOWNLOADING, JobStatus.PROBING, JobStatus.PREPARING_SUBTITLES,
    JobStatus.ENCODING, JobStatus.UPLOADING, JobStatus.CLEANING,
})

_PIPELINE_ORDER = [
    JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.PROBING,
    JobStatus.PREPARING_SUBTITLES, JobStatus.ENCODING, JobStatus.UPLOADING,
]

ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    # CREATED → COMPLETED covers instant actions (cached /mediainfo).
    JobStatus.CREATED: frozenset({JobStatus.WAITING_FOR_INPUT, JobStatus.QUEUED,
                                  JobStatus.DOWNLOADING, JobStatus.COMPLETED,
                                  JobStatus.FAILED, JobStatus.CANCELLED}),
    # WAITING_FOR_INPUT → COMPLETED covers Seedr parents that finish by
    # spawning their episode child jobs.
    JobStatus.WAITING_FOR_INPUT: frozenset({JobStatus.WAITING_FOR_INPUT,
                                            JobStatus.QUEUED, JobStatus.DOWNLOADING,
                                            JobStatus.COMPLETED,
                                            JobStatus.FAILED, JobStatus.CANCELLED}),
    # Any pipeline stage may jump forward past stages it does not need
    # (e.g. a plain encode skips preparing_subtitles), may pause for user
    # input (Seedr episode selection during DOWNLOADING), and may fail or be
    # cancelled.
    **{
        s: frozenset(_PIPELINE_ORDER[i + 1:] +
                     [JobStatus.WAITING_FOR_INPUT, JobStatus.COMPLETED,
                      JobStatus.FAILED, JobStatus.CANCELLED])
        for i, s in enumerate(_PIPELINE_ORDER)
    },
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),      # retry
    JobStatus.CANCELLED: frozenset(),
    JobStatus.CLEANING: frozenset(TERMINAL),
}


class InvalidTransition(RuntimeError):
    def __init__(self, src: JobStatus, dst: JobStatus):
        super().__init__(f"Invalid job transition: {src} → {dst}")
        self.src, self.dst = src, dst


def check_transition(src: JobStatus, dst: JobStatus) -> None:
    if dst not in ALLOWED.get(src, frozenset()):
        raise InvalidTransition(src, dst)
