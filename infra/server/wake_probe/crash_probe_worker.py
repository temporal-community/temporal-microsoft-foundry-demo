"""Crash probe: a heartbeating Activity is interrupted by SIGKILL of its worker, then a
Signal arrives after a second SIGKILL (no graceful shutdown, so the sticky queue is not released)."""

import asyncio
import os
import signal
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

TASK_QUEUE = "crash-probe"


@activity.defn
async def slow(seconds: int) -> str:
    for _ in range(seconds):
        activity.heartbeat()
        await asyncio.sleep(1)
    return f"slow done attempt={activity.info().attempt} pid={os.getpid()}"


@workflow.defn
class CrashProbe:
    def __init__(self) -> None:
        self.approved = False

    @workflow.signal
    def approve(self) -> None:
        self.approved = True

    @workflow.run
    async def run(self, seconds: int) -> list[str]:
        out = [
            await workflow.execute_activity(
                slow,
                seconds,
                start_to_close_timeout=timedelta(seconds=120),
                heartbeat_timeout=timedelta(seconds=6),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1)),
            )
        ]
        await workflow.wait_condition(lambda: self.approved)
        out.append(f"signal handled by pid={workflow.info().workflow_id and 'see-history'}")
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
        workflows=[CrashProbe],
        activities=[slow],
        identity=f"crash-probe@pid{os.getpid()}",
        graceful_shutdown_timeout=timedelta(seconds=3),
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name="crashprobe", build_id="v1"),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    )
    async with worker:
        print(f"crash probe worker polling pid={os.getpid()}", flush=True)
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
