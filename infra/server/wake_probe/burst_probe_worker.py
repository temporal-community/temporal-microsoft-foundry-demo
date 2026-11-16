"""Burst probe: N concurrent Activities from zero workers; how many workers does the controller launch?

usage: burst_probe_worker.py <deployment-name>
"""

import asyncio
import os
import signal
import sys
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

DEPLOYMENT = sys.argv[1]
TASK_QUEUE = f"{DEPLOYMENT}-tq"


@activity.defn
async def work(seconds: int) -> int:
    for _ in range(seconds):
        activity.heartbeat()
        await asyncio.sleep(1)
    return os.getpid()


@workflow.defn
class BurstProbe:
    @workflow.run
    async def run(self, args: list[int]) -> list[int]:
        count, seconds = args
        return list(
            await asyncio.gather(
                *(
                    workflow.execute_activity(
                        work,
                        seconds,
                        start_to_close_timeout=timedelta(seconds=120),
                        heartbeat_timeout=timedelta(seconds=10),
                    )
                    for _ in range(count)
                )
            )
        )


async def main() -> None:
    client = await Client.connect("127.0.0.1:7233", namespace="default")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[BurstProbe],
        activities=[work],
        identity=f"{DEPLOYMENT}@pid{os.getpid()}",
        max_concurrent_activities=2,
        graceful_shutdown_timeout=timedelta(seconds=3),
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=DEPLOYMENT, build_id="v1"),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    )
    async with worker:
        print(f"{DEPLOYMENT} worker polling pid={os.getpid()}", flush=True)
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
