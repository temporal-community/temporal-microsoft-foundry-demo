"""Activities for the probe workflow. The agents' model calls are the plugin's own Activities."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from temporalio import activity

from .settings import Settings
from .shared import Stamp


def _stamp(stage: str) -> Stamp:
    return Stamp(
        stage=stage,
        worker=Settings.from_env().worker_identity,
        at=datetime.now(UTC).isoformat(timespec="seconds"),
        attempt=activity.info().attempt,
    )


@activity.defn
async def where(stage: str) -> Stamp:
    """Record which worker handled a stage."""
    return _stamp(stage)


@activity.defn
async def busy(stage: str, seconds: int) -> Stamp:
    """Stay busy for a while, heartbeating, so a lost sandbox is noticed."""
    for _ in range(seconds):
        activity.heartbeat()
        await asyncio.sleep(1)
    return _stamp(stage)


@activity.defn
async def exit_process(stage: str) -> Stamp:
    """Opt-in fault: the first attempt kills its worker process, later attempts succeed."""
    if os.environ.get("TFD_ENABLE_FAULTS") == "1" and activity.info().attempt == 1:
        activity.logger.warning("TFD fault injection: exiting the worker process")
        os._exit(86)
    return _stamp(stage)
