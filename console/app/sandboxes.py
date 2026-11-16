"""What each Foundry sandbox says about itself, and releasing the ones that went quiet.

Foundry's session status lags reality by minutes, so the worker in each sandbox reports its
own lifecycle here (see docs/contract.md). When a worker says it has gone quiet, the console
stops that session: it runs as the identity that created the session (the controller's), and
the sandbox cannot stop itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .history import iso

logger = logging.getLogger(__name__)

EVENTS = {"boot", "polling", "alive", "keepalive", "idle_exit", "stopping", "worker_failed"}
UP_STATES = {"starting", "working", "idle", "releasing"}
GONE_AFTER_SECONDS = 45.0  # the worker reports every 15 s; three missed reports is silence
KEEP_SECONDS = 3600.0
_SESSION = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_AGENT = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class SandboxActions(Protocol):
    """Calls to Foundry on a session; each returns (HTTP status, short response text)."""

    async def stop(self, agent: str, session: str) -> tuple[int, str]: ...

    async def crash_worker(self, agent: str, session: str) -> tuple[int, str]: ...


def _inflight(detail: Any) -> int:
    try:
        return max(0, int((detail or {}).get("inflight", 0) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


@dataclass
class Sandbox:
    session: str
    agent: str = ""
    agent_version: str = ""
    build_id: str = ""
    first_seen: float = 0.0
    polling_since: float | None = None
    last_seen: float = 0.0
    last_event: str = ""
    inflight: int = 0
    keepalives: int = 0
    release_requested_at: float | None = None
    stop_reason: str | None = None  # "idle": the worker went quiet; "presenter": someone asked
    stop: dict[str, Any] | None = None
    stopping_at: float | None = None  # the process reported SIGTERM
    worker_failed: bool = False
    crashes: list[dict[str, Any]] = field(default_factory=list)  # presenter-requested worker kills

    def state(self, now: float) -> str:
        if self.stop is not None and self.stop.get("ok"):
            return "released"
        if self.stopping_at is not None:
            return "released" if self.release_requested_at is not None else "gone"
        if now - self.last_seen > GONE_AFTER_SECONDS:
            return "gone"
        if self.release_requested_at is not None:
            return "releasing"
        if self.polling_since is None:
            return "starting"
        return "working" if self.inflight > 0 else "idle"

    def ended_at(self, now: float) -> float | None:
        state = self.state(now)
        if state == "released":
            return (self.stop or {}).get("at") or self.stopping_at or self.last_seen
        if state == "gone":
            return self.stopping_at or self.last_seen
        return None

    def public(self, now: float) -> dict[str, Any]:
        ended = self.ended_at(now)
        stop = None
        if self.stop is not None:
            stop = {key: self.stop.get(key) for key in ("ok", "status_code", "seconds", "error", "recorded_only")}
        return {
            "session": self.session,
            "short": self.session[:12],
            "agent": self.agent,
            "agent_version": self.agent_version,
            "build_id": self.build_id,
            "state": self.state(now),
            "first_seen": iso(self.first_seen),
            "polling_since": iso(self.polling_since),
            "last_seen": iso(self.last_seen),
            "last_event": self.last_event,
            "inflight": self.inflight,
            "keepalives": self.keepalives,
            "release_requested_at": iso(self.release_requested_at),
            "stop_reason": self.stop_reason,
            "stop": stop,
            "ended_at": iso(ended),
            "lived_seconds": round(max(0.0, (ended if ended is not None else now) - self.first_seen), 1),
            "worker_failed": self.worker_failed,
            "crashes": [
                {key: crash.get(key) for key in ("ok", "status_code", "seconds", "error", "recorded_only")}
                | {"at": iso(crash.get("at"))}
                for crash in self.crashes
            ],
        }


class SandboxRegistry:
    def __init__(
        self,
        path: Path | None = None,
        actions: SandboxActions | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._actions = actions
        self._clock = clock
        self._sandboxes: dict[str, Sandbox] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping: set[str] = set()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._replay(path)

    # -- intake ---------------------------------------------------------------------------

    @staticmethod
    def validate(body: Any) -> str | None:
        """Why an event body cannot be accepted, or None."""
        if not isinstance(body, dict):
            return "the body must be a JSON object"
        if body.get("event") not in EVENTS:
            return "unknown event"
        if not isinstance(body.get("session"), str) or not _SESSION.match(body["session"]):
            return "session must be a session ID"
        agent = body.get("agent", "")
        if not isinstance(agent, str) or (agent and not _AGENT.match(agent)):
            return "agent must be an agent name"
        if not isinstance(body.get("detail", {}), dict):
            return "detail must be an object"
        return None

    async def record(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply one sandbox event. A quiet worker's sandbox is released in the background."""
        now = self._clock()
        record = {
            "session": body["session"],
            "agent": str(body.get("agent", ""))[:80],
            "agent_version": str(body.get("agent_version", ""))[:20],
            "build_id": str(body.get("build_id", ""))[:40],
            "event": body["event"],
            "detail": {"inflight": _inflight(body.get("detail"))},
            "received_at": now,
        }
        sandbox = self._apply(record)
        if record["event"] != "alive":  # alive is frequent and worthless after a restart
            self._append(record)
        if record["event"] == "idle_exit" and sandbox.stop is None:
            self._spawn(self._stop(sandbox, "idle"))
        return sandbox.public(now)

    def _apply(self, record: dict[str, Any]) -> Sandbox:
        now = record["received_at"]
        session, event = record["session"], record["event"]
        sandbox = self._sandboxes.get(session)
        if sandbox is None or (event == "boot" and sandbox.state(now) in ("released", "gone")):
            # A boot for a session we had finished with: Foundry started it again.
            sandbox = self._sandboxes[session] = Sandbox(session=session, first_seen=now)
        for key in ("agent", "agent_version", "build_id"):
            if record.get(key):
                setattr(sandbox, key, record[key])
        sandbox.last_seen = now
        sandbox.last_event = event
        inflight = record.get("detail", {}).get("inflight", 0)
        if event == "polling":
            sandbox.polling_since = sandbox.polling_since or now
        elif event == "alive":
            sandbox.polling_since = sandbox.polling_since or now
            sandbox.inflight = inflight
        elif event == "keepalive":
            sandbox.keepalives += 1
            sandbox.inflight = inflight
        elif event == "idle_exit":
            sandbox.inflight = 0
            sandbox.release_requested_at = sandbox.release_requested_at or now
            sandbox.stop_reason = sandbox.stop_reason or "idle"
        elif event == "stopping":
            sandbox.stopping_at = now
        elif event == "worker_failed":
            sandbox.worker_failed = True
        elif event == "_stop_result":
            sandbox.stop = record.get("stop")
            sandbox.stop_reason = record.get("stop_reason") or sandbox.stop_reason
            sandbox.release_requested_at = sandbox.release_requested_at or record.get("requested_at")
        elif event == "_crash_result":
            sandbox.crashes.append(record.get("crash") or {})
        return sandbox

    # -- stopping -------------------------------------------------------------------------

    def request_stop(self, session: str, reason: str = "presenter") -> dict[str, Any] | None:
        """Stop a sandbox now, in the background: the Foundry call takes seconds to return.

        ``session`` may be the full ID or a unique prefix. Returns the sandbox as it is at
        this moment (``releasing``), or None if there is no such sandbox.
        """
        sandbox = self._find(session)
        if sandbox is None:
            return None
        if sandbox.stop is None or not sandbox.stop.get("ok"):
            sandbox.release_requested_at = sandbox.release_requested_at or self._clock()
            sandbox.stop_reason = reason
            self._spawn(self._stop(sandbox, reason))
        return sandbox.public(self._clock())

    def request_crash(self, session: str) -> dict[str, Any] | None:
        """Kill the worker process inside a sandbox, in the background.

        The sandbox survives and starts a new worker; Temporal retries whatever the worker
        was doing. This is the presenter's "lose the worker mid-task" button.
        """
        sandbox = self._find(session)
        if sandbox is None:
            return None
        self._spawn(self._crash(sandbox))
        return sandbox.public(self._clock())

    async def _crash(self, sandbox: Sandbox) -> None:
        started = time.monotonic()
        result: dict[str, Any]
        if self._actions is None:
            result = {"ok": True, "recorded_only": True}
        elif not _AGENT.match(sandbox.agent or ""):
            result = {"ok": False, "error": "the sandbox never reported its agent name"}
        else:
            try:
                status_code, text = await self._actions.crash_worker(sandbox.agent, sandbox.session)
                result = {"ok": 200 <= status_code < 300, "status_code": status_code}
                if not result["ok"]:
                    result["error"] = text[:200]
            except Exception as error:  # noqa: BLE001  whatever it is, the page shows the reason
                result = {"ok": False, "error": f"{type(error).__name__}: {error}"[:200]}
        result["seconds"] = round(time.monotonic() - started, 2)
        result["at"] = self._clock()
        sandbox.crashes.append(result)
        logger.info(
            "crash-worker session=%s ok=%s status=%s seconds=%s",
            sandbox.session[:12],
            result.get("ok"),
            result.get("status_code"),
            result["seconds"],
        )
        self._append(
            {"session": sandbox.session, "event": "_crash_result", "crash": result, "received_at": result["at"]}
        )

    async def _stop(self, sandbox: Sandbox, reason: str) -> None:
        if sandbox.session in self._stopping:
            return  # one stop at a time per sandbox
        self._stopping.add(sandbox.session)
        try:
            await self._stop_once(sandbox, reason)
        finally:
            self._stopping.discard(sandbox.session)

    async def _stop_once(self, sandbox: Sandbox, reason: str) -> None:
        requested = self._clock()
        sandbox.release_requested_at = sandbox.release_requested_at or requested
        sandbox.stop_reason = reason if sandbox.stop_reason in (None, "idle") else sandbox.stop_reason
        started = time.monotonic()
        result: dict[str, Any]
        if self._actions is None:
            # No Foundry to call (a local run): the request is all there is to record.
            result = {"ok": True, "recorded_only": True}
        elif not _AGENT.match(sandbox.agent or ""):
            result = {"ok": False, "error": "the sandbox never reported its agent name"}
        else:
            try:
                status_code, text = await self._actions.stop(sandbox.agent, sandbox.session)
                result = {"ok": 200 <= status_code < 300, "status_code": status_code}
                if not result["ok"]:
                    result["error"] = text[:200]
            except Exception as error:  # noqa: BLE001  the page shows why a release did not happen
                result = {"ok": False, "error": f"{type(error).__name__}: {error}"[:200]}
        result["seconds"] = round(time.monotonic() - started, 2)
        result["at"] = self._clock()
        sandbox.stop = result
        logger.info(
            "stop session=%s reason=%s ok=%s status=%s seconds=%s",
            sandbox.session[:12],
            reason,
            result.get("ok"),
            result.get("status_code"),
            result["seconds"],
        )
        self._append(
            {
                "session": sandbox.session,
                "event": "_stop_result",
                "stop": result,
                "stop_reason": sandbox.stop_reason,
                "requested_at": requested,
                "received_at": result["at"],
            }
        )

    def _find(self, session: str) -> Sandbox | None:
        if session in self._sandboxes:
            return self._sandboxes[session]
        if not _SESSION.match(session):
            return None
        matches = [box for key, box in self._sandboxes.items() if key.startswith(session)]
        return matches[0] if len(matches) == 1 else None

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for stops in flight (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- reads ----------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        recent = [
            box
            for box in self._sandboxes.values()
            if box.state(now) in UP_STATES or now - box.last_seen <= KEEP_SECONDS
        ]
        recent.sort(key=lambda box: box.first_seen, reverse=True)
        return {
            "up": sum(1 for box in recent if box.state(now) in UP_STATES),
            "sandboxes": [box.public(now) for box in recent],
            "server_time": iso(now),
        }

    def boot_times(self) -> dict[str, float]:
        """When each sandbox was first heard from, keyed the way worker identities name it."""
        return {box.session[:12]: box.first_seen for box in self._sandboxes.values()}

    # -- persistence ----------------------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> None:
        if self._path is None:
            return
        try:
            with self._path.open("a") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            logger.warning("could not write the sandbox log")

    def _replay(self, path: Path) -> None:
        if not path.exists():
            return
        horizon = self._clock() - 2 * KEEP_SECONDS
        kept: list[str] = []
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
                if record.get("received_at", 0) < horizon:
                    continue
                self._apply(record)
                kept.append(line)
            except (ValueError, KeyError, TypeError):
                continue
        try:  # drop what is too old to matter, so the file stays small across booth days
            path.write_text("".join(f"{line}\n" for line in kept))
        except OSError:
            pass
