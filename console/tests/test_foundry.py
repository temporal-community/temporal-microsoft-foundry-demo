"""The two requests the console sends to Foundry, checked against a recording transport.

The shapes are the ones scripts/foundry.py and the worker use against the real service:
``.../agents/{agent}/endpoint/sessions/{session}:stop`` and
``.../agents/{agent}/endpoint/protocols/invocations?agent_session_id={session}``.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from app.foundry import FoundryActions

ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"
SESSION = "0ca65fab16ecf6ee008GeujLesj0vCxYIr82r8Mf"


def actions(seen: list[httpx.Request], status: int = 204, tokens: list[int] | None = None) -> FoundryActions:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text="x" * 1000)

    def token_source() -> tuple[str, float]:
        if tokens is not None:
            tokens.append(1)
        return "token-1", time.time() + 3600

    return FoundryActions(ENDPOINT + "/", token_source=token_source, transport=httpx.MockTransport(handler))


async def test_stop_posts_to_the_session_as_the_running_identity() -> None:
    seen: list[httpx.Request] = []
    status, text = await actions(seen).stop("tfd-worker-abc", SESSION)
    assert status == 204
    assert len(text) == 300  # only the start of a body is kept
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == f"/api/projects/demo/agents/tfd-worker-abc/endpoint/sessions/{SESSION}:stop"
    assert dict(request.url.params) == {"api-version": "v1"}
    assert request.headers["authorization"] == "Bearer token-1"
    assert request.content == b""


async def test_crash_worker_invokes_the_existing_session() -> None:
    seen: list[httpx.Request] = []
    status, _ = await actions(seen, status=202).crash_worker("tfd-worker-abc", SESSION)
    assert status == 202
    request = seen[0]
    assert request.url.path == "/api/projects/demo/agents/tfd-worker-abc/endpoint/protocols/invocations"
    assert dict(request.url.params) == {"api-version": "v1", "agent_session_id": SESSION}
    assert json.loads(request.content) == {"action": "crash_worker"}


async def test_the_token_is_fetched_once_while_it_is_fresh() -> None:
    seen: list[httpx.Request] = []
    tokens: list[int] = []
    foundry = actions(seen, tokens=tokens)
    await foundry.stop("tfd-worker-abc", SESSION)
    await foundry.crash_worker("tfd-worker-abc", SESSION)
    assert len(seen) == 2
    assert len(tokens) == 1


def test_a_plain_http_endpoint_is_refused() -> None:
    with pytest.raises(ValueError):
        FoundryActions("http://example.test/api/projects/demo")
