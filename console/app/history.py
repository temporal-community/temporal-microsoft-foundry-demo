"""Turn a run's Event History into what the page shows: a timeline, the wakes, the sandboxes.

Everything here reads history and the describe response. Neither needs a worker, so looking
at a run never wakes a Foundry sandbox. ``build_view`` is a pure function over plain dicts
(see ``normalize_event`` for their shape), which is what the tests feed it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

AGENT_ACTIVITY_TYPES = {
    "microsoft_agent_framework.invoke_model",
    "microsoft_agent_framework.invoke_model_streaming",
}
# A workflow task counts as picked up by an already-running sandbox, not woken, when the
# sandbox reported its boot this long before the event that made the task.
WARM_MARGIN_SECONDS = 1.0


def sandbox_of(identity: str) -> str:
    """The sandbox a worker identity names.

    In Foundry the identity is ``foundry/<agent>/v<version>/<session id prefix>/pid<n>``.
    Anything else (a worker on a laptop) is its own sandbox.
    """
    parts = identity.split("/")
    if len(parts) >= 5 and parts[0] == "foundry":
        return parts[3]
    return identity


def agent_of(identity: str) -> str:
    parts = identity.split("/")
    return parts[1] if len(parts) >= 5 and parts[0] == "foundry" else ""


def iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------
# Protobuf to plain dicts
# ---------------------------------------------------------------------------------------


def _ts(timestamp: Any) -> float | None:
    if timestamp is None or (timestamp.seconds == 0 and timestamp.nanos == 0):
        return None
    return timestamp.seconds + timestamp.nanos / 1e9


def decode_payload(payload: Any) -> Any:
    """A JSON payload's value, or None for anything else (binary, encrypted, empty)."""
    try:
        if payload is None or payload.metadata.get("encoding") != b"json/plain":
            return None
        return json.loads(payload.data)
    except (ValueError, AttributeError):
        return None


def _first_payload(payloads: Any) -> Any:
    items = getattr(payloads, "payloads", None)
    return decode_payload(items[0]) if items else None


def _failure_message(failure: Any) -> str:
    """The innermost message of a failure chain, which is the one a person can act on."""
    message = ""
    seen = 0
    while failure is not None and seen < 8 and (failure.message or failure.HasField("cause")):
        message = failure.message or message
        if not failure.HasField("cause"):
            break
        failure = failure.cause
        seen += 1
    return message[:400]


def normalize_event(event: Any) -> dict[str, Any]:
    """One ``HistoryEvent`` as a small dict with only the fields ``build_view`` uses."""
    from temporalio.api.enums.v1 import EventType

    kind = EventType.Name(event.event_type).removeprefix("EVENT_TYPE_").lower()
    out: dict[str, Any] = {"id": event.event_id, "type": kind, "time": _ts(event.event_time)}
    attributes = getattr(event, f"{kind}_event_attributes", None)
    if attributes is None:
        return out
    if kind == "workflow_execution_started":
        out["workflow_type"] = attributes.workflow_type.name
        out["input"] = _first_payload(attributes.input)
    elif kind == "workflow_task_started":
        out["scheduled_event_id"] = attributes.scheduled_event_id
        out["identity"] = attributes.identity
    elif kind == "activity_task_scheduled":
        out["activity_id"] = attributes.activity_id
        out["activity_type"] = attributes.activity_type.name
        summary = decode_payload(event.user_metadata.summary) if event.HasField("user_metadata") else None
        out["summary"] = summary if isinstance(summary, str) else ""
    elif kind == "activity_task_started":
        out["scheduled_event_id"] = attributes.scheduled_event_id
        out["identity"] = attributes.identity
        out["attempt"] = attributes.attempt or 1
    elif kind in ("activity_task_completed", "activity_task_canceled"):
        out["scheduled_event_id"] = attributes.scheduled_event_id
    elif kind in ("activity_task_failed", "activity_task_timed_out"):
        out["scheduled_event_id"] = attributes.scheduled_event_id
        out["message"] = _failure_message(attributes.failure)
    elif kind == "workflow_execution_signaled":
        out["name"] = attributes.signal_name
        out["input"] = _first_payload(attributes.input)
    elif kind == "timer_started":
        out["timer_id"] = attributes.timer_id
        out["seconds"] = attributes.start_to_fire_timeout.ToTimedelta().total_seconds()
        summary = decode_payload(event.user_metadata.summary) if event.HasField("user_metadata") else None
        out["summary"] = summary if isinstance(summary, str) else ""
    elif kind == "timer_fired":
        out["timer_id"] = attributes.timer_id
    elif kind == "workflow_properties_modified":
        out["memo"] = {key: decode_payload(value) for key, value in attributes.upserted_memo.fields.items()}
    elif kind == "workflow_execution_failed":
        out["message"] = _failure_message(attributes.failure)
    elif kind == "workflow_execution_terminated":
        out["message"] = attributes.reason[:400]
    return out


