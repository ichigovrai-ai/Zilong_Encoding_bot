"""Application-wide context shared with pyrogram plugin handlers.

Pyrogram's plugin loader imports handler modules without arguments, so
dependencies are provided through this module instead of constructor
injection. `set_runtime()` is called exactly once in app startup; tests build
their own Runtime with fakes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.config import Config
    from bot.database.connection import Database
    from bot.jobs.manager import JobManager
    from bot.jobs.repository import JobRepository


@dataclass(slots=True)
class Runtime:
    config: "Config"
    db: "Database"
    repo: "JobRepository"
    manager: "JobManager"
    client: Any = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    presets: dict[str, dict] = field(default_factory=dict)


_runtime: Runtime | None = None


def set_runtime(rt: Runtime) -> None:
    global _runtime
    _runtime = rt


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Runtime not initialised")
    return _runtime
