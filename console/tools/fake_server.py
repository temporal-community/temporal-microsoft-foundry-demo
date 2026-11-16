"""The console with a scripted run behind it, for working on the page without Temporal.

    .venv/bin/python tools/fake_server.py [port]          # default 8090, access code "booth"

Open http://127.0.0.1:8090/#/run/research-<stage> where <stage> is one of: starting, planning,
approval, waking, researching, unreported, retry, refused, writing, waiting, followup, done,
failed, rejecting, rejected.
Every stage is the same run frozen at a different moment, with realistic text from
tests/fixtures/realistic_run.json, real page code and real API code. Only Temporal and the
sandboxes are scripted.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.gateway import RunData, RunNotFound
from app.history import iso
from app.main import create_app
from app.sandboxes import Sandbox, SandboxRegistry

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/realistic_run.json").read_text())
MODEL = "microsoft_agent_framework.invoke_model"
SESSIONS = [
    "0ca65fab16ecf6ee008GeujLesj0vCxYIr82r8Mf",
    "04545a0bb534c818007RkqPzWm3TfYv1NdAe5Lhs",
    "0e9efb32f867e5c9002XbGtQpLk8JsHn6VuCy4Dw",
    "03912e4a1014c843005ZmEwRtYu7IoPa9SdFg2Hj",
]
AGENT = "tfd-worker-28bbff8f7a"
# Where each stage freezes the run, in seconds after it started.
STAGES = {
    "starting": 2.4,
    "planning": 8.0,
    "approval": 60.0,
    "waking": 97.6,
    "researching": 116.0,
    # As on Foundry early in the research: the worker has not yet reported that it is busy.
    "unreported": 116.0,
    "retry": 118.0,
    "refused": 116.0,
    "writing": 126.0,
    "waiting": 160.0,
    "followup": 201.0,
    "done": 240.0,
    "failed": 124.0,
    "rejecting": 98.2,
    "rejected": 131.0,
}


def identity(index: int) -> str:
    return f"foundry/{AGENT}/v1/{SESSIONS[index][:12]}/pid13{index}"


class Script:
    """One run's history up to a moment, built from offsets so the live counters move."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.offset = STAGES[stage]
        self.t0 = time.time() - self.offset
        self.events: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.pending_task: dict[str, Any] | None = None
        self.memo: dict[str, Any] = {}
        self.status = "RUNNING"
        self.sandboxes: list[Sandbox] = []
        self._build()

    # -- helpers --------------------------------------------------------------------------

    def reached(self, at: float) -> bool:
        return at <= self.offset

    def add(self, kind: str, at: float, **fields: Any) -> int:
        self.events.append({"id": len(self.events) + 1, "type": kind, "time": self.t0 + at, **fields})
        return len(self.events)

    def task(self, scheduled: float, started: float, box: int) -> bool:
        """A workflow task; False (and a pending task) if it has not started by now."""
        scheduled_id = self.add("workflow_task_scheduled", scheduled)
        if not self.reached(started):
            self.pending_task = {"state": "scheduled", "scheduled_time": self.t0 + scheduled, "attempt": 1}
            return False
        self.add("workflow_task_started", started, scheduled_event_id=scheduled_id, identity=identity(box))
        self.add("workflow_task_completed", started + 0.05)
        return True

    def agent(self, name: str, scheduled: float, started: float, completed: float, box: int, attempt: int = 1) -> bool:
        """A model call; False while it is still running (it is then a pending Activity)."""
        activity_id = str(len(self.events) + 1)
        scheduled_id = self.add(
            "activity_task_scheduled", scheduled, activity_id=activity_id, activity_type=MODEL, summary=name
        )
        if not self.reached(started):
            self.pending.append(
                {
                    "activity_id": activity_id,
                    "state": "scheduled",
                    "attempt": attempt,
                    "last_started_time": None,
                    "identity": "",
                }
            )
            return False
        if not self.reached(completed):
            self.pending.append(
                {
                    "activity_id": activity_id,
                    "state": "started",
                    "attempt": attempt,
                    "last_started_time": self.t0 + started,
                    "identity": identity(box),
                    "message": "",
                }
            )
            return False
        self.add(
            "activity_task_started", started, scheduled_event_id=scheduled_id, identity=identity(box), attempt=attempt
        )
        self.add("activity_task_completed", completed, scheduled_event_id=scheduled_id)
        return True

    def note(self, at: float, **memo: Any) -> None:
        self.memo.update(memo)
        self.add("workflow_properties_modified", at, memo={key: value for key, value in memo.items() if key == "phase"})

    def sandbox(self, index: int, booted: float, polling: float, released: float | None, inflight: int = 0) -> None:
        if not self.reached(booted):
            return
        box = Sandbox(
            session=SESSIONS[index], agent=AGENT, agent_version="1", build_id="28bbff8f7a", first_seen=self.t0 + booted
        )
        box.last_seen = time.time() - 2
        box.last_event = "alive"
        if self.reached(polling):
            box.polling_since = self.t0 + polling
            box.inflight = inflight
        if released is not None and self.reached(released):
            box.release_requested_at = self.t0 + released
            box.stop_reason = "idle"
            box.last_seen = self.t0 + released
            if self.reached(released + 5.2):
                box.stop = {"ok": True, "status_code": 204, "seconds": 5.2, "at": self.t0 + released + 5.2}
        if self.stage == "refused" and index == 1:
            # Foundry refused both presenter requests: the card has to say so.
            box.crashes.append({"ok": False, "status_code": 403, "error": "Forbidden", "at": self.t0 + 110.0})
            box.release_requested_at = self.t0 + 112.0
            box.stop_reason = "presenter"
            box.stop = {"ok": False, "status_code": 403, "error": "Forbidden", "at": self.t0 + 112.4}
        self.sandboxes.append(box)

    # -- the run --------------------------------------------------------------------------

    def _build(self) -> None:
        stage = self.stage
        self.add(
            "workflow_execution_started", 0.0, input={"question": FIXTURE["question"], "follow_up_after_seconds": 60}
        )
        self.memo["question"] = FIXTURE["question"]
        self.sandbox(0, 3.9, 4.8, 28.0, inflight=1 if self.offset < 13 else 0)
        if not self.task(0.0, 4.8, 0):
            return
        self.note(4.85, phase="planning")
        if not self.agent("planner", 4.9, 5.0, 12.9, 0):
            return
        self.task(12.9, 12.95, 0)
        self.note(13.0, phase="awaiting_approval", plan=FIXTURE["plan"])
        if not self.reached(95.0):
            return

        approved = stage not in ("rejecting", "rejected")
        self.add(
            "workflow_execution_signaled",
            95.0,
            name="decide",
            input={"approved": approved, "note": "Keep it practical."},
        )
        if approved:
            busy = 3 if self.offset < 122 else (1 if self.offset < 132 else 0)
            self.sandbox(1, 99.2, 100.1, 147.0, inflight=0 if stage == "unreported" else busy)
        else:
            self.sandbox(1, 99.2, 100.1, 116.0)
        if not self.task(95.0, 100.1, 1):
            return
        self.memo["decision"] = {"approved": approved, "note": "Keep it practical."}
        if not approved:
            self.note(100.2, phase="rejected")
            self.add("workflow_execution_completed", 100.25)
            self.status = "COMPLETED"
            return

        research = [dict(item, status="running", findings="") for item in FIXTURE["research"]]
        self.note(100.2, phase="researching", research=research)
        done_at = [114.2, 118.9, 121.5, 119.7]
        all_done = True
        for index, completed in enumerate(done_at):
            if stage == "retry" and index == 1:
                # The worker died under researcher 2; the retry is running on a new sandbox.
                self.sandbox(3, 112.0, 112.9, None, inflight=1)
                finished = self.agent("researcher 2", 100.25, 113.0, 999.0, 3, attempt=2)
            elif stage == "failed" and index == 3:
                scheduled_id = self.add(
                    "activity_task_scheduled",
                    100.25,
                    activity_id=f"r{index}",
                    activity_type=MODEL,
                    summary="researcher 4",
                )
                self.add(
                    "activity_task_started", 118.0, scheduled_event_id=scheduled_id, identity=identity(1), attempt=6
                )
                self.add(
                    "activity_task_failed",
                    119.0,
                    scheduled_event_id=scheduled_id,
                    message="RateLimitError: the model deployment is over its quota",
                )
                research[index].update(status="failed", error="RateLimitError: the model deployment is over its quota")
                finished = True
            else:
                finished = self.agent(f"researcher {index + 1}", 100.25, 100.3, completed, 1)
                if finished:
                    research[index] = dict(FIXTURE["research"][index])
            all_done = all_done and finished
        self.memo["research"] = research
        if not all_done:
            return
        if stage == "failed":
            self.task(121.5, 121.55, 1)
            self.note(121.6, phase="writing")
            scheduled_id = self.add(
                "activity_task_scheduled", 121.6, activity_id="w", activity_type=MODEL, summary="writer"
            )
            self.add("activity_task_started", 122.5, scheduled_event_id=scheduled_id, identity=identity(1), attempt=6)
            self.add(
                "activity_task_failed",
                123.0,
                scheduled_event_id=scheduled_id,
                message="RateLimitError: the model deployment is over its quota",
            )
            self.add(
                "workflow_execution_failed", 123.2, message="RateLimitError: the model deployment is over its quota"
            )
            self.status = "FAILED"
            return

        self.task(121.5, 121.55, 1)
        self.note(121.6, phase="writing")
        if not self.agent("writer", 121.6, 121.7, 131.8, 1):
            return
        self.task(131.8, 131.85, 1)
        self.memo["report"] = FIXTURE["report"]
        self.memo["follow_up_due"] = iso(self.t0 + 191.9)
        self.note(131.9, phase="waiting_for_follow_up")
        self.add("timer_started", 131.9, timer_id="1", seconds=60.0, summary="Wait before the follow-up")
        if not self.reached(191.9):
            return

        self.add("timer_fired", 191.9, timer_id="1")
        self.sandbox(2, 196.7, 197.6, 222.0, inflight=1 if self.offset < 207 else 0)
        if not self.task(191.9, 197.6, 2):
            return
        self.note(197.65, phase="writing_follow_up")
        if not self.agent("follow-up", 197.7, 197.8, 206.3, 2):
            return
        self.task(206.3, 206.35, 2)
        self.memo["follow_up"] = FIXTURE["follow_up"]
        self.note(206.4, phase="completed")
        self.add("workflow_execution_completed", 206.45)
        self.status = "COMPLETED"

    def data(self, workflow_id: str) -> RunData:
        closed = self.events[-1]["time"] if self.status != "RUNNING" else None
        return RunData(
            workflow_id=workflow_id,
            run_id="019a0b7e-5f6c-7c2e-9d4e-3b1f0a6c8e21",
            status=self.status,
            start_time=self.t0,
            close_time=closed,
            memo=self.memo,
            events=self.events,
            pending_activities=self.pending,
            pending_workflow_task=self.pending_task,
            loaded_at=time.time(),
        )


