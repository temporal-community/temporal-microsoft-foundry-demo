"""The supervisor reaps its child, restarts one that dies, and hands back a quiet sandbox."""

from __future__ import annotations

import asyncio
import sys

from tfd_worker.settings import Settings
from tfd_worker.supervisor import WorkerSupervisor

SETTINGS = Settings.from_env(
    {"TEMPORAL_DEPLOYMENT_NAME": "tfd", "TEMPORAL_BUILD_ID": "test", "TFD_SHUTDOWN_DRAIN_SECONDS": "1"}
)
SLEEPER = (sys.executable, "-c", "import time; time.sleep(60)")
QUIET = (sys.executable, "-c", "pass")


async def wait_for(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_crashed_worker_is_restarted_in_the_same_sandbox() -> None:
    supervisor = WorkerSupervisor(SETTINGS, command=SLEEPER, restart_backoff=0.1)
    assert supervisor.start() == "started"
    await wait_for(lambda: supervisor.status()["child_pid"] is not None)
    first = supervisor.status()["child_pid"]
    assert supervisor.crash_worker() == first
    await wait_for(lambda: supervisor.status()["child_pid"] not in (None, first))
    assert supervisor.status()["starts"] == 2
    await supervisor.close()
    assert supervisor.status()["child_pid"] is None


async def test_quiet_worker_releases_the_sandbox_once() -> None:
    released: list[bool] = []

    async def release() -> None:
        released.append(True)

    supervisor = WorkerSupervisor(SETTINGS, command=QUIET, on_idle_exit=release)
    assert supervisor.start() == "started"
    await wait_for(lambda: supervisor.status()["state"] == "idle_exit")
    assert released == [True]
    assert supervisor.start() == "released"  # a handed-back sandbox does not start again


async def test_worker_that_keeps_dying_reports_failure() -> None:
    failed: list[bool] = []

    async def on_failed() -> None:
        failed.append(True)

    dying = (sys.executable, "-c", "raise SystemExit(3)")
    supervisor = WorkerSupervisor(
        SETTINGS, command=dying, on_failed=on_failed, max_restarts=2, restart_backoff=0.05
    )
    supervisor.start()
    await wait_for(lambda: supervisor.status()["state"] == "failed")
    assert failed == [True]
    assert supervisor.status()["starts"] == 3


async def test_second_start_does_not_spawn_a_second_worker() -> None:
    supervisor = WorkerSupervisor(SETTINGS, command=SLEEPER)
    assert supervisor.start() == "started"
    assert supervisor.start() == "running"
    await supervisor.close()
    assert supervisor.start() == "shutting_down"
