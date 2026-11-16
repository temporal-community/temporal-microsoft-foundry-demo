"""The booth console: a page and the small API behind it.

    uvicorn app.main:app --host 127.0.0.1 --port 8080

See docs/contract.md for the API and README.md for configuration.
"""

from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from temporalio.service import RPCError

from . import auth
from .config import Config
from .foundry import FoundryActions
from .gateway import RunClosed, RunData, RunNotFound, TemporalGateway
from .history import build_view, iso, with_iso_times
from .sandboxes import SandboxRegistry

logger = logging.getLogger("tfd.console")
STATIC_DIR = Path(__file__).parent / "static"
FOLLOW_UP_CHOICES = (0, 60, 120)
FOLLOW_UP_DEFAULT = 60
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class LoginBody(BaseModel):
    code: str = Field(max_length=200)


class StartBody(BaseModel):
    question: str = Field(max_length=4000)
    follow_up_after_seconds: int = 0


class DecisionBody(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=300)
    subquestions: list[str] | None = Field(default=None, max_length=8)


def fail(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def run_view(config: Config, data: RunData, registry: SandboxRegistry) -> dict[str, Any]:
    """Everything the page needs about one run, in one object."""
    now = time.time()
    view = build_view(
        data.events,
        data.pending_activities,
        data.pending_workflow_task,
        now=now if data.status == "RUNNING" else None,
        boot_times=registry.boot_times(),
    )
    memo = data.memo
    request = data.events[0].get("input") if data.events else None
    request = request if isinstance(request, dict) else {}
    question = memo.get("question")
    if not isinstance(question, str):  # before the first workflow task: read the input
        question = request.get("question") if isinstance(request.get("question"), str) else ""
    follow_up_after = request.get("follow_up_after_seconds")
    phase = memo.get("phase") if isinstance(memo.get("phase"), str) else "starting"
    closed = next((item for item in view["timeline"] if item["type"] == "closed"), None)
    failure = None
    if data.status not in ("RUNNING", "COMPLETED"):
        failure = (closed or {}).get("message") or data.status.replace("_", " ").lower()
    ui_url = None
    if config.temporal_ui_url:
        ui_url = (
            f"{config.temporal_ui_url}/namespaces/{quote(config.temporal_namespace)}"
            f"/workflows/{quote(data.workflow_id)}"
        )
    research = memo.get("research") if isinstance(memo.get("research"), list) else []
    # The decision as it was sent, from the Signal in history. The memo only has it once a
    # worker has processed the Signal, and until then every browser should see it as sent.
    sent = next(
        (
            event["input"]
            for event in data.events
            if event["type"] == "workflow_execution_signaled"
            and event.get("name") == "decide"
            and isinstance(event.get("input"), dict)
        ),
        None,
    )
    decision_sent = None
    if sent is not None:
        questions = sent.get("subquestions")
        decision_sent = {
            "approved": bool(sent.get("approved")),
            "note": str(sent.get("note") or "")[:300],
            "subquestions": [str(item)[:200] for item in questions][:8] if isinstance(questions, list) else None,
        }
    return {
        "id": data.workflow_id,
        "run_id": data.run_id,
        "status": data.status,
        "phase": phase,
        "started_at": iso(data.start_time),
        "closed_at": iso(data.close_time),
        "question": question,
        "plan": memo.get("plan") if isinstance(memo.get("plan"), dict) else None,
        "decision": memo.get("decision") if isinstance(memo.get("decision"), dict) else None,
        "decision_sent": decision_sent,
        "research": research,
        "report": memo.get("report") if isinstance(memo.get("report"), str) else "",
        "follow_up": memo.get("follow_up") if isinstance(memo.get("follow_up"), str) else "",
        "follow_up_due": memo.get("follow_up_due") if isinstance(memo.get("follow_up_due"), str) else None,
        "follow_up_after_seconds": follow_up_after if isinstance(follow_up_after, int) else 0,
        "failure": failure,
        "temporal_ui_url": ui_url,
        "server_time": iso(now),
        **with_iso_times(view),
    }


def create_app(
    config: Config | None = None,
    gateway: Any | None = None,
    registry: SandboxRegistry | None = None,
) -> FastAPI:
    config = config or Config.from_env()
    if registry is None:
        actions = FoundryActions(config.foundry_project_endpoint) if config.foundry_stop else None
        registry = SandboxRegistry(config.state_dir / "sandboxes.jsonl", actions)
    gateway = gateway or TemporalGateway(config)
    wrong_codes = auth.RateLimiter(limit=5, window=60)  # per client address
    start_limit = auth.RateLimiter(limit=config.starts_per_minute, window=60)
    all_starts_limit = auth.RateLimiter(limit=max(config.starts_per_minute * 6, 12), window=60)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await registry.drain()

    app = FastAPI(title="tfd-console", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.config, app.state.gateway, app.state.registry = config, gateway, registry

    # -- plumbing -------------------------------------------------------------------------

    @app.middleware("http")
    async def harden(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            # The cookie is SameSite=Strict; this also refuses a cross-site form or script.
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path == "/" or request.url.path.startswith(("/static", "/api")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException):
        return JSONResponse({"error": str(error.detail)}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_request: Request, _error: RequestValidationError):
        return JSONResponse({"error": "the request is not valid"}, status_code=400)

    last_logged: dict[str, float] = {}

    def logged_lately(key: str, every: float = 10.0) -> bool:
        # The page asks about once a second; one line per problem every few seconds is enough.
        now = time.monotonic()
        if now - last_logged.get(key, float("-inf")) < every:
            return True
        last_logged[key] = now
        return False

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        # The message may hold an address or an ID; the page gets none of it.
        unreachable = isinstance(error, RPCError) or (
            isinstance(error, RuntimeError) and "client connect" in str(error)
        )
        if unreachable:
            if not logged_lately("temporal"):
                logger.warning("Temporal is not reachable: %s", str(error)[:300])
            return JSONResponse({"error": "the console could not reach Temporal"}, status_code=502)
        if not logged_lately(f"{request.method} {request.url.path}"):
            logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=error)
        return JSONResponse({"error": "the console hit an error; see its log"}, status_code=500)

    def client_address(request: Request) -> str:
        # Behind the proxy, the last entry is the address the proxy itself saw; earlier
        # entries are whatever the caller chose to send.
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",")[-1].strip() or (request.client.host if request.client else "?")

    def session(request: Request) -> str:
        session_id = auth.read_cookie(config.session_secret, request.cookies.get(auth.COOKIE_NAME))
        if session_id is None:
            raise fail(401, "enter the access code")
        return session_id

    def internal(request: Request) -> None:
        given = request.headers.get("authorization", "")
        expected = f"Bearer {config.internal_token}"
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise fail(401, "unauthorized")

    # -- page -----------------------------------------------------------------------------

    @app.get("/")
    async def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # -- login ----------------------------------------------------------------------------

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, bool]:
        # The access code is a short phrase, so guessing has to be slow: five wrong codes
        # from one address and that address waits out the minute. Right codes do not count,
        # because every laptop at the booth shares the conference network's address.
        address = client_address(request)
        if wrong_codes.full(address):
            raise fail(429, "too many wrong codes; wait a minute")
        if not auth.codes_match(body.code.strip(), config.access_code):
            wrong_codes.allow(address)
            raise fail(401, "that is not the access code")
        response.set_cookie(
            auth.COOKIE_NAME,
            auth.make_cookie(config.session_secret),
            max_age=auth.SESSION_SECONDS,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
            path="/",
        )
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/session")
    async def session_check(_session: str = Depends(session)) -> dict[str, bool]:
        """200 with a valid login cookie, 401 without. The proxy asks this before it lets a
        browser through to the Temporal UI."""
        return {"ok": True}

    @app.get("/api/config")
    async def configuration(_session: str = Depends(session)) -> dict[str, Any]:
        choices = [seconds for seconds in FOLLOW_UP_CHOICES if seconds <= config.max_follow_up_seconds]
        return {
            "agent_name": config.agent_name,
            "model_name": config.model_name,
            "region": config.region,
            "temporal_namespace": config.temporal_namespace,
            "task_queue": config.task_queue,
            "temporal_ui_url": config.temporal_ui_url or None,
            "presets": list(config.presets),
            # Stopping a whole sandbox recovers slowly (its connections are not closed, so
            # Temporal keeps offering work to it for a while). Off unless asked for.
            "show_stop_button": config.show_stop_button,
            "follow_up_choices": choices,
            "follow_up_default": FOLLOW_UP_DEFAULT if FOLLOW_UP_DEFAULT in choices else choices[0],
            "limits": {
                "max_question_length": config.max_question_length,
                "max_follow_up_seconds": config.max_follow_up_seconds,
                "max_open_runs": config.max_open_runs,
            },
        }

    # -- runs -----------------------------------------------------------------------------

    @app.post("/api/runs")
    async def start_run(body: StartBody, session_id: str = Depends(session)) -> dict[str, str]:
        question = " ".join(body.question.split())
        if len(question) < 8:
            raise fail(400, "ask a question of at least a few words")
        if len(question) > config.max_question_length:
            raise fail(400, f"keep the question under {config.max_question_length} characters")
        follow_up = body.follow_up_after_seconds
        if follow_up < 0 or follow_up > config.max_follow_up_seconds:
            raise fail(400, "that follow-up delay is not allowed")
        if not start_limit.allow(session_id) or not all_starts_limit.allow("all"):
            raise fail(429, "too many runs started; wait a minute")
        if await gateway.busy_runs() >= config.max_open_runs:
            raise fail(429, "the demo is busy; try again in a minute")
        workflow_id = await gateway.start(question, follow_up)
        logger.info("started %s follow_up=%s", workflow_id, follow_up)
        return {"id": workflow_id}

    @app.get("/api/runs")
    async def recent_runs(_session: str = Depends(session)) -> dict[str, Any]:
        return {"runs": with_iso_times(await gateway.recent(20))}

    @app.get("/api/runs/{workflow_id}")
    async def get_run(workflow_id: str, _session: str = Depends(session)) -> dict[str, Any]:
        try:
            data = await gateway.load(workflow_id)
        except RunNotFound:
            raise fail(404, "there is no such run") from None
        return run_view(config, data, registry)

    @app.post("/api/runs/{workflow_id}/decision")
    async def decide(workflow_id: str, body: DecisionBody, _session: str = Depends(session)) -> dict[str, bool]:
        decision: dict[str, Any] = {"approved": body.approved, "note": " ".join(body.note.split())}
        if body.subquestions is not None:
            cleaned = [" ".join(item.split())[:200] for item in body.subquestions if item and item.strip()]
            if cleaned:
                decision["subquestions"] = cleaned
        try:
            await gateway.decide(workflow_id, decision)
        except RunNotFound:
            raise fail(404, "there is no such run") from None
        except RunClosed:
            raise fail(409, "that run has already finished") from None
        logger.info("decision %s approved=%s", workflow_id, body.approved)
        return {"ok": True}

    # -- sandboxes ------------------------------------------------------------------------

    @app.get("/api/sandboxes")
    async def sandboxes(_session: str = Depends(session)) -> dict[str, Any]:
        return registry.snapshot()

    @app.post("/api/sandboxes/{session_id}/stop", status_code=202)
    async def stop_sandbox(session_id: str, _session: str = Depends(session)) -> dict[str, Any]:
        sandbox = registry.request_stop(session_id, "presenter")
        if sandbox is None:
            raise fail(404, "there is no such sandbox")
        return sandbox

    @app.post("/api/sandboxes/{session_id}/crash-worker", status_code=202)
    async def crash_worker(session_id: str, _session: str = Depends(session)) -> dict[str, Any]:
        sandbox = registry.request_crash(session_id)
        if sandbox is None:
            raise fail(404, "there is no such sandbox")
        return sandbox

    @app.post("/api/internal/sandbox-events")
    async def sandbox_event(request: Request, _auth: None = Depends(internal)) -> dict[str, bool]:
        try:
            body = await request.json()
        except ValueError:
            raise fail(400, "the body must be JSON") from None
        problem = SandboxRegistry.validate(body)
        if problem:
            raise fail(400, problem)
        await registry.record(body)
        return {"ok": True}

    return app


def _app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    return create_app()


def __getattr__(name: str) -> Any:
    # ``uvicorn app.main:app`` builds the app on first use; importing this module in tests
    # does not need a configured environment.
    if name == "app":
        application = _app()
        globals()["app"] = application
        return application
    raise AttributeError(name)