class ScriptedGateway:
    def __init__(self) -> None:
        self.current: Script | None = None

    async def load(self, workflow_id: str) -> RunData:
        stage = workflow_id.removeprefix("research-")
        if stage not in STAGES:
            raise RunNotFound(workflow_id)
        # Rebuilt on every request, so the run stays frozen at its stage however long the
        # page is left open.
        self.current = Script(stage)
        return self.current.data(workflow_id)

    async def start(self, question: str, follow_up_after_seconds: int) -> str:
        return "research-starting"

    async def decide(self, workflow_id: str, decision: dict[str, Any]) -> None:
        return None

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {
                "id": "research-done",
                "status": "COMPLETED",
                "phase": "completed",
                "question": FIXTURE["question"],
                "started_at": now - 900,
                "closed_at": now - 690,
            },
            {
                "id": "research-approval",
                "status": "RUNNING",
                "phase": "awaiting_approval",
                "question": "What is new in Microsoft Foundry Agent Service in 2026?",
                "started_at": now - 240,
                "closed_at": None,
            },
            {
                "id": "research-failed",
                "status": "FAILED",
                "phase": "writing",
                "question": "How do durable execution and AI agents fit together?",
                "started_at": now - 3000,
                "closed_at": now - 2870,
            },
        ]

    async def busy_runs(self) -> int:
        return 0


