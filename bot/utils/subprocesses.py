"""Cancellable subprocess execution.

All external processes (ffmpeg/ffprobe/aria2c) run through here: argument
lists only (never shell=True), bounded timeouts, and graceful-then-forced
termination on cancellation.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

log = logging.getLogger(__name__)


class CancelToken:
    """Cooperative cancellation shared by a job's stages.

    Stages check `raise_if_cancelled()` between steps; long-running
    subprocesses register themselves so `cancel()` can terminate them.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._procs: set[asyncio.subprocess.Process] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise asyncio.CancelledError("job cancelled")

    def register(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.add(proc)
        if self._event.is_set():
            _terminate(proc)

    def unregister(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.discard(proc)

    def cancel(self) -> None:
        self._event.set()
        for proc in list(self._procs):
            _terminate(proc)

    async def wait(self) -> None:
        await self._event.wait()


def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Ask nicely first; the runner escalates to kill() after a grace period."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


async def run_process(
    args: list[str],
    *,
    timeout: float = 3600.0,
    cancel_token: CancelToken | None = None,
    cwd: str | None = None,
    grace: float = 5.0,
) -> tuple[int, bytes, bytes]:
    """Run a process to completion. Returns (returncode, stdout, stderr).

    On timeout or cancellation the process gets `grace` seconds after
    terminate() before kill().
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    if cancel_token:
        cancel_token.register(proc)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout, stderr
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _terminate(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            await proc.wait()
        raise
    finally:
        if cancel_token:
            cancel_token.unregister(proc)


def tail_text(data: bytes, limit: int = 2000) -> str:
    """Decode the tail of a stderr blob for error reports."""
    text = data.decode("utf-8", errors="replace")
    return text[-limit:] if len(text) > limit else text
