"""Wake-path probe: start -> activity -> wait for Signal -> activity -> durable timer -> activity.

Each stage should be handled by a different, freshly woken worker process.
"""

import asyncio
import os
import signal
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

TASK_QUEUE = "wake-probe"
OPTS = {"start_to_close_timeout": timedelta(seconds=30)}


@activity.defn
async def stamp(stage: str) -> str:
    return f"{stage}@pid{os.getpid()}"


@workflow.defn
class WakeProbe:
    def __init__(self) -> None:
        self.approved = False

    @workflow.signal
    def approve(self) -> None:
        self.approved = True

    @workflow.run
    async def run(self, timer_seconds: int) -> list[str]:
        out = [await workflow.execute_activity(stamp, "start", **OPTS)]
        await workflow.wait_condition(lambda: self.approved)
        out.append(await workflow.execute_activity(stamp, "after-signal", **OPTS))
        await workflow.sleep(timer_seconds)
        out.append(await workflow.execute_activity(stamp, "after-timer", **OPTS))
        return out


async def main() -> None:
    client = await Client.connect("127.0.0.1:7233", namespace="default")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[WakeProbe],
        activities=[stamp],
        identity=f"probe@pid{os.getpid()}",
        graceful_shutdown_timeout=timedelta(seconds=3),
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name="probe", build_id="v1"),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    )
    async with worker:
        print(f"probe worker polling pid={os.getpid()}", flush=True)
        await stop.wait()
    print(f"probe worker stopped pid={os.getpid()}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
