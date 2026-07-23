"""Job state machine transitions and callback-data codec."""
from __future__ import annotations

import pytest

from bot.jobs.states import InvalidTransition, JobStatus, check_transition
from bot.ui.callbacks import Callback, pack, parse


def test_normal_lifecycle():
    path = [JobStatus.CREATED, JobStatus.QUEUED, JobStatus.DOWNLOADING,
            JobStatus.PROBING, JobStatus.ENCODING, JobStatus.UPLOADING,
            JobStatus.COMPLETED]
    for src, dst in zip(path, path[1:]):
        check_transition(src, dst)


def test_stage_skipping_allowed():
    check_transition(JobStatus.PROBING, JobStatus.ENCODING)  # no subs needed


def test_cancel_from_anywhere_active():
    for src in (JobStatus.CREATED, JobStatus.QUEUED, JobStatus.DOWNLOADING,
                JobStatus.ENCODING, JobStatus.UPLOADING,
                JobStatus.WAITING_FOR_INPUT):
        check_transition(src, JobStatus.CANCELLED)


def test_terminal_states_frozen():
    with pytest.raises(InvalidTransition):
        check_transition(JobStatus.COMPLETED, JobStatus.QUEUED)
    with pytest.raises(InvalidTransition):
        check_transition(JobStatus.CANCELLED, JobStatus.ENCODING)


def test_no_backwards_pipeline():
    with pytest.raises(InvalidTransition):
        check_transition(JobStatus.ENCODING, JobStatus.DOWNLOADING)


def test_failed_can_retry():
    check_transition(JobStatus.FAILED, JobStatus.QUEUED)


def test_seedr_parent_flow_transitions():
    # magnet job pauses for episode selection mid-download, then completes
    # by spawning children
    check_transition(JobStatus.DOWNLOADING, JobStatus.WAITING_FOR_INPUT)
    check_transition(JobStatus.WAITING_FOR_INPUT, JobStatus.COMPLETED)


def test_cached_mediainfo_completes_from_created():
    check_transition(JobStatus.CREATED, JobStatus.COMPLETED)


# ── Callback codec ───────────────────────────────────────────────────────

def test_pack_parse_roundtrip():
    data = pack("r", "abc12345", "720")
    cb = parse(data)
    assert cb == Callback("r", "abc12345", "720")
    assert len(data.encode()) <= 64


def test_parse_rejects_garbage():
    assert parse("") is None
    assert parse("nonsense") is None
    assert parse("z|zz|abc12345|x") is None          # unknown verb
    assert parse("y|r|abc12345|720") is None         # wrong prefix
    assert parse("z|r|../../etc|720") is None        # bad job id
    assert parse("z|r|abc12345|" + "x" * 60) is None  # oversized arg


def test_pack_length_guard():
    with pytest.raises(ValueError):
        Callback("fs", "abc12345", "x" * 60).pack()
