"""Start the worker's local stand-in for a Foundry session, and try again if it dies at once.

`worker/local/session.py` picks a port that is free on 127.0.0.1, and the agent server then
binds it on every interface, which fails now and then on a busy laptop. The controller does
not retry a failed wake, so one collision leaves a local run waiting for minutes. This
wrapper is what the local controller is told to start; arguments pass through.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

SESSION = Path(__file__).resolve().parents[2] / "worker" / "local" / "session.py"


def main() -> int:
    child: subprocess.Popen[bytes] | None = None

    def forward(*_: object) -> None:
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    code = 1
    for _ in range(4):
        started = time.monotonic()
        child = subprocess.Popen([sys.executable, str(SESSION), *sys.argv[1:]])
        code = child.wait()
        if code == 0 or time.monotonic() - started > 5:
            break  # it ran; only an immediate failure is worth another try
    return code


if __name__ == "__main__":
    raise SystemExit(main())
