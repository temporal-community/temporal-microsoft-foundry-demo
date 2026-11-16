"""The sandbox registry: states, release on idle, presenter actions, persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.sandboxes import SandboxRegistry

SESSION = "0ca65fab16ecf6ee008GeujLesj0vCxYIr82r8MfOLg0eYdhCY"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeFoundry:
    def __init__(self, status: int = 204, delay: float = 0.0, fail: Exception | None = None) -> None:
        self.status, self.delay, self.fail = status, delay, fail
        self.stops: list[tuple[str, str]] = []
        self.crashes: list[tuple[str, str]] = []

    async def stop(self, agent: str, session: str) -> tuple[int, str]:
        self.stops.append((agent, session))
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return self.status, "" if self.status < 300 else "forbidden"

    async def crash_worker(self, agent: str, session: str) -> tuple[int, str]:
        self.crashes.append((agent, session))
        return 200, '{"status": "worker_killed", "pid": 133}'


def event(name: str, session: str = SESSION, **detail: object) -> dict:
    return {
        "session": session,
        "agent": "tfd-worker-abc",
        "agent_version": "1",
        "build_id": "abc",
        "event": name,
        "at": 1.0,
        "detail": detail,
    }


def state(registry: SandboxRegistry, session: str = SESSION) -> str:
    return next(box["state"] for box in registry.snapshot()["sandboxes"] if box["session"] == session)


async def test_states_follow_the_sandbox_through_its_life() -> None:
    clock = Clock()
    registry = SandboxRegistry(clock=clock)
    await registry.record(event("boot"))
    assert state(registry) == "starting"
    assert registry.snapshot()["up"] == 1
    clock.now += 2
    await registry.record(event("polling"))
    assert state(registry) == "idle"
    clock.now += 15
    await registry.record(event("alive", inflight=3))
    assert state(registry) == "working"
    await registry.record(event("keepalive", inflight=3))
    box = registry.snapshot()["sandboxes"][0]
    assert (box["keepalives"], box["inflight"], box["short"]) == (1, 3, SESSION[:12])
    await registry.record(event("alive", inflight=0))
    assert state(registry) == "idle"


async def test_idle_exit_releases_the_sandbox_through_foundry() -> None:
    clock = Clock()
    foundry = FakeFoundry(delay=0.05)
    registry = SandboxRegistry(actions=foundry, clock=clock)
    await registry.record(event("boot"))
    await registry.record(event("polling"))
    clock.now += 20
    await registry.record(event("idle_exit"))
    assert state(registry) == "releasing"  # the stop is in flight; the event call did not wait
    assert registry.snapshot()["up"] == 1
    await registry.drain()
    assert foundry.stops == [("tfd-worker-abc", SESSION)]
    box = registry.snapshot()["sandboxes"][0]
    assert box["state"] == "released"
    assert box["stop_reason"] == "idle"
    assert box["stop"]["ok"] is True and box["stop"]["status_code"] == 204
    assert box["lived_seconds"] == 20.0
    assert registry.snapshot()["up"] == 0


async def test_repeated_idle_exit_stops_once() -> None:
    foundry = FakeFoundry(delay=0.05)
    registry = SandboxRegistry(actions=foundry)
    await registry.record(event("boot"))
    await registry.record(event("idle_exit"))
    await registry.record(event("idle_exit"))
    await registry.drain()
    assert len(foundry.stops) == 1


async def test_failed_stop_is_recorded_and_the_sandbox_goes_silent() -> None:
    clock = Clock()
    registry = SandboxRegistry(actions=FakeFoundry(status=403), clock=clock)
    await registry.record(event("boot"))
    await registry.record(event("idle_exit"))
    await registry.drain()
    box = registry.snapshot()["sandboxes"][0]
    assert box["state"] == "releasing"
    assert (box["stop"]["ok"], box["stop"]["status_code"], box["stop"]["error"]) == (False, 403, "forbidden")
    clock.now += 46
    assert state(registry) == "gone"
    assert registry.snapshot()["up"] == 0


async def test_stop_error_text_is_kept_short_and_does_not_raise() -> None:
    registry = SandboxRegistry(actions=FakeFoundry(fail=RuntimeError("no route to host")))
    await registry.record(event("boot"))
    await registry.record(event("idle_exit"))
    await registry.drain()
    assert registry.snapshot()["sandboxes"][0]["stop"]["error"] == "RuntimeError: no route to host"


async def test_silence_means_gone() -> None:
    clock = Clock()
    registry = SandboxRegistry(clock=clock)
    await registry.record(event("boot"))
    await registry.record(event("polling"))
    clock.now += 44
    assert state(registry) == "idle"
    clock.now += 2
    assert state(registry) == "gone"
    box = registry.snapshot()["sandboxes"][0]
    assert box["lived_seconds"] == 0.0  # it was last heard from at the same instant it booted
    clock.now += 3600
    assert registry.snapshot()["sandboxes"] == []  # an hour later it leaves the list


async def test_sigterm_without_a_release_request_is_gone() -> None:
    registry = SandboxRegistry()
    await registry.record(event("boot"))
    await registry.record(event("polling"))
    await registry.record(event("stopping"))
    assert state(registry) == "gone"


async def test_presenter_stop_does_not_block_and_takes_a_prefix() -> None:
    foundry = FakeFoundry(delay=0.05)
    registry = SandboxRegistry(actions=foundry)
    await registry.record(event("boot"))
    await registry.record(event("polling"))
    await registry.record(event("alive", inflight=2))
    box = registry.request_stop(SESSION[:12])
    assert box is not None and box["state"] == "releasing"
    assert foundry.stops == []  # nothing awaited yet
    await registry.drain()
    box = registry.snapshot()["sandboxes"][0]
    assert (box["state"], box["stop_reason"]) == ("released", "presenter")
    assert registry.request_stop("nope-nope") is None
    assert registry.request_stop("../etc") is None


async def test_crash_worker_is_recorded_and_the_sandbox_stays_up() -> None:
    foundry = FakeFoundry()
    registry = SandboxRegistry(actions=foundry)
    await registry.record(event("boot"))
    await registry.record(event("polling"))
    assert registry.request_crash(SESSION) is not None
    await registry.drain()
    box = registry.snapshot()["sandboxes"][0]
    assert foundry.crashes == [("tfd-worker-abc", SESSION)]
    assert box["state"] == "idle"
    assert len(box["crashes"]) == 1 and box["crashes"][0]["ok"] is True


async def test_without_foundry_requests_are_only_recorded() -> None:
    registry = SandboxRegistry()
    await registry.record(event("boot"))
    await registry.record(event("idle_exit"))
    registry.request_crash(SESSION)
    await registry.drain()
    box = registry.snapshot()["sandboxes"][0]
    assert box["state"] == "released"
    assert box["stop"] == {"ok": True, "status_code": None, "seconds": 0.0, "error": None, "recorded_only": True}
    assert box["crashes"][0]["recorded_only"] is True


async def test_a_session_that_boots_again_starts_over() -> None:
    registry = SandboxRegistry()
    await registry.record(event("boot"))
    await registry.record(event("idle_exit"))
    await registry.drain()
    assert state(registry) == "released"
    await registry.record(event("boot"))
    assert state(registry) == "starting"


async def test_state_survives_a_restart(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "state" / "sandboxes.jsonl"
    registry = SandboxRegistry(path, FakeFoundry(), clock=clock)
    await registry.record(event("boot"))
    await registry.record(event("polling"))
    await registry.record(event("alive", inflight=1))  # not persisted
    other = "1" * 40
    await registry.record(event("boot", session=other))
    await registry.record(event("idle_exit", session=other))
    await registry.drain()
    clock.now += 5

    again = SandboxRegistry(path, FakeFoundry(), clock=clock)
    states = {box["session"]: box["state"] for box in again.snapshot()["sandboxes"]}
    assert states == {SESSION: "idle", other: "released"}
    assert again.boot_times() == {SESSION[:12]: 1000.0, other[:12]: 1000.0}
    assert "alive" not in path.read_text()


def test_validation_rejects_what_it_should() -> None:
    assert SandboxRegistry.validate(event("boot")) is None
    assert SandboxRegistry.validate([]) is not None
    assert SandboxRegistry.validate(event("explode")) is not None
    assert SandboxRegistry.validate(event("boot", session="a/b")) is not None
    assert SandboxRegistry.validate({**event("boot"), "agent": "bad name"}) is not None
    assert SandboxRegistry.validate({**event("boot"), "detail": 3}) is not None
