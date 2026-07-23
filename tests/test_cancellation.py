"""CancelToken semantics and subprocess termination."""
from __future__ import annotations

import asyncio
import sys

import pytest

from bot.utils.subprocesses import CancelToken, run_process


async def test_token_raises_after_cancel():
    token = CancelToken()
    token.raise_if_cancelled()  # no-op before cancel
    token.cancel()
    assert token.cancelled
    with pytest.raises(asyncio.CancelledError):
        token.raise_if_cancelled()


async def test_run_process_success():
    py = sys.executable
    code, out, _ = await run_process([py, "-c", "print('hi')"], timeout=30)
    assert code == 0 and b"hi" in out


async def test_run_process_timeout_kills():
    py = sys.executable
    with pytest.raises(asyncio.TimeoutError):
        await run_process([py, "-c", "import time; time.sleep(30)"],
                          timeout=1.0, grace=1.0)


async def test_cancel_terminates_registered_process():
    py = sys.executable
    token = CancelToken()

    async def _cancel_soon():
        await asyncio.sleep(0.3)
        token.cancel()

    canceller = asyncio.create_task(_cancel_soon())
    # The token terminates the process; communicate() then returns quickly
    # with a non-zero exit code on any platform.
    code, _, _ = await run_process(
        [py, "-c", "import time; time.sleep(30)"],
        timeout=25.0, cancel_token=token, grace=2.0,
    )
    await canceller
    assert code != 0


async def test_late_registration_still_terminated():
    """Registering a process AFTER cancel() must terminate it immediately."""
    py = sys.executable
    token = CancelToken()
    token.cancel()
    proc = await asyncio.create_subprocess_exec(
        py, "-c", "import time; time.sleep(30)",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    token.register(proc)
    await asyncio.wait_for(proc.wait(), timeout=5)
    assert proc.returncode is not None
