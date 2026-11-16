"""The console's use of Temporal: start a run, send a decision, read a run.

Reads use describe and Event History only. Nothing here sends a Query, because a Query needs
a worker and so would wake a Foundry sandbox just to look. The client is the plain
``temporalio`` client with JSON-shaped payloads; the worker's types are not imported.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from .config import Config
from .history import decode_payload, normalize_event, normalize_pending

WORKFLOW_TYPE = "DeepResearchWorkflow"
RUN_ID = re.compile(r"^research-[A-Za-z0-9-]{1,48}$")
# A run in one of these phases is waiting on a person or a timer and uses nothing.
WAITING_PHASES = {"awaiting_approval", "waiting_for_follow_up"}
RUN_LIMIT = timedelta(hours=2)  # an abandoned run closes by itself
LIVE_CACHE_SECONDS = 0.6


class RunNotFound(Exception):
    pass


class RunClosed(Exception):
    pass


@dataclass
class RunData:
    workflow_id: str
    run_id: str
    status: str
    start_time: float | None
    close_time: float | None
    memo: dict[str, Any]
    events: list[dict[str, Any]]
    pending_activities: list[dict[str, Any]]
    pending_workflow_task: dict[str, Any] | None
    loaded_at: float


def _epoch(moment: Any) -> float | None:
    return moment.timestamp() if moment is not None else None


class TemporalGateway:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: Client | None = None
        self._connect_lock = asyncio.Lock()
        self._closed: OrderedDict[str, RunData] = OrderedDict()
        self._live: dict[str, tuple[float, RunData]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _connected(self) -> Client:
        async with self._connect_lock:
            if self._client is None:
                self._client = await Client.connect(
                    self._config.temporal_address, namespace=self._config.temporal_namespace
                )
            return self._client

    async def start(self, question: str, follow_up_after_seconds: int) -> str:
        client = await self._connected()
        workflow_id = f"research-{secrets.token_hex(4)}"
        await client.start_workflow(
            WORKFLOW_TYPE,
            {
                "question": question,
                "max_subquestions": 4,
                "follow_up_after_seconds": follow_up_after_seconds,
                "auto_approve": False,
            },
            id=workflow_id,
            task_queue=self._config.task_queue,
            # The workflow upserts the same key in its first task; this shows the question
            # in the run list while Foundry is still waking.
            memo={"question": question},
            execution_timeout=RUN_LIMIT,
        )
        return workflow_id

    async def decide(self, workflow_id: str, decision: dict[str, Any]) -> None:
        data = await self.load(workflow_id)
        if data.status != "RUNNING":
            raise RunClosed(workflow_id)
        client = await self._connected()
        try:
            await client.get_workflow_handle(workflow_id).signal("decide", decision)
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                raise RunClosed(workflow_id) from error
            raise
        self._live.pop(workflow_id, None)

    async def load(self, workflow_id: str) -> RunData:
        """Everything known about a run, from describe and history."""
        if not RUN_ID.match(workflow_id):
            raise RunNotFound(workflow_id)
        closed = self._closed.get(workflow_id)
        if closed is not None:
            return closed
        lock = self._locks.setdefault(workflow_id, asyncio.Lock())
        async with lock:  # several browsers on one run share a read
            cached = self._live.get(workflow_id)
            if cached is not None and time.monotonic() - cached[0] < LIVE_CACHE_SECONDS:
                return cached[1]
            data = await self._read(workflow_id)
            if data.status == "RUNNING":
                self._live[workflow_id] = (time.monotonic(), data)
            else:  # a closed run never changes
                self._live.pop(workflow_id, None)
                self._locks.pop(workflow_id, None)
                self._closed[workflow_id] = data
                while len(self._closed) > 200:
                    self._closed.popitem(last=False)
            return data

    async def _read(self, workflow_id: str) -> RunData:
        client = await self._connected()
        handle = client.get_workflow_handle(workflow_id)
        try:
            description = await handle.describe()
            if description.workflow_type != WORKFLOW_TYPE:
                raise RunNotFound(workflow_id)
            # Pin the history read to the run that was described.
            run_handle = client.get_workflow_handle(workflow_id, run_id=description.run_id)
            events = [normalize_event(event) async for event in run_handle.fetch_history_events()]
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                raise RunNotFound(workflow_id) from error
            raise
        raw = description.raw_description
        pending_activities, pending_workflow_task = normalize_pending(raw)
        return RunData(
            workflow_id=workflow_id,
            run_id=description.run_id or "",
            status=description.status.name if description.status is not None else "UNKNOWN",
            start_time=_epoch(description.start_time),
            close_time=_epoch(description.close_time),
            memo={key: decode_payload(value) for key, value in raw.workflow_execution_info.memo.fields.items()},
            events=events,
            pending_activities=pending_activities,
            pending_workflow_task=pending_workflow_task,
            loaded_at=time.time(),
        )

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        client = await self._connected()
        runs = []
        # Only this console's task queue: another booth station has its own console.
        query = f'WorkflowType="{WORKFLOW_TYPE}" AND TaskQueue="{self._config.task_queue}"'
        async for item in client.list_workflows(query, limit=max(limit * 3, 60)):
            if not RUN_ID.match(item.id):
                continue
            memo = {key: decode_payload(value) for key, value in item.raw_info.memo.fields.items()}
            runs.append(
                {
                    "id": item.id,
                    "status": item.status.name if item.status is not None else "UNKNOWN",
                    "phase": memo.get("phase") if isinstance(memo.get("phase"), str) else "starting",
                    "question": memo.get("question") if isinstance(memo.get("question"), str) else "",
                    "started_at": _epoch(item.start_time),
                    "closed_at": _epoch(item.close_time),
                }
            )
        runs.sort(key=lambda run: run["started_at"] or 0, reverse=True)
        return runs[:limit]

    async def busy_runs(self) -> int:
        """Runs that are using a sandbox or a model right now (not the ones that are waiting)."""
        client = await self._connected()
        query = (
            f'WorkflowType="{WORKFLOW_TYPE}" AND TaskQueue="{self._config.task_queue}" AND ExecutionStatus="Running"'
        )
        count = 0
        async for item in client.list_workflows(query, limit=200):
            phase = decode_payload(item.raw_info.memo.fields.get("phase"))
            if phase not in WAITING_PHASES:
                count += 1
        return count
