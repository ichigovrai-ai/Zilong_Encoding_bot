"""Job dataclass and (de)serialisation to SQLite rows."""
from __future__ import annotations

import json
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Any

from bot.jobs.states import JobStatus

_ALPHABET = string.ascii_lowercase + string.digits


def new_job_id() -> str:
    """8-char lowercase token — compact enough for callback data."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))


class InputType:
    TG_VIDEO = "tg_video"
    TG_DOCUMENT = "tg_document"
    URL = "url"
    MAGNET = "magnet"
    TORRENT_FILE = "torrent_file"
    SEEDR_FILE = "seedr_file"


class JobKind:
    ENCODE = "encode"
    COMPRESS = "compress"
    CONVERT = "convert"
    HARDSUB = "hardsub"
    SEEDR_HARDSUB = "seedr_hardsub"
    EXTRACT = "extract"
    MEDIAINFO = "mediainfo"
    CONVERTSUB = "convertsub"


@dataclass(slots=True)
class Job:
    id: str
    user_id: int
    chat_id: int
    kind: str
    status: JobStatus = JobStatus.CREATED
    stage: str = ""
    input_type: str = ""
    input_ref: str = ""
    input_name: str = ""
    input_size: int = 0
    # Wizard selections + encode plan. Never serialised into callback data.
    options: dict[str, Any] = field(default_factory=dict)
    # Seedr context: torrent_id, folder_id, file ids, matches …
    seedr: dict[str, Any] = field(default_factory=dict)
    progress: float = 0.0
    status_msg_id: int = 0
    error: str = ""
    work_dir: str = ""
    output_path: str = ""
    cancel_requested: bool = False
    cleaned: bool = False
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        from bot.jobs.states import TERMINAL
        return self.status in TERMINAL

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "kind": self.kind,
            "status": str(self.status),
            "stage": self.stage,
            "input_type": self.input_type,
            "input_ref": self.input_ref,
            "input_name": self.input_name,
            "input_size": self.input_size,
            "options": json.dumps(self.options, ensure_ascii=False),
            "seedr": json.dumps(self.seedr, ensure_ascii=False),
            "progress": self.progress,
            "status_msg_id": self.status_msg_id,
            "error": self.error,
            "work_dir": self.work_dir,
            "output_path": self.output_path,
            "cancel_requested": int(self.cancel_requested),
            "cleaned": int(self.cleaned),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Job":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            kind=row["kind"],
            status=JobStatus(row["status"]),
            stage=row["stage"],
            input_type=row["input_type"],
            input_ref=row["input_ref"],
            input_name=row["input_name"],
            input_size=row["input_size"],
            options=json.loads(row["options"] or "{}"),
            seedr=json.loads(row["seedr"] or "{}"),
            progress=row["progress"],
            status_msg_id=row["status_msg_id"],
            error=row["error"],
            work_dir=row["work_dir"],
            output_path=row["output_path"],
            cancel_requested=bool(row["cancel_requested"]),
            cleaned=bool(row["cleaned"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
