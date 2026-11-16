"""Tell the demo console what this sandbox is doing.

Foundry's own session status lags by minutes, so the sandbox reports its lifecycle itself.
Best effort by design: a console that is down or slow must never hold up the worker.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .settings import Settings


async def report(settings: Settings, event: str, **detail: Any) -> bool:
    if not settings.console_url:
        return False
    headers = {}
    if settings.console_token:
        headers["Authorization"] = f"Bearer {settings.console_token}"
    body = {
        "session": settings.session_id,
        "agent": settings.agent_name,
        "agent_version": settings.agent_version,
        "build_id": settings.build_id,
        "event": event,
        "at": time.time(),
        "detail": detail,
    }
    try:
        async with httpx.AsyncClient(timeout=4) as http:
            response = await http.post(
                f"{settings.console_url}/api/internal/sandbox-events", json=body, headers=headers
            )
        return response.status_code < 300
    except Exception:
        return False
