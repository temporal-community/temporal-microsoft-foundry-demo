"""The Temporal worker. Runs as a child process of the supervisor.

Exit code 0 means the worker stopped on request or because it had nothing to do; anything
else is a failure the supervisor may retry.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import timedelta
from typing import Any

from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.microsoft_agent_framework import MicrosoftAgentFrameworkPlugin
from temporalio.service import TLSConfig
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    Worker,
    WorkerDeploymentConfig,
)

from .activities import busy, exit_process, where
from .diagnostics import ping_own_session
from .models import chat_client_factory
from .report import report
from .settings import MODEL_NAME, Settings
from .supervisor import event
from .workflows import (
    DeepResearchWorkflow,
    FaultProbeWorkflow,
    ProbeWorkflow,
    ReportFollowUpWorkflow,
)

logger = logging.getLogger(__name__)

LIFETIME_TICK_SECONDS = 5.0
ALIVE_REPORT_SECONDS = 15.0
SHUTDOWN_IDLE_SECONDS = 4.0  # a worker with nothing in flight should stop at once


class ActivityTracker(Interceptor):
    """Counts Activities in flight, so the worker can tell when it has gone quiet.

    The plugin runs every model call as an Activity, so this sees an agent's whole turn.
    Same approach as samples-python/bedrock_agentcore/strands_agent/agentcore_worker.py.
    """

    def __init__(self) -> None:
        self.inflight = 0
        self.last_change = time.monotonic()

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _TrackedActivity(next, self)

    def busy(self, linger: float) -> bool:
        """An Activity is running, or one ended less than ``linger`` seconds ago."""
        return self.inflight > 0 or time.monotonic() - self.last_change < linger


class _TrackedActivity(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, tracker: ActivityTracker) -> None:
        super().__init__(next)
        self._tracker = tracker

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self._tracker.inflight += 1
        self._tracker.last_change = time.monotonic()
        try:
            return await self.next.execute_activity(input)
        finally:
            self._tracker.inflight -= 1
            self._tracker.last_change = time.monotonic()


def tls_config(settings: Settings) -> TLSConfig | bool:
    if not settings.temporal_tls:
        return False
    if settings.temporal_server_ca is None and settings.temporal_server_name is None:
        return True
    return TLSConfig(
        server_root_ca_cert=settings.temporal_server_ca, domain=settings.temporal_server_name
    )


async def connect(settings: Settings) -> Client:
    plugin = MicrosoftAgentFrameworkPlugin(chat_clients={MODEL_NAME: chat_client_factory(settings)})
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        api_key=settings.temporal_api_key,
        tls=tls_config(settings),
        plugins=[plugin],
    )


def build_worker(client: Client, settings: Settings, tracker: ActivityTracker) -> Worker:
    deployment = None
    if settings.versioning:
        deployment = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=settings.deployment_name, build_id=settings.build_id
            ),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        )
    return Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[DeepResearchWorkflow, ProbeWorkflow, FaultProbeWorkflow, ReportFollowUpWorkflow],
        activities=[where, busy, exit_process],
        interceptors=[tracker],
        identity=settings.worker_identity,
        graceful_shutdown_timeout=timedelta(seconds=settings.drain_seconds),
        # Poller counts stay at the SDK defaults. With fewer Activity pollers than parallel
        # researchers, some tasks miss a waiting poller, and the controller answers every such
        # task with one more sandbox (measured with two pollers: three sandboxes for one run).
        # As in the provider's example worker: every Activity goes through the task queue.
        disable_eager_activity_execution=True,
        deployment_config=deployment,
    )


async def run(settings: Settings) -> str:
    """Poll until asked to stop, or until quiet for long enough. Returns the reason."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    event("worker_connecting", address=settings.temporal_address, identity=settings.worker_identity)
    client = await connect(settings)
    tracker = ActivityTracker()
    async with build_worker(client, settings, tracker):
        event(
            "worker_polling",
            task_queue=settings.task_queue,
            version=f"{settings.deployment_name}.{settings.build_id}",
        )
        await report(settings, "polling", identity=settings.worker_identity)
        reason = await poll_until_done(settings, tracker, stop)
        event("worker_draining", reason=reason, inflight=tracker.inflight)
        # Leaving this block shuts the worker down, which talks to the server and can stall
        # when the connection is broken. A worker that is neither polling properly nor gone
        # can still take a workflow task it will not finish, so the exit is bounded.
        limit = (settings.drain_seconds if tracker.inflight else SHUTDOWN_IDLE_SECONDS) + 2
        loop.call_later(limit, _exit_now, reason)
    event("worker_stopped", reason=reason)
    return reason


def _exit_now(reason: str) -> None:
    event("worker_shutdown_cut_short", reason=reason)
    os._exit(0)


async def poll_until_done(settings: Settings, tracker: ActivityTracker, stop: asyncio.Event) -> str:
    """Return why the worker should stop polling: "stop", "quiet" or "deadline".

    The sandbox lives until its idle timeout after the last inbound request, and is then
    stopped without warning. The worker therefore keeps a deadline that many seconds (less a
    margin) after the last inbound request and drains when it passes. While Activities are
    running it sends its own session a request every ``keepalive_seconds``, which moves the
    deadline; once quiet it stops doing that and the sandbox is reclaimed.
    """
    budget = settings.worker_budget_seconds
    last_inbound = time.monotonic()  # the request that created this sandbox
    last_alive = time.monotonic()
    wake = {"deploymentName": settings.deployment_name, "buildId": settings.build_id}
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=LIFETIME_TICK_SECONDS)
            return "stop"
        except TimeoutError:
            pass
        now = time.monotonic()
        busy = tracker.busy(settings.busy_linger_seconds)
        if now - last_alive >= ALIVE_REPORT_SECONDS:
            last_alive = now
            await report(settings, "alive", inflight=tracker.inflight)
        if settings.keepalive_seconds and busy and now - last_inbound >= settings.keepalive_seconds:
            result = await ping_own_session(settings, wake)
            if 200 <= int(result.get("status_code", 0)) < 300:
                last_inbound = time.monotonic()
                event("keepalive", inflight=tracker.inflight)
                await report(settings, "keepalive", inflight=tracker.inflight)
            else:
                event("keepalive_failed", **{k: str(v)[:80].replace(" ", "_") for k, v in result.items()})
        quiet_for = now - tracker.last_change
        if settings.idle_exit_seconds and not busy and quiet_for >= settings.idle_exit_seconds:
            return "quiet"
        if budget is not None and time.monotonic() - last_inbound >= budget:
            return "deadline"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run(Settings.from_env()))


if __name__ == "__main__":
    main()