def normalize_pending(raw_description: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Pending Activities and the pending workflow task from a describe response.

    History only gets an Activity's started event when the Activity closes, so a model call
    that is running right now is visible here and nowhere else.
    """
    from temporalio.api.enums.v1 import PendingActivityState, PendingWorkflowTaskState

    activities = []
    for pending in raw_description.pending_activities:
        state = PendingActivityState.Name(pending.state).removeprefix("PENDING_ACTIVITY_STATE_").lower()
        activities.append(
            {
                "activity_id": pending.activity_id,
                "activity_type": pending.activity_type.name,
                "state": state,
                "attempt": pending.attempt or 1,
                "scheduled_time": _ts(pending.scheduled_time),
                "last_started_time": _ts(pending.last_started_time),
                "identity": pending.last_worker_identity,
                "message": _failure_message(pending.last_failure) if pending.HasField("last_failure") else "",
            }
        )
    task = None
    if raw_description.HasField("pending_workflow_task"):
        pending_task = raw_description.pending_workflow_task
        task = {
            "state": PendingWorkflowTaskState.Name(pending_task.state)
            .removeprefix("PENDING_WORKFLOW_TASK_STATE_")
            .lower(),
            "scheduled_time": _ts(pending_task.original_scheduled_time) or _ts(pending_task.scheduled_time),
            "started_time": _ts(pending_task.started_time),
            "attempt": pending_task.attempt or 1,
        }
    return activities, task


# ---------------------------------------------------------------------------------------
# History to view
# ---------------------------------------------------------------------------------------

_CLOSE_EVENTS = {
    "workflow_execution_completed": "completed",
    "workflow_execution_failed": "failed",
    "workflow_execution_timed_out": "timed_out",
    "workflow_execution_terminated": "terminated",
    "workflow_execution_canceled": "canceled",
}
_ACTIVITY_CLOSE = {
    "activity_task_completed": "completed",
    "activity_task_failed": "failed",
    "activity_task_timed_out": "timed_out",
    "activity_task_canceled": "canceled",
}


def build_view(
    events: Iterable[Mapping[str, Any]],
    pending_activities: Sequence[Mapping[str, Any]] = (),
    pending_workflow_task: Mapping[str, Any] | None = None,
    *,
    now: float | None = None,
    boot_times: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Timeline, wakes, sandboxes used and any wake in progress, with epoch-second times.

    A wake is the first task a sandbox runs for this run: its worker identity names a
    session not seen earlier. The latency runs from the event that made the task (the start,
    a Signal, a timer firing, an Activity being scheduled) to the task starting. A retry
    that lands on a new sandbox is a wake with no latency, because history does not record
    when the retry was offered.
    """
    timeline: list[dict[str, Any]] = []
    activities: dict[int, dict[str, Any]] = {}  # by the scheduled event's ID
    by_activity_id: dict[str, dict[str, Any]] = {}
    task_starts: list[dict[str, Any]] = []  # every task start, to find each sandbox's first
    trigger: tuple[str, float] | None = None  # what made the next workflow task

    for event in events:
        kind, at = event["type"], event["time"]
        if kind == "workflow_execution_started":
            timeline.append({"type": "started", "at": at})
            trigger = trigger or ("start", at)
        elif kind == "workflow_execution_signaled":
            decision = event.get("input")
            approved = decision.get("approved") if isinstance(decision, dict) else None
            timeline.append({"type": "signal", "at": at, "name": event.get("name", ""), "approved": approved})
            trigger = trigger or ("signal", at)
        elif kind == "timer_started":
            timeline.append(
                {
                    "type": "timer_started",
                    "at": at,
                    "seconds": event.get("seconds"),
                    "fires_at": at + event["seconds"] if event.get("seconds") is not None else None,
                    "summary": event.get("summary", ""),
                }
            )
        elif kind == "timer_fired":
            timeline.append({"type": "timer_fired", "at": at})
            trigger = trigger or ("timer", at)
        elif kind == "workflow_task_started":
            cause, since = trigger or ("unknown", None)
            task_starts.append(
                {
                    "at": at,
                    "identity": event.get("identity", ""),
                    "kind": "workflow_task",
                    "cause": cause,
                    "since": since,
                }
            )
        elif kind == "workflow_task_completed":
            trigger = None
        elif kind == "activity_task_scheduled":
            activity_type = event.get("activity_type", "")
            is_agent = activity_type in AGENT_ACTIVITY_TYPES
            entry = {
                "type": "agent" if is_agent else "activity",
                "at": at,
                "agent": event.get("summary") or activity_type.rsplit(".", 1)[-1],
                "activity_id": event.get("activity_id", ""),
                "status": "scheduled",
                "attempt": 1,
                "scheduled_at": at,
                "started_at": None,
                "closed_at": None,
                "duration_seconds": None,
                "sandbox": None,
                "message": "",
            }
            activities[event["id"]] = entry
            by_activity_id[entry["activity_id"]] = entry
            timeline.append(entry)
        elif kind == "activity_task_started":
            known = activities.get(event.get("scheduled_event_id", -1))
            if known is not None:
                _started(known, task_starts, at, event.get("identity", ""), event.get("attempt", 1))
        elif kind in _ACTIVITY_CLOSE:
            known = activities.get(event.get("scheduled_event_id", -1))
            if known is not None:
                known["status"] = _ACTIVITY_CLOSE[kind]
                known["closed_at"] = at
                known["message"] = event.get("message", "")
                if known["started_at"] is not None:
                    known["duration_seconds"] = round(max(0.0, at - known["started_at"]), 1)
            trigger = trigger or ("activity_result", at)
        elif kind == "workflow_properties_modified":
            phase = (event.get("memo") or {}).get("phase")
            if isinstance(phase, str):
                timeline.append({"type": "phase", "at": at, "phase": phase})
        elif kind in _CLOSE_EVENTS:
            timeline.append(
                {"type": "closed", "at": at, "status": _CLOSE_EVENTS[kind], "message": event.get("message", "")}
            )

    # Activities that are running or waiting right now.
    pending_wake: dict[str, Any] | None = None
    for pending in pending_activities:
        live = by_activity_id.get(pending.get("activity_id", ""))
        if live is None or live["closed_at"] is not None:
            continue
        attempt = pending.get("attempt", 1)
        live["attempt"] = attempt
        live["message"] = pending.get("message", "")
        if pending.get("state") == "started" and pending.get("last_started_time") is not None:
            _started(live, task_starts, pending["last_started_time"], pending.get("identity", ""), attempt)
        elif attempt > 1:
            live["status"] = "retrying"
        else:
            pending_wake = _earlier(pending_wake, {"since": live["scheduled_at"], "cause": "activity"})
    if pending_workflow_task and pending_workflow_task.get("state") == "scheduled":
        cause, since = trigger or ("unknown", pending_workflow_task.get("scheduled_time"))
        if since is not None:
            pending_wake = _earlier(pending_wake, {"since": since, "cause": cause})
    if pending_wake is not None and now is not None:
        pending_wake["seconds"] = round(max(0.0, now - pending_wake["since"]), 1)

    # The first task each sandbox ran for this run is where it was woken.
    sandboxes: dict[str, dict[str, Any]] = {}
    wakes: list[dict[str, Any]] = []
    for start in sorted(task_starts, key=lambda item: item["at"]):
        identity = start["identity"]
        sandbox_id = sandbox_of(identity)
        box = sandboxes.get(sandbox_id)
        if box is None:
            box = sandboxes[sandbox_id] = {
                "id": sandbox_id,
                "identity": identity,
                "agent": agent_of(identity),
                "first_task_at": start["at"],
                "last_task_at": start["at"],
                "workflow_tasks": 0,
                "activities": 0,
                "agents": [],
            }
            since = start["since"]
            boot = (boot_times or {}).get(sandbox_id)
            wake = {
                "at": start["at"],
                "sandbox": sandbox_id,
                "cause": start["cause"],
                "latency_seconds": round(max(0.0, start["at"] - since), 1) if since is not None else None,
                "warm": boot is not None and since is not None and boot < since - WARM_MARGIN_SECONDS,
            }
            wakes.append(wake)
            timeline.append({"type": "wake", **wake})
        box["last_task_at"] = max(box["last_task_at"], start["at"])
        if start["kind"] == "workflow_task":
            box["workflow_tasks"] += 1
        else:
            box["activities"] += 1
            if start.get("agent") and start["agent"] not in box["agents"]:
                box["agents"].append(start["agent"])

    order = {"started": 0, "signal": 1, "timer_fired": 1, "wake": 2}
    timeline.sort(key=lambda item: (item["at"], order.get(item["type"], 3)))
    return {
        "timeline": timeline,
        "wakes": wakes,
        "sandboxes": list(sandboxes.values()),
        "pending_wake": pending_wake,
    }


def _started(entry: dict[str, Any], task_starts: list[dict[str, Any]], at: float, identity: str, attempt: int) -> None:
    entry["status"] = "running"
    entry["started_at"] = at
    entry["attempt"] = attempt
    entry["sandbox"] = sandbox_of(identity) if identity else None
    if identity:
        first_attempt = attempt <= 1
        task_starts.append(
            {
                "at": at,
                "identity": identity,
                "kind": "activity",
                "agent": entry["agent"],
                "cause": "activity" if first_attempt else "retry",
                "since": entry["scheduled_at"] if first_attempt else None,
            }
        )


def _earlier(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if current is None or candidate["since"] < current["since"]:
        return candidate
    return current


_TIME_KEYS = {
    "at",
    "since",
    "fires_at",
    "scheduled_at",
    "started_at",
    "closed_at",
    "first_task_at",
    "last_task_at",
}


def with_iso_times(value: Any) -> Any:
    """The same structure with every epoch-second time field as an ISO 8601 string."""
    if isinstance(value, dict):
        return {
            key: iso(item) if key in _TIME_KEYS and isinstance(item, (int, float)) else with_iso_times(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [with_iso_times(item) for item in value]
    return value
