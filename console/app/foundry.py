"""What the console does to a Foundry session, as the identity this process runs as.

On the demo VM that is the VM's managed identity, the same one the Temporal worker
controller uses to wake sandboxes, so it owns every session it acts on.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

SCOPE = "https://ai.azure.com/.default"

# Returns a bearer token for SCOPE and the epoch second it expires.
TokenSource = Callable[[], tuple[str, float]]


def _azure_token_source() -> TokenSource:
    # Imported here so a local run without Azure never loads the credential chain.
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()

    def fetch() -> tuple[str, float]:
        token = credential.get_token(SCOPE)
        return token.token, float(token.expires_on)

    return fetch


class FoundryActions:
    def __init__(
        self,
        project_endpoint: str,
        *,
        token_source: TokenSource | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not project_endpoint.startswith("https://"):
            raise ValueError("FOUNDRY_PROJECT_ENDPOINT must be an https URL to act on sessions")
        self._endpoint = project_endpoint.rstrip("/")
        self._token_source = token_source
        self._transport = transport
        self._token: tuple[str, float] | None = None

    def _fresh_token(self) -> str:
        if self._token_source is None:
            self._token_source = _azure_token_source()
        if self._token is None or self._token[1] - time.time() < 300:
            self._token = self._token_source()
        return self._token[0]

    async def _post(self, url: str, params: dict[str, str], body: dict[str, Any] | None) -> tuple[int, str]:
        token = await asyncio.to_thread(self._fresh_token)
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as http:
            response = await http.post(url, params=params, headers={"Authorization": f"Bearer {token}"}, json=body)
        return response.status_code, response.text[:300]

    async def stop(self, agent: str, session: str) -> tuple[int, str]:
        """Stop the session's compute. Measured: a hard stop that takes about 5 s to return."""
        return await self._post(
            f"{self._endpoint}/agents/{agent}/endpoint/sessions/{session}:stop",
            {"api-version": "v1"},
            None,
        )

    async def crash_worker(self, agent: str, session: str) -> tuple[int, str]:
        """Ask the sandbox to kill its worker process mid-task; the sandbox itself survives."""
        return await self._post(
            f"{self._endpoint}/agents/{agent}/endpoint/protocols/invocations",
            {"api-version": "v1", "agent_session_id": session},
            {"action": "crash_worker"},
        )
