"""SQLite persistence for jobs. All state changes go through here so
transitions are validated and recorded exactly once."""
from __future__ import annotations

import json
import time
from typing import Any

from bot.database.connection import Database
from bot.jobs.models import Job
from bot.jobs.states import ACTIVE, TERMINAL, JobStatus, check_transition

_COLS = (
    "id", "user_id", "chat_id", "kind", "status", "stage", "input_type",
    "input_ref", "input_name", "input_size", "options", "seedr", "progress",
    "status_msg_id", "error", "work_dir", "output_path", "cancel_requested",
    "cleaned", "created_at", "started_at", "finished_at",
)


class JobRepository:
    def __init__(self, db: Database):
        self._db = db

    async def insert(self, job: Job) -> None:
        row = job.to_row()
        cols = ", ".join(_COLS)
        marks = ", ".join("?" for _ in _COLS)
        async with self._db.transaction() as conn:
            await conn.execute(
                f"INSERT INTO jobs ({cols}) VALUES ({marks})",
                tuple(row[c] for c in _COLS),
            )
            await conn.execute(
                "INSERT INTO job_events (job_id, ts, event, detail) VALUES (?, ?, ?, ?)",
                (job.id, time.time(), "created", job.kind),
            )

    async def get(self, job_id: str) -> Job | None:
        row = await self._db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return Job.from_row(row) if row else None

    async def transition(self, job_id: str, new_status: JobStatus, *,
                         stage: str | None = None, error: str = "",
                         detail: str = "") -> Job:
        """Atomically move a job to `new_status`, validating the edge."""
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} not found")
            job = Job.from_row(row)
            check_transition(job.status, new_status)

            now = time.time()
            sets: dict[str, Any] = {"status": str(new_status)}
            if stage is not None:
                sets["stage"] = stage
            if error:
                sets["error"] = error[:4000]
            if new_status in ACTIVE and job.started_at is None:
                sets["started_at"] = now
            if new_status in TERMINAL:
                sets["finished_at"] = now

            assign = ", ".join(f"{k} = ?" for k in sets)
            await conn.execute(
                f"UPDATE jobs SET {assign} WHERE id = ?",
                (*sets.values(), job_id),
            )
            await conn.execute(
                "INSERT INTO job_events (job_id, ts, event, detail) VALUES (?, ?, ?, ?)",
                (job_id, now, str(new_status), detail or error[:500]),
            )
            job.status = new_status
            if stage is not None:
                job.stage = stage
            return job

    async def update_fields(self, job_id: str, **fields: Any) -> None:
        """Update non-status columns (options/seedr dicts are JSON-encoded)."""
        if not fields:
            return
        for key in ("options", "seedr"):
            if key in fields and isinstance(fields[key], dict):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        if "cancel_requested" in fields:
            fields["cancel_requested"] = int(bool(fields["cancel_requested"]))
        if "cleaned" in fields:
            fields["cleaned"] = int(bool(fields["cleaned"]))
        assert all(k in _COLS and k != "status" for k in fields), fields
        assign = ", ".join(f"{k} = ?" for k in fields)
        await self._db.execute(
            f"UPDATE jobs SET {assign} WHERE id = ?", (*fields.values(), job_id)
        )

    async def request_cancel(self, job_id: str) -> None:
        await self._db.execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,)
        )

    async def by_status(self, *statuses: JobStatus) -> list[Job]:
        marks = ", ".join("?" for _ in statuses)
        rows = await self._db.fetchall(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at",
            tuple(str(s) for s in statuses),
        )
        return [Job.from_row(r) for r in rows]

    async def user_jobs(self, user_id: int, limit: int = 20) -> list[Job]:
        rows = await self._db.fetchall(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [Job.from_row(r) for r in rows]

    async def count_open_for_user(self, user_id: int) -> int:
        terminal = tuple(str(s) for s in TERMINAL)
        marks = ", ".join("?" for _ in terminal)
        row = await self._db.fetchone(
            f"SELECT COUNT(*) AS n FROM jobs WHERE user_id = ? AND status NOT IN ({marks})",
            (user_id, *terminal),
        )
        return int(row["n"]) if row else 0

    async def awaiting_job_for_user(self, user_id: int) -> Job | None:
        """Most recent wizard job of this user that is waiting for a typed
        value or an uploaded subtitle (options.await is set)."""
        row = await self._db.fetchone(
            "SELECT * FROM jobs WHERE user_id = ? "
            "AND status IN ('created', 'waiting_for_input') "
            "AND json_extract(options, '$.await') IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        return Job.from_row(row) if row else None

    async def stale_terminal_jobs(self, older_than: float) -> list[Job]:
        terminal = tuple(str(s) for s in TERMINAL)
        marks = ", ".join("?" for _ in terminal)
        rows = await self._db.fetchall(
            f"SELECT * FROM jobs WHERE status IN ({marks}) AND cleaned = 0 "
            f"AND COALESCE(finished_at, created_at) < ?",
            (*terminal, older_than),
        )
        return [Job.from_row(r) for r in rows]
