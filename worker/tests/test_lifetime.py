"""When the worker stops polling: deadline after the last inbound request, moved by keep-alives."""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from tfd_worker import worker_main
from tfd_worker.settings import Settings
from tfd_worker.worker_main import ActivityTracker, poll_until_done


def settings(**overrides: object) -> Settings:
    base = Settings.from_env(
        {
            "TEMPORAL_DEPLOYMENT_NAME": "tfd",
            "TEMPORAL_BUILD_ID": "test",
            "TFD_SESSION_IDLE_TIMEOUT_SECONDS": "3",
            "TFD_DEADLINE_MARGIN_SECONDS": "2",  # budget: 1 s after the last inbound request
            "TFD_BUSY_LINGER_SECONDS": "0",
        }
    )
    return dataclasses.replace(base, **overrides)


@pytest.fixture(autouse=True)
def fast_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_main, "LIFETIME_TICK_SECONDS", 0.05)


def pings(monkeypatch: pytest.MonkeyPatch, status_code: int) -> list[dict]:
    seen: list[dict] = []

    async def fake_ping(_settings: Settings, body: dict | None = None) -> dict:
        seen.append(body or {})
        return {"status_code": status_code}

    monkeypatch.setattr(worker_main, "ping_own_session", fake_ping)
    return seen


async def test_idle_worker_stops_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = pings(monkeypatch, 202)
    started = time.monotonic()
    reason = await poll_until_done(settings(keepalive_seconds=1), ActivityTracker(), asyncio.Event())
    assert reason == "deadline"
    assert 1.0 <= time.monotonic() - started < 1.5
    assert seen == []  # an idle worker never keeps the sandbox alive


async def test_busy_worker_keeps_the_session_alive_then_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = pings(monkeypatch, 202)
    monkeypatch.setattr(worker_main, "event", lambda *args, **kwargs: None)
    tracker = ActivityTracker()
    tracker.inflight = 1

    async def finish_work() -> None:
        await asyncio.sleep(2.5)  # well past the 1 s budget
        tracker.inflight = 0
        tracker.last_change = time.monotonic()

    started = time.monotonic()
    work = asyncio.create_task(finish_work())
    # A 2 s budget, kept alive every 1 s.
    config = dataclasses.replace(settings(), session_idle_timeout_seconds=4, keepalive_seconds=1)
    reason = await poll_until_done(config, tracker, asyncio.Event())
    await work
    elapsed = time.monotonic() - started
    assert reason == "deadline"
    assert elapsed > 3.5  # outlived the 2 s budget because it was busy
    assert len(seen) >= 2
    assert seen[0] == {"deploymentName": "tfd", "buildId": "test"}


async def test_failed_keepalive_does_not_move_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = pings(monkeypatch, 403)
    monkeypatch.setattr(worker_main, "event", lambda *args, **kwargs: None)
    tracker = ActivityTracker()
    tracker.inflight = 1
    started = time.monotonic()
    config = dataclasses.replace(settings(), session_idle_timeout_seconds=4, keepalive_seconds=1)
    reason = await poll_until_done(config, tracker, asyncio.Event())
    assert reason == "deadline"
    assert 2.0 <= time.monotonic() - started < 2.6  # the 2 s budget, not extended
    assert seen


async def test_stop_request_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    pings(monkeypatch, 202)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)
    assert await poll_until_done(settings(), ActivityTracker(), stop) == "stop"


async def test_quiet_exit_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    pings(monkeypatch, 202)
    config = dataclasses.replace(settings(), session_idle_timeout_seconds=0, idle_exit_seconds=1)
    started = time.monotonic()
    assert await poll_until_done(config, ActivityTracker(), asyncio.Event()) == "quiet"
    assert 1.0 <= time.monotonic() - started < 1.5


def test_settings_require_a_version() -> None:
    with pytest.raises(ValueError, match="TEMPORAL_BUILD_ID"):
        Settings.from_env({"TEMPORAL_DEPLOYMENT_NAME": "tfd"})


def test_version_mismatch_is_named() -> None:
    config = settings()
    assert config.version_mismatch("tfd", "test") is None
    assert config.version_mismatch(None, None) is None
    assert "build" in (config.version_mismatch("tfd", "other") or "")
    assert "deployment" in (config.version_mismatch("else", "test") or "")


def test_unversioned_worker_needs_no_deployment() -> None:
    config = Settings.from_env({"TFD_WORKER_VERSIONING": "0"})
    assert not config.versioning
    assert config.version_mismatch("anything", "at-all") is None


def test_brief_without_links_gets_its_sources_listed() -> None:
    from tfd_worker.workflows import _with_sources

    findings = [
        "- A fact ([Learn](https://learn.example/a))\n- Another ([Docs](https://docs.example/b))",
        "- Same source again ([Learn](https://learn.example/a))",
    ]
    bare = "# Title\n\nAnswer with no links."
    out = _with_sources(bare, findings)
    assert out.startswith(bare)
    assert out.count("https://learn.example/a") == 1
    assert "## Sources" in out and "[Docs](https://docs.example/b)" in out
    cited = "See [a](https://x.example/1), [b](https://x.example/2) and [c](https://x.example/3)."
    assert _with_sources(cited, findings) == cited
    assert _with_sources(bare, ["no links here"]) == bare