class ScriptedRegistry(SandboxRegistry):
    def __init__(self, gateway: ScriptedGateway) -> None:
        super().__init__()
        self._gateway = gateway

    def _boxes(self) -> list[Sandbox]:
        return self._gateway.current.sandboxes if self._gateway.current else []

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        boxes = sorted(self._boxes(), key=lambda box: box.first_seen, reverse=True)
        public = [box.public(now) for box in boxes]
        return {
            "up": sum(1 for box in public if box["state"] in ("starting", "working", "idle", "releasing")),
            "sandboxes": public,
            "server_time": iso(now),
        }

    def boot_times(self) -> dict[str, float]:
        return {box.session[:12]: box.first_seen for box in self._boxes()}

    def request_stop(self, session: str, reason: str = "presenter") -> dict[str, Any] | None:
        return next((box.public(time.time()) for box in self._boxes() if box.session.startswith(session)), None)

    request_crash = request_stop  # type: ignore[assignment]


def build() -> Any:
    config = Config.from_env(
        {
            "TFD_ACCESS_CODE": "booth",
            "TFD_INTERNAL_TOKEN": "local-token",
            "TFD_SESSION_SECRET": "dev",
            "TFD_COOKIE_SECURE": "0",
            "TFD_AGENT_NAME": AGENT,
            "TFD_MODEL_NAME": "gpt-5-mini",
            "TFD_REGION": "East US 2",
            "TFD_TEMPORAL_UI_URL": "/temporal-ui",
        }
    )
    gateway = ScriptedGateway()
    return create_app(config, gateway, ScriptedRegistry(gateway))


if __name__ == "__main__":
    uvicorn.run(build(), host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8090, log_level="warning")
