"""Chat client factories for the plugin: a Foundry model deployment, or an offline stub.

Factories run on the worker, on first use; Workflows only carry the name ``MODEL_NAME``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .settings import FOUNDRY_SCOPE, MODEL_NAME, Settings


def chat_client_factory(settings: Settings) -> Callable[[], Any]:
    if settings.model == "stub":
        return _stub_factory()
    return _foundry_factory(settings)


def _foundry_factory(settings: Settings) -> Callable[[], Any]:
    if not settings.project_endpoint:
        raise ValueError("FOUNDRY_PROJECT_ENDPOINT is required for a Foundry model.")

    def build() -> Any:
        # Imported here: the provider stack is only needed in the process that calls the model.
        from agent_framework.openai import OpenAIChatClient
        from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
        from openai import AsyncOpenAI

        # Inside Foundry this resolves to the hosted agent's own identity; no key is stored.
        token_provider = get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_SCOPE)
        return OpenAIChatClient(
            model=MODEL_NAME,
            async_client=AsyncOpenAI(
                base_url=f"{settings.project_endpoint}/openai/v1/",
                api_key=token_provider,
                max_retries=0,  # Temporal owns retries
                timeout=120.0,
            ),
        )

    return build


def _stub_reply(prompt: str, structured: bool) -> str:
    """An offline answer shaped like the real agents' answers, derived from the prompt alone."""
    import json

    first = prompt.splitlines()[0][:90] if prompt else ""
    if structured:  # the planner
        topic = first.removeprefix("Question: ")
        return json.dumps(
            {
                "summary": f"Stub plan: split '{topic}' into what exists, what it costs and what to do.",
                "subquestions": [
                    f"What is the current state of: {topic}",
                    f"What are the costs and limits of: {topic}",
                    f"What should a team do next about: {topic}",
                ],
            }
        )
    if prompt.startswith("Overall question"):  # a researcher
        return (
            "- Stub finding one, dated 2026-11-01 ([example.com](https://example.com/one))\n"
            "- Stub finding two, which disagrees slightly ([example.org](https://example.org/two))"
        )
    if prompt.startswith("Brief:"):  # the follow-up
        return "**Update (stub).** Nothing material changed. Next: re-run the search next week."
    return (  # the writer
        f"# Stub brief\n\nShort answer first. This brief was produced offline for: {first}\n\n"
        "## Evidence\n\nSee [example.com](https://example.com/one).\n\n## Open questions\n\n- One open question"
    )


def _stub_factory() -> Callable[[], Any]:
    from agent_framework import Message
    from temporalio.microsoft_agent_framework.testing import FakeChatClient

    class StubChatClient(FakeChatClient):
        """Answers from the prompt alone, so a fresh worker process gives the same reply."""

        def __init__(self) -> None:
            super().__init__(["stub"])

        def _inner_get_response(
            self,
            *,
            messages: Sequence[Message],
            stream: bool,
            options: Mapping[str, Any],
            **kwargs: Any,
        ) -> Any:
            last = next((m.text for m in reversed(messages) if m.role == "user" and m.text), "")
            self._responses = [_stub_reply(last, bool(options.get("response_format")))]
            self._cursor = 0
            return super()._inner_get_response(
                messages=messages, stream=stream, options=options, **kwargs
            )

    client = StubChatClient()
    return lambda: client
