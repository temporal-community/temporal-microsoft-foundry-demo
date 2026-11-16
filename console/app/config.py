"""Configuration, read from the environment once at startup."""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tfd.console")

DEFAULT_PRESETS = (
    "What is new in Microsoft Foundry Agent Service in 2026?",
    "How are enterprises running long-lived AI agents in production?",
    "What should a platform team check before adopting Microsoft Agent Framework?",
    "How do durable execution and AI agents fit together?",
)


def _int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    return int(raw) if raw else default


def _flag(environ: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes"}


def _presets(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Preset questions: a JSON list of strings, or questions separated by ``|``.

    The second form is for systemd environment files, whose quoting rules differ between
    versions. A value that is neither falls back to the built-in questions: a booth page
    with the wrong presets is better than no page.
    """
    raw = environ.get("TFD_PRESETS", "").strip()
    if not raw:
        return DEFAULT_PRESETS
    values: object
    if raw.startswith("["):
        try:
            values = json.loads(raw)
        except ValueError:
            values = None
    else:
        values = raw.split("|")
    if isinstance(values, list) and values and all(isinstance(item, str) and item.strip() for item in values):
        return tuple(item.strip() for item in values)[:8]
    logger.warning("TFD_PRESETS is neither a JSON list of strings nor a |-separated list; using the built-in questions")
    return DEFAULT_PRESETS


@dataclass(frozen=True)
class Config:
    temporal_address: str
    temporal_namespace: str
    task_queue: str
    access_code: str
    internal_token: str
    session_secret: bytes
    cookie_secure: bool
    foundry_project_endpoint: str
    agent_name: str
    model_name: str
    region: str
    foundry_stop: bool
    show_stop_button: bool
    temporal_ui_url: str
    max_open_runs: int
    max_question_length: int
    max_follow_up_seconds: int
    starts_per_minute: int
    state_dir: Path
    presets: tuple[str, ...]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Config:
        missing = [name for name in ("TFD_ACCESS_CODE", "TFD_INTERNAL_TOKEN") if not environ.get(name)]
        if missing:
            # Refuse to serve a public page with no access code or an open callback.
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        secret = environ.get("TFD_SESSION_SECRET", "")
        return cls(
            temporal_address=environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            temporal_namespace=environ.get("TEMPORAL_NAMESPACE", "default"),
            task_queue=environ.get("TFD_TASK_QUEUE", "tfd"),
            access_code=environ["TFD_ACCESS_CODE"],
            internal_token=environ["TFD_INTERNAL_TOKEN"],
            # Without a configured secret, logins last until the process restarts.
            session_secret=secret.encode() if secret else secrets.token_bytes(32),
            cookie_secure=_flag(environ, "TFD_COOKIE_SECURE", default=True),
            foundry_project_endpoint=environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/"),
            agent_name=environ.get("TFD_AGENT_NAME", ""),
            model_name=environ.get("TFD_MODEL_NAME", ""),
            region=environ.get("TFD_REGION", ""),
            foundry_stop=_flag(environ, "TFD_FOUNDRY_STOP"),
            show_stop_button=_flag(environ, "TFD_SHOW_STOP_BUTTON"),
            temporal_ui_url=environ.get("TFD_TEMPORAL_UI_URL", "").rstrip("/"),
            max_open_runs=_int(environ, "TFD_MAX_OPEN_RUNS", 8),
            max_question_length=_int(environ, "TFD_MAX_QUESTION_LENGTH", 300),
            max_follow_up_seconds=_int(environ, "TFD_MAX_FOLLOW_UP_SECONDS", 600),
            starts_per_minute=_int(environ, "TFD_STARTS_PER_MINUTE", 4),
            state_dir=Path(environ.get("TFD_STATE_DIR", "state")),
            presets=_presets(environ),
        )
