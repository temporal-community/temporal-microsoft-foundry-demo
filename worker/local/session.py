"""Local stand-in for one Foundry session, for the controller's dev-only subprocess provider.

Does what Foundry does when the controller invokes a hosted agent: starts the container entry
point (main.py), sends it one invocation, and leaves it alone until it is terminated. The
subprocess provider wraps this in timeout(1), which plays the idle timeout.

    session.py --build-id local1 --idle-timeout 40 [--deployment tfd] [--log-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


def free_port() -> int:
    # Probe on every interface, as the agent server binds: a port free on loopback alone
    # can still be taken.
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--deployment", default="tfd")
    parser.add_argument("--idle-timeout", type=int, default=0)
    parser.add_argument("--idle-exit", type=int, default=0)
    parser.add_argument("--log-dir", default="/tmp/foundry/run/logs")
    args = parser.parse_args()

    session_id = uuid.uuid4().hex
    port = free_port()
    env = dict(os.environ)
    env.update(
        PORT=str(port),
        TEMPORAL_DEPLOYMENT_NAME=args.deployment,
        TEMPORAL_BUILD_ID=args.build_id,
        TEMPORAL_TASK_QUEUE=args.deployment,
        TFD_MODEL=env.get("TFD_MODEL", "stub"),
        TFD_SESSION_IDLE_TIMEOUT_SECONDS=str(args.idle_timeout),
        TFD_DEADLINE_MARGIN_SECONDS=env.get("TFD_DEADLINE_MARGIN_SECONDS", "10"),
        TFD_IDLE_EXIT_SECONDS=str(args.idle_exit),
        TFD_DRAIN_SECONDS=env.get("TFD_DRAIN_SECONDS", "5"),
        TFD_SHUTDOWN_DRAIN_SECONDS=env.get("TFD_SHUTDOWN_DRAIN_SECONDS", "3"),
        TFD_DIAGNOSTICS="1",
        FOUNDRY_AGENT_NAME=f"local-{args.build_id}",
        FOUNDRY_AGENT_VERSION="1",
        FOUNDRY_AGENT_SESSION_ID=session_id,
    )
    Path(args.log_dir).mkdir(parents=True, exist_ok=True)
    log = open(Path(args.log_dir) / f"session-{session_id[:8]}.log", "ab")
    child = subprocess.Popen(
        [sys.executable, "-u", str(APP_DIR / "main.py")],
        cwd=str(APP_DIR),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    signal.signal(signal.SIGTERM, lambda *_: child.terminate())
    signal.signal(signal.SIGINT, lambda *_: child.terminate())

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline and child.poll() is None:
        try:
            urllib.request.urlopen(f"{base}/readiness", timeout=1).read()
            break
        except OSError:
            time.sleep(0.2)
    if child.poll() is None:
        request = urllib.request.Request(
            f"{base}/invocations",
            data=json.dumps({"deploymentName": args.deployment, "buildId": args.build_id}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10).read()
        except OSError as error:
            log.write(f"local session: invocation failed: {error}\n".encode())
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
