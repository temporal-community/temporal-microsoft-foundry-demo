"""Foundry hosted agent entry point.

Temporal's worker controller POSTs ``{"deploymentName": ..., "buildId": ...}`` to this agent's
invocations endpoint when a task has no worker. Foundry answers a request that names no
session by creating a sandbox and starting this program in it.

The worker starts when the program starts, not when the request arrives. The controller's
Foundry provider hangs up after 5 s, a cold start takes about that long, and Foundry does not
deliver a request whose caller has gone: the sandbox is up, the handler never runs. A sandbox
only exists because someone asked for one, so starting at boot loses nothing. The handler
still answers at once and refuses a request meant for another build.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse

from tfd_worker import diagnostics
from tfd_worker.report import report
from tfd_worker.settings import Settings
from tfd_worker.supervisor import WorkerSupervisor, event


async def _read_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def create_app(
    settings: Settings | None = None, supervisor: WorkerSupervisor | None = None
) -> InvocationAgentServerHost:
    settings = settings or Settings.from_env()

    async def release_sandbox() -> None:
        # The sandbox cannot stop its own session (Foundry answers 403), so it tells the
        # console, which runs as the identity that created the session and can.
        await report(settings, "idle_exit")
        if settings.self_stop:
            await diagnostics.stop_own_session(settings)

    async def worker_failed() -> None:
        await report(settings, "worker_failed")

    supervisor = supervisor or WorkerSupervisor(
        settings, on_idle_exit=release_sandbox, on_failed=worker_failed
    )
    app = InvocationAgentServerHost()

    def on_sigterm() -> None:
        supervisor.request_shutdown()
        try:
            asyncio.get_running_loop().create_task(report(settings, "stopping"))
        except RuntimeError:
            pass

    app.register_pre_shutdown_callback(on_sigterm)

    @app.invoke_handler
    async def invoke(request: Request) -> JSONResponse:
        body = await _read_body(request)
        if "probe" in body:
            if not settings.diagnostics:
                return JSONResponse({"error": "diagnostics are disabled"}, status_code=403)
            status_code, payload = await diagnostics.run_probe(body, settings, supervisor)
            return JSONResponse(payload, status_code=status_code)

        if body.get("action") == "crash_worker":
            # Presenter's staged failure: the worker process dies mid-task, the sandbox survives.
            if not settings.allow_faults:
                return JSONResponse({"error": "faults are disabled"}, status_code=403)
            return JSONResponse({"status": "worker_killed", "pid": supervisor.crash_worker()})

        mismatch = settings.version_mismatch(body.get("deploymentName"), body.get("buildId"))
        if mismatch:
            event("wake_refused", reason=mismatch.replace(" ", "_"))
            return JSONResponse({"status": "wrong_version", "detail": mismatch}, status_code=409)

        state = "disabled" if settings.worker_disabled else supervisor.start()
        event("wake", worker=state, session=settings.session_id[:12])
        if state in ("shutting_down", "released"):
            return JSONResponse({"status": state}, status_code=503)
        return JSONResponse(
            {
                "status": "accepted",
                "worker": state,
                "deployment": settings.deployment_name,
                "build_id": settings.build_id,
                "task_queue": settings.task_queue,
                "session": settings.session_id,
            },
            status_code=202,
        )

    @app.shutdown_handler
    async def shutdown() -> None:
        closing = asyncio.create_task(supervisor.close())
        # Measurement aid: keep the process alive after SIGTERM and count the seconds, to see
        # how long the platform waits before it kills the sandbox.
        for second in range(int(os.environ.get("TFD_SHUTDOWN_HOLD_SECONDS", "0"))):
            event("after_sigterm", second=second)
            await asyncio.sleep(1)
        await closing

    app.state.tfd_supervisor = supervisor
    return app


async def serve() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    if settings.start_on_boot and not settings.worker_disabled:
        event("boot_start", worker=app.state.tfd_supervisor.start())
    boot = asyncio.create_task(report(settings, "boot"))
    await app.run_async()
    await boot


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    event("server_starting")
    asyncio.run(serve())
