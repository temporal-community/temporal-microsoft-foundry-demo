"""Start, steer and inspect the demo workflows from a laptop.

Everything here works with no worker running: it starts, signals, and reads describe and
history. It never sends a Query, because a Query wakes a sandbox just to look.

    tfd.py start-probe --id p1 --signal --timer 60
    tfd.py go --id p1
    tfd.py start-research --id research-1 --question "..." --follow-up-after 120
    tfd.py decide --id research-1 --approve --note "looks right"
    tfd.py show --id research-1

Connection: TEMPORAL_ADDRESS (default 127.0.0.1:7233), TEMPORAL_NAMESPACE, TEMPORAL_TASK_QUEUE.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from temporalio.client import Client  # noqa: E402
from temporalio.microsoft_agent_framework import MicrosoftAgentFrameworkPlugin  # noqa: E402

import connection  # noqa: E402
from tfd_worker.shared import ProbeRequest, ReportRequest  # noqa: E402

TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "tfd")


async def connect() -> Client:
    # The plugin gives this client the same data converter the worker uses.
    return await connection.connect(plugins=[MicrosoftAgentFrameworkPlugin()])


async def show(client: Client, workflow_id: str, *, verbose: bool) -> None:
    handle = client.get_workflow_handle(workflow_id)
    description = await handle.describe()
    print(f"workflow {workflow_id}: {description.status.name if description.status else '?'}")
    memo = await description.memo()
    for key, value in memo.items():
        text = str(value)
        print(f"  memo.{key}: {text if verbose or len(text) < 160 else text[:160] + ' ...'}")
    scheduled: dict[int, str] = {}
    async for item in handle.fetch_history_events():
        stamp = item.event_time.ToDatetime().strftime("%H:%M:%S")
        if item.HasField("activity_task_scheduled_event_attributes"):
            scheduled[item.event_id] = item.activity_task_scheduled_event_attributes.activity_type.name
        elif item.HasField("activity_task_started_event_attributes"):
            started = item.activity_task_started_event_attributes
            name = scheduled.get(started.scheduled_event_id, "?")
            print(f"  {stamp} activity {name} attempt {started.attempt} on {started.identity}")
        elif item.HasField("workflow_execution_signaled_event_attributes"):
            print(f"  {stamp} signal {item.workflow_execution_signaled_event_attributes.signal_name}")
        elif item.HasField("timer_started_event_attributes"):
            print(f"  {stamp} timer started")
        elif item.HasField("timer_fired_event_attributes"):
            print(f"  {stamp} timer fired")
        elif item.HasField("workflow_execution_completed_event_attributes"):
            print(f"  {stamp} completed")
    if description.status and description.status.name == "COMPLETED" and verbose:
        print(await handle.result())


async def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("start-probe")
    probe.add_argument("--id", required=True)
    probe.add_argument("--busy", type=int, default=0)
    probe.add_argument("--signal", action="store_true")
    probe.add_argument("--timer", type=int, default=0)
    probe.add_argument("--fan-out", type=int, default=0)
    probe.add_argument("--fan-out-seconds", type=int, default=5)
    fault = commands.add_parser("start-fault")
    fault.add_argument("--id", required=True)
    report = commands.add_parser("start-report")
    report.add_argument("--id", required=True)
    report.add_argument("--subject", required=True)
    report.add_argument("--follow-up-after", type=int, default=300)
    research = commands.add_parser("start-research")
    research.add_argument("--id", required=True)
    research.add_argument("--question", required=True)
    research.add_argument("--max-subquestions", type=int, default=4)
    research.add_argument("--follow-up-after", type=int, default=0)
    research.add_argument("--auto-approve", action="store_true")
    go = commands.add_parser("go")
    go.add_argument("--id", required=True)
    decide = commands.add_parser("decide")
    decide.add_argument("--id", required=True)
    group = decide.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")
    decide.add_argument("--note", default="")
    decide.add_argument("--subquestion", action="append", help="replace the plan (repeatable)")
    shown = commands.add_parser("show")
    shown.add_argument("--id", required=True)
    shown.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    client = await connect()
    if args.command == "start-probe":
        request = ProbeRequest(
            busy_seconds=args.busy,
            wait_for_signal=args.signal,
            timer_seconds=args.timer,
            fan_out=args.fan_out,
            fan_out_seconds=args.fan_out_seconds,
        )
        await client.start_workflow("ProbeWorkflow", request, id=args.id, task_queue=TASK_QUEUE)
        print(f"started ProbeWorkflow {args.id}")
    elif args.command == "start-fault":
        await client.start_workflow("FaultProbeWorkflow", id=args.id, task_queue=TASK_QUEUE)
        print(f"started FaultProbeWorkflow {args.id}")
    elif args.command == "start-report":
        request = ReportRequest(subject=args.subject, follow_up_after_seconds=args.follow_up_after)
        await client.start_workflow(
            "ReportFollowUpWorkflow", request, id=args.id, task_queue=TASK_QUEUE
        )
        print(f"started ReportFollowUpWorkflow {args.id}")
    elif args.command == "start-research":
        # Plain JSON, as the console sends it (see docs/contract.md).
        await client.start_workflow(
            "DeepResearchWorkflow",
            {
                "question": args.question,
                "max_subquestions": args.max_subquestions,
                "follow_up_after_seconds": args.follow_up_after,
                "auto_approve": args.auto_approve,
            },
            id=args.id,
            task_queue=TASK_QUEUE,
        )
        print(f"started DeepResearchWorkflow {args.id}")
    elif args.command == "go":
        await client.get_workflow_handle(args.id).signal("go")
        print("signal go sent")
    elif args.command == "decide":
        decision = {"approved": args.approve, "note": args.note}
        if args.subquestion:
            decision["subquestions"] = args.subquestion
        await client.get_workflow_handle(args.id).signal("decide", decision)
        print("decision sent")
    elif args.command == "show":
        await show(client, args.id, verbose=args.verbose)


if __name__ == "__main__":
    asyncio.run(main())
