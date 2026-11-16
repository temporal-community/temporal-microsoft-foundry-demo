"""Probes that run inside the sandbox, for measuring the platform from where the worker lives.

Enabled only when ``TFD_DIAGNOSTICS=1``. Each is a request to the invocations endpoint with a
``probe`` field; none of them returns a secret.
"""

from __future__ import annotations

import asyncio
import os
import platform
import socket
import ssl
import sys
import time
from typing import Any

import httpx

from .settings import FOUNDRY_SCOPE, Settings
from .supervisor import WorkerSupervisor, event

_STARTED = time.time()
_ENV_PREFIXES = ("FOUNDRY_", "AZURE_", "IDENTITY_", "OTEL_", "APPLICATIONINSIGHTS_", "TFD_")
_tick_task: asyncio.Task[None] | None = None


async def _foundry_token() -> str:
    from azure.identity.aio import DefaultAzureCredential

    async with DefaultAzureCredential() as credential:
        return (await credential.get_token(FOUNDRY_SCOPE)).token


def _agent_url(settings: Settings, suffix: str) -> str:
    return f"{settings.project_endpoint}/agents/{settings.agent_name}/endpoint/{suffix}"


async def stop_own_session(settings: Settings) -> dict[str, Any]:
    """Ask Foundry to stop this session's compute, as the agent's own identity."""
    if not (settings.project_endpoint and settings.session_id):
        return {"skipped": "not running in a Foundry session"}
    url = _agent_url(settings, f"sessions/{settings.session_id}:stop")
    try:
        token = await _foundry_token()
        async with httpx.AsyncClient(timeout=20) as http:
            response = await http.post(
                url, params={"api-version": "v1"}, headers={"Authorization": f"Bearer {token}"}
            )
        result = {"status_code": response.status_code, "body": response.text[:500]}
    except Exception as error:  # the caller wants the reason, whatever it is
        result = {"error": f"{type(error).__name__}: {error}"[:500]}
    event("self_stop", **{key: str(value)[:120] for key, value in result.items()})
    return result


async def ping_own_session(
    settings: Settings, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send this session an inbound request from inside the sandbox.

    Measured 2026-11-16: the hosted agent's identity may do this with no extra role, and the
    request resets the session's idle timer. It is the only keep-alive Foundry offers.
    """
    if os.environ.get("TFD_KEEPALIVE_FAKE") == "1":
        return {"status_code": 202, "fake": True}  # local runs: exercise the loop
    if not (settings.project_endpoint and settings.session_id):
        return {"skipped": "not running in a Foundry session"}
    url = _agent_url(settings, "protocols/invocations")
    try:
        token = await _foundry_token()
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(
                url,
                params={"api-version": "v1", "agent_session_id": settings.session_id},
                headers={"Authorization": f"Bearer {token}"},
                json=body if body is not None else {"probe": "status"},
            )
        return {
            "status_code": response.status_code,
            "session_header": response.headers.get("x-agent-session-id"),
            "body": response.text[:300],
        }
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"[:500]}


async def _connect(target: str, use_tls: bool, timeout: float) -> dict[str, Any]:
    host, _, port = target.rpartition(":")
    started = time.monotonic()
    try:
        context = ssl.create_default_context() if use_tls else None
        if context is not None:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # reachability only; nothing is sent
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port), ssl=context), timeout=timeout
        )
        peer = writer.get_extra_info("peername")
        writer.close()
        await writer.wait_closed()
        return {"target": target, "ok": True, "ms": round((time.monotonic() - started) * 1000), "peer": str(peer)}
    except Exception as error:
        return {
            "target": target,
            "ok": False,
            "ms": round((time.monotonic() - started) * 1000),
            "error": f"{type(error).__name__}: {error}"[:200],
        }


async def _public_address() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            return (await http.get("https://api.ipify.org")).text.strip()[:64]
    except Exception as error:
        return f"unknown ({type(error).__name__})"


async def _tick(
    settings: Settings, seconds: int, interval: int, self_ping_at: int, self_stop_at: int
) -> None:
    """Log a line every ``interval`` seconds; optionally act on this session from inside.

    The actions are scheduled here, not sent as separate probes, because a probe is itself an
    inbound request and would hide what a request from inside does to the idle timer.
    """
    for sequence in range(seconds // interval + 1):
        elapsed = sequence * interval
        event("tick", seq=sequence, uptime=round(time.time() - _STARTED))
        if self_ping_at and elapsed <= self_ping_at < elapsed + interval:
            result = await ping_own_session(settings)
            event("self_ping", **{key: str(value)[:160].replace(" ", "_") for key, value in result.items()})
        if self_stop_at and elapsed <= self_stop_at < elapsed + interval:
            await stop_own_session(settings)
        await asyncio.sleep(interval)
    event("tick_done")


async def run_probe(
    body: dict[str, Any], settings: Settings, supervisor: WorkerSupervisor
) -> tuple[int, dict[str, Any]]:
    global _tick_task
    probe = body.get("probe")
    if probe == "status":
        return 200, {"supervisor": supervisor.status(), "uptime": round(time.time() - _STARTED)}
    if probe == "env":
        return 200, {
            "agent": settings.agent_name,
            "agent_version": settings.agent_version,
            "session": settings.session_id,
            "build_id": settings.build_id,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "uptime": round(time.time() - _STARTED),
            "env_names": sorted(name for name in os.environ if name.startswith(_ENV_PREFIXES)),
        }
    if probe == "egress":
        timeout = float(body.get("timeout", 8))
        results = await asyncio.gather(
            *(_connect(str(target), bool(body.get("tls")), timeout) for target in body.get("targets", []))
        )
        return 200, {"public_address": await _public_address(), "results": list(results)}
    if probe == "tick":
        if _tick_task is None or _tick_task.done():
            _tick_task = asyncio.create_task(
                _tick(
                    settings,
                    int(body.get("seconds", 600)),
                    int(body.get("interval", 5)),
                    int(body.get("self_ping_at", 0)),
                    int(body.get("self_stop_at", 0)),
                )
            )
        return 200, {"ticking": True}
    if probe == "events":
        path = os.path.expanduser(os.environ.get("TFD_EVENT_LOG", ""))
        try:
            with open(path) as handle:
                lines = handle.read().splitlines()
        except OSError as error:
            return 200, {"error": f"{type(error).__name__}: {error}"}
        return 200, {"count": len(lines), "lines": lines[-int(body.get("last", 200)) :]}
    if probe == "self_stop":
        return 200, await stop_own_session(settings)
    if probe == "self_ping":
        return 200, await ping_own_session(settings)
    return 400, {"error": f"unknown probe {probe!r}"}
