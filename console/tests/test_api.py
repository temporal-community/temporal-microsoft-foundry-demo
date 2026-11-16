"""The HTTP API with a fake Temporal behind it."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.gateway import RunClosed, RunData, RunNotFound
from app.main import create_app
from app.sandboxes import SandboxRegistry

S1 = "foundry/tfd-worker-abc/v1/aaaaaaaaaaaa/pid10"
CODE = "booth-code"
TOKEN = "internal-token"


class FakeGateway:
    def __init__(self) -> None:
        self.started: list[tuple[str, int]] = []
        self.decisions: list[tuple[str, dict[str, Any]]] = []
        self.busy = 0
        self.status = "RUNNING"

    async def start(self, question: str, follow_up_after_seconds: int) -> str:
        self.started.append((question, follow_up_after_seconds))
        return "research-0a1b2c3d"

    async def decide(self, workflow_id: str, decision: dict[str, Any]) -> None:
        if workflow_id == "research-closed":
            raise RunClosed(workflow_id)
        if workflow_id != "research-0a1b2c3d":
            raise RunNotFound(workflow_id)
        self.decisions.append((workflow_id, decision))

    async def load(self, workflow_id: str) -> RunData:
        if workflow_id != "research-0a1b2c3d":
            raise RunNotFound(workflow_id)
        now = time.time()
        events = [
            {"id": 1, "type": "workflow_execution_started", "time": now - 30, "input": {"question": "why"}},
            {"id": 2, "type": "workflow_task_scheduled", "time": now - 30},
            {"id": 3, "type": "workflow_task_started", "time": now - 25, "scheduled_event_id": 2, "identity": S1},
            {"id": 4, "type": "workflow_task_completed", "time": now - 25},
            {
                "id": 5,
                "type": "activity_task_scheduled",
                "time": now - 25,
                "activity_id": "1",
                "activity_type": "microsoft_agent_framework.invoke_model",
                "summary": "planner",
            },
        ]
        pending = [
            {
                "activity_id": "1",
                "state": "started",
                "attempt": 1,
                "last_started_time": now - 24.9,
                "identity": S1,
                "message": "",
            }
        ]
        return RunData(
            workflow_id=workflow_id,
            run_id="run-1",
            status=self.status,
            start_time=now - 30,
            close_time=None,
            memo={"phase": "planning", "question": "why does it work", "research": "not a list"},
            events=events,
            pending_activities=pending,
            pending_workflow_task=None,
            loaded_at=now,
        )

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "id": "research-0a1b2c3d",
                "status": "RUNNING",
                "phase": "planning",
                "question": "why does it work",
                "started_at": 1000.0,
                "closed_at": None,
            }
        ]

    async def busy_runs(self) -> int:
        return self.busy


def config(tmp_path: Path, **overrides: Any) -> Config:
    environ = {
        "TFD_ACCESS_CODE": CODE,
        "TFD_INTERNAL_TOKEN": TOKEN,
        "TFD_SESSION_SECRET": "s",
        "TFD_COOKIE_SECURE": "0",
        "TFD_STATE_DIR": str(tmp_path),
        "TFD_AGENT_NAME": "tfd-worker-abc",
        "TFD_MODEL_NAME": "gpt-5-mini",
        "TFD_REGION": "East US 2",
        "TFD_TEMPORAL_UI_URL": "/temporal-ui",
        "TFD_MAX_OPEN_RUNS": "2",
        "TFD_STARTS_PER_MINUTE": "3",
    }
    environ.update({key: str(value) for key, value in overrides.items()})
    return Config.from_env(environ)


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def client(tmp_path: Path, gateway: FakeGateway) -> TestClient:
    app = create_app(config(tmp_path), gateway, SandboxRegistry(tmp_path / "sandboxes.jsonl"))
    return TestClient(app, base_url="http://testserver")


def login(client: TestClient) -> None:
    assert client.post("/api/login", json={"code": CODE}).status_code == 200


def test_everything_but_the_page_needs_the_access_code(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}
    for path in ("/api/config", "/api/runs", "/api/runs/research-0a1b2c3d", "/api/sandboxes", "/api/session"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json() == {"error": "enter the access code"}
    assert client.post("/api/runs", json={"question": "what is durable execution"}).status_code == 401
    assert client.post("/api/sandboxes/abcdef123456/stop").status_code == 401
    assert client.post("/api/sandboxes/abcdef123456/crash-worker").status_code == 401


def test_login_sets_a_signed_http_only_cookie(client: TestClient) -> None:
    wrong = client.post("/api/login", json={"code": "nope"})
    assert wrong.status_code == 401 and "set-cookie" not in wrong.headers
    right = client.post("/api/login", json={"code": f" {CODE} "})
    cookie = right.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert CODE not in cookie
    assert client.get("/api/session").json() == {"ok": True}
    client.cookies.set("tfd_session", "v1.forged.9999999999.00")
    assert client.get("/api/session").status_code == 401


def test_five_wrong_codes_lock_an_address_out_for_a_minute(client: TestClient) -> None:
    for _ in range(5):
        assert client.post("/api/login", json={"code": "guess"}).status_code == 401
    locked = client.post("/api/login", json={"code": CODE})  # even the right code waits
    assert locked.status_code == 429 and "wait" in locked.json()["error"]
    elsewhere = {"X-Forwarded-For": "203.0.113.9"}
    assert client.post("/api/login", json={"code": CODE}, headers=elsewhere).status_code == 200


def test_right_codes_are_not_counted(client: TestClient) -> None:
    for _ in range(12):  # a booth of laptops behind one address
        assert client.post("/api/login", json={"code": CODE}).status_code == 200


def test_config_has_names_presets_and_no_secrets(client: TestClient) -> None:
    login(client)
    body = client.get("/api/config").json()
    assert body["agent_name"] == "tfd-worker-abc"
    assert body["model_name"] == "gpt-5-mini"
    assert body["temporal_ui_url"] == "/temporal-ui"
    assert body["follow_up_choices"] == [0, 60, 120] and body["follow_up_default"] == 60
    assert len(body["presets"]) >= 3
    assert CODE not in str(body) and TOKEN not in str(body)


def test_start_validates_limits_and_caps(client: TestClient, gateway: FakeGateway) -> None:
    login(client)
    assert client.post("/api/runs", json={"question": "  why  "}).status_code == 400
    assert client.post("/api/runs", json={"question": "x" * 301}).status_code == 400
    assert (
        client.post(
            "/api/runs", json={"question": "what is durable execution", "follow_up_after_seconds": 601}
        ).status_code
        == 400
    )
    assert client.post("/api/runs", json={"question": 7}).status_code == 400
    started = client.post("/api/runs", json={"question": "what   is durable\nexecution", "follow_up_after_seconds": 60})
    assert started.json() == {"id": "research-0a1b2c3d"}
    assert gateway.started == [("what is durable execution", 60)]
    gateway.busy = 2
    busy = client.post("/api/runs", json={"question": "what is durable execution"})
    assert busy.status_code == 429 and "busy" in busy.json()["error"]
    gateway.busy = 0
    assert client.post("/api/runs", json={"question": "what is durable execution"}).status_code == 200
    limited = client.post("/api/runs", json={"question": "what is durable execution"})  # fourth in a minute
    assert limited.status_code == 429 and "wait" in limited.json()["error"]


def test_run_view_has_everything_in_one_object(client: TestClient) -> None:
    login(client)
    body = client.get("/api/runs/research-0a1b2c3d").json()
    assert body["status"] == "RUNNING" and body["phase"] == "planning"
    assert body["question"] == "why does it work"
    assert body["research"] == [] and body["plan"] is None  # a malformed memo value is not passed on
    assert body["temporal_ui_url"] == "/temporal-ui/namespaces/default/workflows/research-0a1b2c3d"
    assert [item["type"] for item in body["timeline"]] == ["started", "wake", "agent"]
    assert body["wakes"][0]["latency_seconds"] == 5.0 and body["wakes"][0]["cause"] == "start"
    agent = body["timeline"][2]
    assert (agent["agent"], agent["status"], agent["sandbox"]) == ("planner", "running", "aaaaaaaaaaaa")
    assert body["sandboxes"][0]["id"] == "aaaaaaaaaaaa"
    assert body["wakes"][0]["at"].endswith("Z")
    assert body["decision"] is None and body["decision_sent"] is None
    assert body["follow_up_after_seconds"] == 0
    assert client.get("/api/runs/research-ffffffff").status_code == 404
    assert client.get("/api/runs/temporal-sys-something").status_code == 404
    assert client.get("/api/runs").json()["runs"][0]["started_at"] == "1970-01-01T00:16:40.000Z"


def test_decision_is_sent_as_the_signal(client: TestClient, gateway: FakeGateway) -> None:
    login(client)
    response = client.post(
        "/api/runs/research-0a1b2c3d/decision",
        json={"approved": True, "note": " looks  right ", "subquestions": [" one ", "", "two"]},
    )
    assert response.json() == {"ok": True}
    assert gateway.decisions == [
        ("research-0a1b2c3d", {"approved": True, "note": "looks right", "subquestions": ["one", "two"]})
    ]
    client.post("/api/runs/research-0a1b2c3d/decision", json={"approved": False})
    assert gateway.decisions[-1][1] == {"approved": False, "note": ""}
    assert client.post("/api/runs/research-closed/decision", json={"approved": True}).status_code == 409
    assert client.post("/api/runs/research-missing/decision", json={"approved": True}).status_code == 404
    assert client.post("/api/runs/research-0a1b2c3d/decision", json={"approved": "maybe"}).status_code == 400


def test_sandbox_events_need_the_bearer_and_feed_the_registry(client: TestClient) -> None:
    session = "0ca65fab16ecf6ee008GeujLesj0vCxYIr82r8MfOLg0eYdhCY"
    event = {
        "session": session,
        "agent": "tfd-worker-abc",
        "agent_version": "1",
        "build_id": "abc",
        "event": "boot",
        "at": 1.0,
        "detail": {},
    }
    assert client.post("/api/internal/sandbox-events", json=event).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post("/api/internal/sandbox-events", json=event, headers=wrong).status_code == 401
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/api/internal/sandbox-events", json=event, headers=bearer).json() == {"ok": True}
    assert (
        client.post("/api/internal/sandbox-events", json={**event, "event": "nope"}, headers=bearer).status_code == 400
    )
    assert client.post("/api/internal/sandbox-events", content=b"not json", headers=bearer).status_code == 400
    login(client)
    assert client.post("/api/internal/sandbox-events", json=event).status_code == 401  # a login is not the bearer
    snapshot = client.get("/api/sandboxes").json()
    assert snapshot["up"] == 1 and snapshot["sandboxes"][0]["state"] == "starting"
    stopped = client.post(f"/api/sandboxes/{session[:12]}/stop")
    assert stopped.status_code == 202 and stopped.json()["state"] in ("releasing", "released")
    assert client.post(f"/api/sandboxes/{session}/crash-worker").status_code == 202
    assert client.post("/api/sandboxes/000000000000/stop").status_code == 404
    assert TOKEN not in client.get("/api/sandboxes").text


def test_cross_site_posts_are_refused_and_headers_are_set(client: TestClient) -> None:
    refused = client.post("/api/login", json={"code": CODE}, headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403
    same = client.post("/api/login", json={"code": CODE}, headers={"Origin": "http://testserver"})
    assert same.status_code == 200
    page = client.get("/")
    assert page.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"


def test_refuses_to_start_without_an_access_code() -> None:
    with pytest.raises(ValueError, match="TFD_ACCESS_CODE"):
        Config.from_env({"TFD_INTERNAL_TOKEN": "x"})


def test_temporal_being_down_is_a_502_and_one_log_line(
    tmp_path: Path, gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    async def unreachable(limit: int = 20) -> list[dict[str, Any]]:
        raise RuntimeError("Failed client connect: Server connection error: 127.0.0.1:7233 refused")

    gateway.recent = unreachable  # type: ignore[method-assign]
    app = create_app(config(tmp_path), gateway, SandboxRegistry(tmp_path / "sandboxes.jsonl"))
    client = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
    login(client)
    with caplog.at_level("WARNING", logger="tfd.console"):
        for _ in range(5):  # the page asks about once a second
            response = client.get("/api/runs")
            assert response.status_code == 502
            assert response.json() == {"error": "the console could not reach Temporal"}
    assert len([record for record in caplog.records if "not reachable" in record.getMessage()]) == 1
    assert client.get("/api/sandboxes").status_code == 200  # what the console knows itself still works


def test_a_fault_in_the_console_is_a_500_that_says_nothing_about_it(
    tmp_path: Path, gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(limit: int = 20) -> list[dict[str, Any]]:
        raise KeyError("secret-detail")

    gateway.recent = broken  # type: ignore[method-assign]
    app = create_app(config(tmp_path), gateway, SandboxRegistry(tmp_path / "sandboxes.jsonl"))
    client = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
    login(client)
    with caplog.at_level("ERROR", logger="tfd.console"):
        response = client.get("/api/runs")
    assert response.status_code == 500
    assert "secret-detail" not in response.text
    assert any("unhandled error on GET /api/runs" in record.getMessage() for record in caplog.records)
