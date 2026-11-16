"""Message probe: what do a Query and an Update do when no worker is running?"""

import asyncio
import os
import signal
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

TASK_QUEUE = "msg-probe"


@workflow.defn
class MsgProbe:
    def __init__(self) -> None:
        self.decisions: list[str] = []

    @workflow.update
    async def decide(self, decision: str) -> str:
        self.decisions.append(decision)
        return f"recorded {decision} by {workflow.info().workflow_id}"

    @workflow.query
    def phase(self) -> str:
        return f"decisions={self.decisions}"

    @workflow.run
    async def run(self, needed: int) -> list[str]:
        await workflow.wait_condition(lambda: len(self.decisions) >= needed)
        return self.decisions


async def main() -> None:
    client = await Client.connect("127.0.0.1:7233", namespace="default")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[MsgProbe],
        identity=f"msg-probe@pid{os.getpid()}",
        graceful_shutdown_timeout=timedelta(seconds=3),
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name="msgprobe", build_id="v1"),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    )
    async with worker:
        print(f"msg probe worker polling pid={os.getpid()}", flush=True)
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
