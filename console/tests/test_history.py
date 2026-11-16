"""History to timeline, wakes and sandboxes, from small hand-made histories."""

from __future__ import annotations

from typing import Any

from app.history import (
    build_view,
    normalize_event,
    normalize_pending,
    sandbox_of,
    with_iso_times,
)

S1 = "foundry/tfd-worker-abc/v1/aaaaaaaaaaaa/pid10"
S2 = "foundry/tfd-worker-abc/v1/bbbbbbbbbbbb/pid11"
S3 = "foundry/tfd-worker-abc/v1/cccccccccccc/pid12"
MODEL = "microsoft_agent_framework.invoke_model"


class History:
    """Builds events in the shape ``normalize_event`` produces."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(self, kind: str, time: float, **fields: Any) -> int:
        event_id = len(self.events) + 1
        self.events.append({"id": event_id, "type": kind, "time": time, **fields})
        return event_id

    def workflow_task(self, scheduled: float, started: float, identity: str, completed: float | None = None) -> None:
        scheduled_id = self.add("workflow_task_scheduled", scheduled)
        self.add("workflow_task_started", started, scheduled_event_id=scheduled_id, identity=identity)
        self.add("workflow_task_completed", completed if completed is not None else started + 0.02)

    def activity(
        self,
        agent: str,
        scheduled: float,
        started: float,
        completed: float,
        identity: str,
        attempt: int = 1,
        close: str = "activity_task_completed",
        activity_id: str | None = None,
    ) -> None:
        scheduled_id = self.add(
            "activity_task_scheduled",
            scheduled,
            activity_id=activity_id or str(len(self.events)),
            activity_type=MODEL,
            summary=agent,
        )
        self.add("activity_task_started", started, scheduled_event_id=scheduled_id, identity=identity, attempt=attempt)
        self.add(close, completed, scheduled_event_id=scheduled_id, message="boom" if "failed" in close else "")


def started_run() -> History:
    history = History()
    history.add("workflow_execution_started", 100.0, input={"question": "why"})
    history.workflow_task(100.0, 105.0, S1)
    return history


def test_identity_names_the_sandbox() -> None:
    assert sandbox_of(S1) == "aaaaaaaaaaaa"
    assert sandbox_of("local/host/pid3") == "local/host/pid3"
    assert sandbox_of("123@laptop") == "123@laptop"


def test_wake_at_start_is_measured_from_the_start() -> None:
    view = build_view(started_run().events)
    assert view["wakes"] == [
        {"at": 105.0, "sandbox": "aaaaaaaaaaaa", "cause": "start", "latency_seconds": 5.0, "warm": False}
    ]
    assert [item["type"] for item in view["timeline"]] == ["started", "wake"]
    assert view["sandboxes"][0]["id"] == "aaaaaaaaaaaa"
    assert view["sandboxes"][0]["agent"] == "tfd-worker-abc"
    assert view["pending_wake"] is None


def test_no_wake_when_the_same_sandbox_continues() -> None:
    history = started_run()
    history.activity("planner", 105.1, 105.2, 111.0, S1)
    history.workflow_task(111.0, 111.1, S1)
    view = build_view(history.events)
    assert len(view["wakes"]) == 1
    box = view["sandboxes"][0]
    assert (box["workflow_tasks"], box["activities"], box["agents"]) == (2, 1, ["planner"])
    planner = next(item for item in view["timeline"] if item["type"] == "agent")
    assert planner["agent"] == "planner"
    assert planner["status"] == "completed"
    assert planner["sandbox"] == "aaaaaaaaaaaa"
    assert planner["duration_seconds"] == 5.8


def test_wake_after_a_signal() -> None:
    history = started_run()
    history.add("workflow_properties_modified", 105.1, memo={"phase": "awaiting_approval"})
    history.add("workflow_execution_signaled", 300.0, name="decide", input={"approved": True, "note": ""})
    history.workflow_task(300.0, 304.8, S2)
    view = build_view(history.events)
    assert [(wake["cause"], wake["sandbox"], wake["latency_seconds"]) for wake in view["wakes"]] == [
        ("start", "aaaaaaaaaaaa", 5.0),
        ("signal", "bbbbbbbbbbbb", 4.8),
    ]
    kinds = [item["type"] for item in view["timeline"]]
    assert kinds == ["started", "wake", "phase", "signal", "wake"]
    signal = view["timeline"][3]
    assert (signal["name"], signal["approved"]) == ("decide", True)


def test_wake_after_a_timer() -> None:
    history = started_run()
    history.add("timer_started", 106.0, timer_id="1", seconds=60.0, summary="Wait before the follow-up")
    history.add("timer_fired", 166.0, timer_id="1")
    history.workflow_task(166.0, 172.2, S3)
    view = build_view(history.events)
    wake = view["wakes"][-1]
    assert (wake["cause"], wake["sandbox"], wake["latency_seconds"]) == ("timer", "cccccccccccc", 6.2)
    timer = next(item for item in view["timeline"] if item["type"] == "timer_started")
    assert timer["fires_at"] == 166.0


def test_wake_latency_survives_a_sticky_queue_timeout() -> None:
    # The task is first offered to the dead sandbox's sticky queue, times out, and is
    # scheduled again. The wait is still measured from the Signal.
    history = started_run()
    history.add("workflow_execution_signaled", 300.0, name="decide", input={"approved": True})
    history.add("workflow_task_scheduled", 300.0)
    history.add("workflow_task_timed_out", 310.0)
    history.workflow_task(310.0, 314.0, S2)
    view = build_view(history.events)
    assert view["wakes"][-1]["latency_seconds"] == 14.0


def test_activity_retried_on_another_sandbox_is_a_wake_without_latency() -> None:
    history = started_run()
    history.activity("researcher 1", 105.1, 140.0, 150.0, S2, attempt=2)
    view = build_view(history.events)
    wake = view["wakes"][-1]
    assert (wake["cause"], wake["sandbox"], wake["latency_seconds"]) == ("retry", "bbbbbbbbbbbb", None)
    agent = next(item for item in view["timeline"] if item["type"] == "agent")
    assert (agent["attempt"], agent["sandbox"]) == (2, "bbbbbbbbbbbb")


def test_fan_out_onto_a_second_sandbox_is_a_wake_measured_from_scheduling() -> None:
    history = started_run()
    history.activity("researcher 1", 105.1, 105.2, 120.0, S1)
    history.activity("researcher 2", 105.1, 110.6, 121.0, S2)
    view = build_view(history.events)
    wake = view["wakes"][-1]
    assert (wake["cause"], wake["latency_seconds"]) == ("activity", 5.5)


def test_earliest_task_decides_the_wake_even_when_history_lists_it_later() -> None:
    # An Activity's started event is written when the Activity closes, so a workflow task
    # that the same sandbox ran later can come first in history.
    history = started_run()
    scheduled_id = history.add("activity_task_scheduled", 105.1, activity_id="7", activity_type=MODEL, summary="writer")
    history.add("workflow_execution_signaled", 130.0, name="decide", input=None)
    history.workflow_task(130.0, 130.1, S2)
    history.add("activity_task_started", 110.0, scheduled_event_id=scheduled_id, identity=S2, attempt=1)
    history.add("activity_task_completed", 140.0, scheduled_event_id=scheduled_id)
    view = build_view(history.events)
    wake = view["wakes"][-1]
    assert (wake["cause"], wake["at"], wake["latency_seconds"]) == ("activity", 110.0, 4.9)


def test_running_activity_comes_from_the_describe_response() -> None:
    history = started_run()
    history.add("activity_task_scheduled", 105.1, activity_id="1", activity_type=MODEL, summary="planner")
    pending = [
        {
            "activity_id": "1",
            "state": "started",
            "attempt": 1,
            "last_started_time": 105.2,
            "identity": S1,
            "message": "",
        }
    ]
    view = build_view(history.events, pending, now=108.0)
    agent = next(item for item in view["timeline"] if item["type"] == "agent")
    assert (agent["status"], agent["sandbox"], agent["started_at"]) == ("running", "aaaaaaaaaaaa", 105.2)
    assert view["sandboxes"][0]["activities"] == 1
    assert view["pending_wake"] is None


def test_pending_workflow_task_is_a_wake_in_progress() -> None:
    history = started_run()
    history.add("workflow_execution_signaled", 300.0, name="decide", input={"approved": True})
    history.add("workflow_task_scheduled", 300.0)
    view = build_view(history.events, (), {"state": "scheduled", "scheduled_time": 300.0, "attempt": 1}, now=302.5)
    assert view["pending_wake"] == {"since": 300.0, "cause": "signal", "seconds": 2.5}


def test_activity_waiting_for_a_worker_is_a_wake_in_progress() -> None:
    history = started_run()
    history.add("activity_task_scheduled", 105.1, activity_id="1", activity_type=MODEL, summary="planner")
    pending = [{"activity_id": "1", "state": "scheduled", "attempt": 1, "last_started_time": None, "identity": ""}]
    view = build_view(history.events, pending, now=107.1)
    assert view["pending_wake"] == {"since": 105.1, "cause": "activity", "seconds": 2.0}
    retrying = [
        {
            "activity_id": "1",
            "state": "scheduled",
            "attempt": 3,
            "last_started_time": 106.0,
            "identity": S1,
            "message": "rate limited",
        }
    ]
    view = build_view(history.events, retrying, now=107.1)
    agent = next(item for item in view["timeline"] if item["type"] == "agent")
    assert (agent["status"], agent["attempt"], agent["message"]) == ("retrying", 3, "rate limited")
    assert view["pending_wake"] is None


def test_warm_sandbox_is_not_reported_as_woken() -> None:
    # Another run's sandbox was already up and took this run's first task at once.
    view = build_view(started_run().events, boot_times={"aaaaaaaaaaaa": 60.0})
    assert view["wakes"][0]["warm"] is True
    view = build_view(started_run().events, boot_times={"aaaaaaaaaaaa": 101.5})
    assert view["wakes"][0]["warm"] is False


def test_failed_activity_and_failed_run() -> None:
    history = started_run()
    history.activity("writer", 105.1, 105.2, 109.0, S1, close="activity_task_failed")
    history.add("workflow_execution_failed", 110.0, message="Every researcher failed.")
    view = build_view(history.events)
    agent = next(item for item in view["timeline"] if item["type"] == "agent")
    assert (agent["status"], agent["message"]) == ("failed", "boom")
    closed = view["timeline"][-1]
    assert (closed["type"], closed["status"], closed["message"]) == ("closed", "failed", "Every researcher failed.")


def test_non_agent_activity_is_named_by_its_type() -> None:
    history = started_run()
    scheduled_id = history.add("activity_task_scheduled", 105.1, activity_id="1", activity_type="where", summary="")
    history.add("activity_task_started", 105.2, scheduled_event_id=scheduled_id, identity=S1, attempt=1)
    history.add("activity_task_completed", 105.3, scheduled_event_id=scheduled_id)
    entry = next(item for item in build_view(history.events)["timeline"] if item["type"] == "activity")
    assert entry["agent"] == "where"


def test_times_become_iso_strings() -> None:
    view = with_iso_times(build_view(started_run().events))
    assert view["wakes"][0]["at"] == "1970-01-01T00:01:45.000Z"
    assert view["wakes"][0]["latency_seconds"] == 5.0
    assert view["sandboxes"][0]["first_task_at"].endswith("Z")


def test_normalize_real_protobuf_events() -> None:
    from google.protobuf.duration_pb2 import Duration
    from temporalio.api.common.v1 import Payload
    from temporalio.api.enums.v1 import (
        EventType,
        PendingActivityState,
        PendingWorkflowTaskState,
    )
    from temporalio.api.history.v1 import HistoryEvent
    from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse

    def json_payload(text: str) -> Payload:
        return Payload(metadata={"encoding": b"json/plain"}, data=text.encode())

    scheduled = HistoryEvent(event_id=6, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    scheduled.event_time.FromSeconds(1000)
    scheduled.user_metadata.summary.CopyFrom(json_payload('"researcher 2"'))
    scheduled.activity_task_scheduled_event_attributes.activity_id = "4"
    scheduled.activity_task_scheduled_event_attributes.activity_type.name = MODEL
    assert normalize_event(scheduled) == {
        "id": 6,
        "type": "activity_task_scheduled",
        "time": 1000.0,
        "activity_id": "4",
        "activity_type": MODEL,
        "summary": "researcher 2",
    }

    started = HistoryEvent(event_id=7, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED)
    started.event_time.FromMilliseconds(1000500)
    started.activity_task_started_event_attributes.scheduled_event_id = 6
    started.activity_task_started_event_attributes.identity = S1
    started.activity_task_started_event_attributes.attempt = 2
    assert normalize_event(started)["attempt"] == 2
    assert normalize_event(started)["time"] == 1000.5

    signaled = HistoryEvent(event_id=9, event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED)
    signaled.workflow_execution_signaled_event_attributes.signal_name = "decide"
    signaled.workflow_execution_signaled_event_attributes.input.payloads.append(json_payload('{"approved": false}'))
    assert normalize_event(signaled)["input"] == {"approved": False}

    timer = HistoryEvent(event_id=10, event_type=EventType.EVENT_TYPE_TIMER_STARTED)
    timer.timer_started_event_attributes.start_to_fire_timeout.CopyFrom(Duration(seconds=60))
    assert normalize_event(timer)["seconds"] == 60.0

    memo = HistoryEvent(event_id=11, event_type=EventType.EVENT_TYPE_WORKFLOW_PROPERTIES_MODIFIED)
    memo.workflow_properties_modified_event_attributes.upserted_memo.fields["phase"].CopyFrom(json_payload('"writing"'))
    assert normalize_event(memo)["memo"] == {"phase": "writing"}

    failed = HistoryEvent(event_id=12, event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED)
    failed.workflow_execution_failed_event_attributes.failure.message = "Workflow failed"
    failed.workflow_execution_failed_event_attributes.failure.cause.message = "Every researcher failed."
    assert normalize_event(failed)["message"] == "Every researcher failed."

    description = DescribeWorkflowExecutionResponse()
    pending = description.pending_activities.add()
    pending.activity_id = "4"
    pending.activity_type.name = MODEL
    pending.state = PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
    pending.attempt = 1
    pending.last_started_time.FromSeconds(1001)
    pending.last_worker_identity = S1
    description.pending_workflow_task.state = PendingWorkflowTaskState.PENDING_WORKFLOW_TASK_STATE_SCHEDULED
    description.pending_workflow_task.scheduled_time.FromSeconds(1002)
    activities, task = normalize_pending(description)
    assert activities == [
        {
            "activity_id": "4",
            "activity_type": MODEL,
            "state": "started",
            "attempt": 1,
            "scheduled_time": None,
            "last_started_time": 1001.0,
            "identity": S1,
            "message": "",
        }
    ]
    assert task == {"state": "scheduled", "scheduled_time": 1002.0, "started_time": None, "attempt": 1}
