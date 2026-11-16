"""Runs one Temporal worker child process per sandbox, independent of HTTP requests.

Adapted from the example worker for the Foundry provider (02strich/temporal-example-workers, cmd/foundry-worker):
the invocation handler only reserves a worker, a child process does the polling, and the child
is always reaped. Differences: the lifetime comes from the session's idle timeout instead of a
fixed 300 s, a child that dies while starting is retried, and a child that stopped because it
had nothing to do can release the sandbox.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from .settings import Settings

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
KILL_GRACE_SECONDS = 5.0


def event(name: str, **fields: Any) -> None:
    """One greppable line per lifecycle event.

    With ``TFD_EVENT_LOG`` set the line is also appended to that file. A session's home
    directory outlives its sandbox, so the file can be read back after the sandbox is gone;
    the log stream cannot.
    """
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    now = time.time()
    logger.info("TFD_EVENT %s t=%.3f %s", name, now, details)
    path = os.environ.get("TFD_EVENT_LOG")
    if path:
        try:
            with open(os.path.expanduser(path), "a") as handle:
                handle.write(f"{now:.3f} pid={os.getpid()} {name} {details}\n")
        except OSError:
            pass


class WorkerSupervisor:
    def __init__(
        self,
        settings: Settings,
        *,
        command: Sequence[str] | None = None,
        on_idle_exit: Callable[[], Awaitable[None]] | None = None,
        on_failed: Callable[[], Awaitable[None]] | None = None,
        max_restarts: int = 3,
        restart_backoff: float = 2.0,
    ) -> None:
        self._settings = settings
        self._command = (
            tuple(command)
            if command is not None
            else (sys.executable, "-u", "-m", "tfd_worker.worker_main")
        )
        self._on_idle_exit = on_idle_exit
        self._on_failed = on_failed
        self._process: asyncio.subprocess.Process | None = None
        self._max_restarts = max_restarts
        self._restart_backoff = restart_backoff
        self._shutdown = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state = "not_started"
        self._child_pid: int | None = None
        self._starts = 0
        self._started_at: float | None = None

    def start(self) -> str:
        """Reserve a worker without waiting for it to connect.

        Called only on the server's event loop. Nothing is awaited between the check and the
        assignment, so two simultaneous requests cannot start two workers.
        """
        if self._shutdown.is_set():
            return "shutting_down"
        if self._state == "idle_exit":
            # The worker went quiet and this sandbox was handed back; a new task gets a new one.
            return "released"
        if self._task is not None and not self._task.done():
            return "running"
        loop = asyncio.get_running_loop()
        self._started_at = time.time()
        # No deadline here: the worker stops itself (it knows whether it is busy and when it
        # last kept the session alive). The supervisor reaps it and handles shutdown.
        self._task = loop.create_task(self._supervise(None))
        return "started"

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            event("shutdown_requested", state=self._state)
        self._shutdown.set()

    async def close(self) -> None:
        self.request_shutdown()
        if self._task is not None:
            # Do not cancel process creation: a spawned child must be reaped.
            await asyncio.shield(self._task)

    def _child_env(self) -> dict[str, str]:
        """A worker restarted after a crash stays longer before it calls itself quiet.

        The work it was restarted for comes back only after Temporal has timed out the
        interrupted attempt (about 10 s), which is close to the normal quiet period.
        """
        env = dict(os.environ)
        if self._starts > 0 and self._settings.idle_exit_seconds > 0:
            env["TFD_IDLE_EXIT_SECONDS"] = str(max(self._settings.idle_exit_seconds, 45))
        return env

    def crash_worker(self) -> int | None:
        """Kill the worker process without warning (a staged failure). Returns its pid.

        The sandbox stays up, so the kernel closes the worker's connections and Temporal sees
        its polls end at once. The supervisor then starts a new worker in the same sandbox.
        """
        process = self._process
        if process is None or process.returncode is not None:
            return None
        event("worker_crash_requested", pid=process.pid)
        process.kill()
        return process.pid

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "child_pid": self._child_pid,
            "starts": self._starts,
            "started_at": self._started_at,
            "budget_seconds": self._settings.worker_budget_seconds,
        }

    async def _supervise(self, deadline: float | None) -> None:
        loop = asyncio.get_running_loop()
        restarts = 0
        try:
            while not self._shutdown.is_set():
                outcome, returncode = await self._run_child(deadline)
                if outcome != "exited":
                    self._state = f"stopped_on_{outcome}"
                    return
                if returncode == 0:
                    # The child stopped by itself: it had nothing to do.
                    self._state = "idle_exit"
                    event("worker_idle_exit")
                    if self._on_idle_exit is not None and not self._shutdown.is_set():
                        await self._on_idle_exit()
                    return
                out_of_time = (
                    deadline is not None and loop.time() + self._restart_backoff >= deadline
                )
                if restarts >= self._max_restarts or out_of_time:
                    self._state = "failed"
                    event("worker_failed", returncode=returncode, restarts=restarts)
                    if self._on_failed is not None:
                        await self._on_failed()
                    return
                restarts += 1
                event("worker_restart", returncode=returncode, attempt=restarts)
                await asyncio.sleep(self._restart_backoff)
        except Exception:
            self._state = "failed"
            logger.exception("Worker supervision failed")

    async def _run_child(self, deadline: float | None) -> tuple[str, int | None]:
        """Run one child until it exits, the deadline passes, or shutdown is requested."""
        loop = asyncio.get_running_loop()
        process = None
        wait_task: asyncio.Task[int] | None = None
        stop_task: asyncio.Task[bool] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command, cwd=str(APP_DIR), env=self._child_env()
            )
            self._process = process
            self._starts += 1
            self._child_pid = process.pid
            self._state = "running"
            event("worker_spawned", pid=process.pid, start=self._starts)
            wait_task = asyncio.create_task(process.wait())
            stop_task = asyncio.create_task(self._shutdown.wait())
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            done, _ = await asyncio.wait(
                (wait_task, stop_task), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if wait_task in done:
                event("worker_exited", pid=process.pid, returncode=process.returncode)
                return "exited", process.returncode

            reason = "shutdown" if stop_task in done else "deadline"
            drain = (
                self._settings.shutdown_drain_seconds
                if reason == "shutdown"
                else self._settings.drain_seconds
            )
            event("worker_stopping", pid=process.pid, reason=reason, drain=drain)
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=drain + KILL_GRACE_SECONDS)
            except TimeoutError:
                event("worker_drain_timeout", pid=process.pid)
            return reason, process.returncode
        finally:
            # Also clean up on unexpected errors or cancellation.
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
            pending = [task for task in (wait_task, stop_task) if task is not None]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._child_pid = None
            self._process = None
