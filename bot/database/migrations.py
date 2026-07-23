"""Versioned schema migrations. Each entry runs once, in order."""
from __future__ import annotations

import aiosqlite

MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE users (
        user_id     INTEGER PRIMARY KEY,
        first_seen  REAL NOT NULL,
        last_seen   REAL NOT NULL
    );

    CREATE TABLE user_settings (
        user_id  INTEGER PRIMARY KEY,
        settings TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE jobs (
        id            TEXT PRIMARY KEY,
        user_id       INTEGER NOT NULL,
        chat_id       INTEGER NOT NULL,
        kind          TEXT NOT NULL,
        status        TEXT NOT NULL,
        stage         TEXT NOT NULL DEFAULT '',
        input_type    TEXT NOT NULL DEFAULT '',
        input_ref     TEXT NOT NULL DEFAULT '',
        input_name    TEXT NOT NULL DEFAULT '',
        input_size    INTEGER NOT NULL DEFAULT 0,
        options       TEXT NOT NULL DEFAULT '{}',
        seedr         TEXT NOT NULL DEFAULT '{}',
        progress      REAL NOT NULL DEFAULT 0,
        status_msg_id INTEGER NOT NULL DEFAULT 0,
        error         TEXT NOT NULL DEFAULT '',
        work_dir      TEXT NOT NULL DEFAULT '',
        output_path   TEXT NOT NULL DEFAULT '',
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        cleaned       INTEGER NOT NULL DEFAULT 0,
        created_at    REAL NOT NULL,
        started_at    REAL,
        finished_at   REAL
    );
    CREATE INDEX idx_jobs_status ON jobs(status);
    CREATE INDEX idx_jobs_user ON jobs(user_id, created_at);

    CREATE TABLE job_events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id  TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        ts      REAL NOT NULL,
        event   TEXT NOT NULL,
        detail  TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_job_events_job ON job_events(job_id);

    CREATE TABLE presets (
        name    TEXT PRIMARY KEY,
        config  TEXT NOT NULL,
        builtin INTEGER NOT NULL DEFAULT 0
    );
    """,
]


async def apply_migrations(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    async with conn.execute("SELECT version FROM schema_version") as cur:
        row = await cur.fetchone()
    current = row[0] if row else 0

    for i, script in enumerate(MIGRATIONS[current:], start=current + 1):
        await conn.executescript(script)
        if current == 0 and i == 1:
            await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (i,))
        else:
            await conn.execute("UPDATE schema_version SET version = ?", (i,))
        current = i
    await conn.commit()
