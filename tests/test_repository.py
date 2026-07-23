"""Persistence: insert, transitions, cancellation flags, recovery queries.
Uses a real SQLite database in a temp directory."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.database.connection import Database
from bot.jobs.models import InputType, Job, JobKind, new_job_id
from bot.jobs.repository import JobRepository
from bot.jobs.states import InvalidTransition, JobStatus


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield JobRepository(db)
    await db.close()


def _job(**kw) -> Job:
    defaults = dict(
        id=new_job_id(), user_id=111, chat_id=111, kind=JobKind.ENCODE,
        input_type=InputType.TG_VIDEO, input_ref="fid", input_name="a.mkv",
    )
    defaults.update(kw)
    return Job(**defaults)


async def test_insert_and_get(repo: JobRepository):
    job = _job(options={"resolution": 720}, seedr={"folder_id": 5})
    await repo.insert(job)
    got = await repo.get(job.id)
    assert got is not None
    assert got.options == {"resolution": 720}
    assert got.seedr == {"folder_id": 5}
    assert got.status == JobStatus.CREATED


async def test_valid_transition_records_event(repo: JobRepository):
    job = _job()
    await repo.insert(job)
    await repo.transition(job.id, JobStatus.QUEUED)
    await repo.transition(job.id, JobStatus.DOWNLOADING)
    got = await repo.get(job.id)
    assert got.status == JobStatus.DOWNLOADING
    assert got.started_at is not None


async def test_invalid_transition_rejected(repo: JobRepository):
    job = _job()
    await repo.insert(job)
    await repo.transition(job.id, JobStatus.QUEUED)
    await repo.transition(job.id, JobStatus.DOWNLOADING)
    with pytest.raises(InvalidTransition):
        await repo.transition(job.id, JobStatus.QUEUED)
    # state unchanged after failed transition
    assert (await repo.get(job.id)).status == JobStatus.DOWNLOADING


async def test_terminal_sets_finished(repo: JobRepository):
    job = _job()
    await repo.insert(job)
    await repo.transition(job.id, JobStatus.QUEUED)
    await repo.transition(job.id, JobStatus.CANCELLED)
    got = await repo.get(job.id)
    assert got.finished_at is not None


async def test_cancel_flag(repo: JobRepository):
    job = _job()
    await repo.insert(job)
    await repo.request_cancel(job.id)
    assert (await repo.get(job.id)).cancel_requested


async def test_open_count_per_user(repo: JobRepository):
    for _ in range(3):
        await repo.insert(_job())
    done = _job()
    await repo.insert(done)
    await repo.transition(done.id, JobStatus.CANCELLED)
    assert await repo.count_open_for_user(111) == 3


async def test_awaiting_job_lookup(repo: JobRepository):
    job = _job(options={"await": "crf"})
    await repo.insert(job)
    found = await repo.awaiting_job_for_user(111)
    assert found is not None and found.id == job.id
    assert await repo.awaiting_job_for_user(222) is None


async def test_by_status_for_recovery(repo: JobRepository):
    j1 = _job()
    await repo.insert(j1)
    await repo.transition(j1.id, JobStatus.QUEUED)
    j2 = _job()
    await repo.insert(j2)
    await repo.transition(j2.id, JobStatus.QUEUED)
    await repo.transition(j2.id, JobStatus.ENCODING)
    queued = await repo.by_status(JobStatus.QUEUED)
    active = await repo.by_status(JobStatus.ENCODING)
    assert [j.id for j in queued] == [j1.id]
    assert [j.id for j in active] == [j2.id]


async def test_error_truncated(repo: JobRepository):
    job = _job()
    await repo.insert(job)
    await repo.transition(job.id, JobStatus.FAILED, error="x" * 10_000)
    assert len((await repo.get(job.id)).error) <= 4000
