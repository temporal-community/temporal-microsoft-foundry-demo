"""Configuration read from the hosted agent's environment."""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass

# The name Workflows use for the model, and also the Foundry deployment name. They have to
# match: Agent Framework copies the chat client's ``model`` into the request options, so the
# name given to TemporalChatClient reaches the provider as the model id.
MODEL_NAME = "gpt-5-mini"
FOUNDRY_SCOPE = "https://ai.azure.com/.default"


def _int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    return int(raw) if raw else default


def _flag(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    # Temporal connection
    temporal_address: str
    temporal_namespace: str
    temporal_api_key: str | None
    temporal_tls: bool
    temporal_server_ca: bytes | None
    temporal_server_name: str | None
    task_queue: str
    # Worker Versioning ties the worker to a Worker Deployment Version, which is what Temporal's
    # worker controller wakes. Off (TFD_WORKER_VERSIONING=0) for a plain Temporal Service, where
    # something else has to wake the sandbox.
    versioning: bool
    deployment_name: str
    build_id: str
    # "stub" answers offline; "foundry" calls the MODEL_NAME deployment in the project.
    model: str
    # Foundry platform values (empty when run outside Foundry)
    project_endpoint: str
    agent_name: str
    agent_version: str
    session_id: str
    # Lifecycle. A session lives until the agent version's idle timeout after its last
    # inbound request, and the controller sends it exactly one.
    session_idle_timeout_seconds: int  # 0 = unknown: run until SIGTERM
    deadline_margin_seconds: int
    idle_exit_seconds: int  # 0 = never exit early
    keepalive_seconds: int  # while busy, send this session a request this often; 0 = off
    busy_linger_seconds: int  # still "busy" this long after the last Activity ended
    drain_seconds: int  # wait for in-flight Activities when the worker retires on its own
    shutdown_drain_seconds: int  # the same, when the platform is shutting the sandbox down
    self_stop: bool
    diagnostics: bool
    worker_disabled: bool  # answer wake requests without starting a worker (platform probes)
    allow_faults: bool  # accept the presenter's "crash the worker" request
    console_url: str  # the demo console, told about this sandbox's lifecycle; empty = nobody
    console_token: str | None
    start_on_boot: bool  # start the worker when the container starts, not on the request

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Settings:
        versioning = environ.get("TFD_WORKER_VERSIONING", "1").strip() != "0"
        missing = [
            name
            for name in ("TEMPORAL_DEPLOYMENT_NAME", "TEMPORAL_BUILD_ID")
            if versioning and not environ.get(name)
        ]
        if missing:
            # A worker that polls under a guessed version can take another build's work.
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        ca = environ.get("TEMPORAL_TLS_SERVER_CA_B64", "").strip()
        api_key = environ.get("TEMPORAL_API_KEY") or None
        return cls(
            temporal_address=environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            temporal_namespace=environ.get("TEMPORAL_NAMESPACE", "default"),
            temporal_api_key=api_key,
            temporal_tls=_flag(environ, "TEMPORAL_TLS") or bool(ca),
            temporal_server_ca=base64.b64decode(ca) if ca else None,
            temporal_server_name=environ.get("TEMPORAL_TLS_SERVER_NAME") or None,
            task_queue=environ.get("TEMPORAL_TASK_QUEUE", "tfd"),
            versioning=versioning,
            deployment_name=environ.get("TEMPORAL_DEPLOYMENT_NAME", ""),
            build_id=environ.get("TEMPORAL_BUILD_ID", ""),
            model=environ.get("TFD_MODEL", "stub"),
            project_endpoint=environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/"),
            agent_name=environ.get("FOUNDRY_AGENT_NAME", ""),
            agent_version=environ.get("FOUNDRY_AGENT_VERSION", ""),
            session_id=environ.get("FOUNDRY_AGENT_SESSION_ID", ""),
            session_idle_timeout_seconds=_int(environ, "TFD_SESSION_IDLE_TIMEOUT_SECONDS", 0),
            deadline_margin_seconds=_int(environ, "TFD_DEADLINE_MARGIN_SECONDS", 45),
            idle_exit_seconds=_int(environ, "TFD_IDLE_EXIT_SECONDS", 0),
            keepalive_seconds=_int(environ, "TFD_KEEPALIVE_SECONDS", 0),
            busy_linger_seconds=_int(environ, "TFD_BUSY_LINGER_SECONDS", 20),
            drain_seconds=_int(environ, "TFD_DRAIN_SECONDS", 60),
            shutdown_drain_seconds=_int(environ, "TFD_SHUTDOWN_DRAIN_SECONDS", 20),
            self_stop=_flag(environ, "TFD_SELF_STOP"),
            diagnostics=_flag(environ, "TFD_DIAGNOSTICS"),
            worker_disabled=_flag(environ, "TFD_WORKER_DISABLED"),
            allow_faults=_flag(environ, "TFD_ALLOW_FAULTS"),
            console_url=environ.get("TFD_CONSOLE_URL", "").rstrip("/"),
            console_token=environ.get("TFD_CONSOLE_TOKEN") or api_key,
            start_on_boot=environ.get("TFD_START_ON_BOOT", "1").strip() != "0",
        )

    @property
    def worker_budget_seconds(self) -> int | None:
        """How long a worker may keep polling after the session's last inbound request.

        Foundry stops an idle sandbox without warning, so the worker has to stop polling and
        drain before that. None means no deadline (run until told to stop).
        """
        if self.session_idle_timeout_seconds <= 0:
            return None
        return max(1, self.session_idle_timeout_seconds - self.deadline_margin_seconds)

    @property
    def worker_identity(self) -> str:
        """Ties a Temporal task to the Foundry session that ran it."""
        if self.session_id:
            return (
                f"foundry/{self.agent_name}/v{self.agent_version}/"
                f"{self.session_id[:12]}/pid{os.getpid()}"
            )
        return f"local/{os.uname().nodename}/pid{os.getpid()}"

    def version_mismatch(self, deployment_name: object, build_id: object) -> str | None:
        """Why a wake request for another version must be refused, or None if it is ours."""
        if not self.versioning:
            return None
        if deployment_name is not None and deployment_name != self.deployment_name:
            return f"this agent serves deployment {self.deployment_name!r}, not {deployment_name!r}"
        if build_id is not None and build_id != self.build_id:
            return f"this agent serves build {self.build_id!r}, not {build_id!r}"
        return None
