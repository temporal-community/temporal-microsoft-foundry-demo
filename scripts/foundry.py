"""Poke the hosted agent from a laptop: wake it, probe it, list and stop sessions, read logs.

    foundry.py wake --agent tfd-worker-abc                      # what the controller does
    foundry.py wake --agent tfd-worker-abc --abandon-after 5    # hang up early, as the provider does
    foundry.py probe --agent tfd-worker-abc --session ID --body '{"probe": "env"}'
    foundry.py sessions --agent tfd-worker-abc
    foundry.py watch --agent tfd-worker-abc --seconds 400
    foundry.py logs --agent tfd-worker-abc --version 1 --session ID --seconds 60
    foundry.py stop --agent tfd-worker-abc --session ID
    foundry.py agents

Reads PROJECT_ENDPOINT. Authenticates as the current Azure login.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

SCOPE = "https://ai.azure.com/.default"
API = {"api-version": "v1"}


def clock() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]


class Foundry:
    def __init__(self) -> None:
        self.endpoint = os.environ["PROJECT_ENDPOINT"].rstrip("/")
        self._credential = DefaultAzureCredential()

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._credential.get_token(SCOPE).token}"}

    def url(self, agent: str, suffix: str) -> str:
        return f"{self.endpoint}/agents/{agent}/{suffix}"

    def invoke(
        self, agent: str, body: dict[str, Any], session: str | None, timeout: float
    ) -> dict[str, Any]:
        params = dict(API)
        if session:
            params["agent_session_id"] = session
        headers = self.headers()
        started = time.monotonic()
        try:
            response = httpx.post(
                self.url(agent, "endpoint/protocols/invocations"),
                params=params,
                headers=headers,
                json=body,
                timeout=httpx.Timeout(timeout, connect=10),
            )
        except httpx.TimeoutException:
            return {"abandoned_after": round(time.monotonic() - started, 2)}
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text[:500]
        return {
            "status_code": response.status_code,
            "seconds": round(time.monotonic() - started, 2),
            "session": response.headers.get("x-agent-session-id"),
            "invocation": response.headers.get("x-agent-invocation-id"),
            "body": payload,
        }

    def sessions(self, agent: str) -> list[dict[str, Any]]:
        # Observed 2026-11-15: the list returns one session per page whatever ``limit`` says,
        # with ``has_more: false`` even when there are more, so follow the cursor until empty.
        found: list[dict[str, Any]] = []
        headers = self.headers()
        after: str | None = None
        for _ in range(500):
            params = {**API, "limit": "100", "order": "asc"}
            if after:
                params["after"] = after
            response = httpx.get(
                self.url(agent, "endpoint/sessions"), params=params, headers=headers, timeout=30
            )
            response.raise_for_status()
            page = response.json().get("data", [])
            if not page:
                break
            found.extend(page)
            after = page[-1].get("agent_session_id")
        return found

    def stop(self, agent: str, session: str) -> int:
        response = httpx.post(
            self.url(agent, f"endpoint/sessions/{session}:stop"),
            params=API,
            headers=self.headers(),
            timeout=30,
        )
        return response.status_code

    def logs(self, agent: str, version: str, session: str, seconds: float, everything: bool) -> None:
        deadline = time.monotonic() + seconds
        with httpx.stream(
            "GET",
            self.url(agent, f"versions/{version}/sessions/{session}:logstream"),
            params=API,
            headers={**self.headers(), "Accept": "text/event-stream"},
            timeout=httpx.Timeout(seconds + 30, connect=10),
        ) as response:
            if response.status_code != 200:
                print(f"log stream: HTTP {response.status_code} {response.read()[:300]!r}")
                return
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    return
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                try:
                    record = json.loads(data)
                    text = f"{record.get('timestamp', '')} {record.get('stream', '')} {record.get('message', '')}"
                except ValueError:
                    text = data
                if everything or "TFD_EVENT" in text or '"status"' in data or " status " in text:
                    print(text[:400], flush=True)


def fmt_time(value: Any) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(int(value), UTC).strftime("%H:%M:%S")


def print_sessions(sessions: list[dict[str, Any]]) -> None:
    for item in sessions:
        print(
            f"  {item.get('agent_session_id', '')[:16]:16}  {item.get('status', ''):9}"
            f"  created {fmt_time(item.get('created_at'))}"
            f"  last {fmt_time(item.get('last_accessed_at'))}"
            f"  stopped {fmt_time(item.get('stopped_at'))}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("wake", "probe"):
        sub = commands.add_parser(name)
        sub.add_argument("--agent", required=True)
        sub.add_argument("--session")
        sub.add_argument("--body", default="{}")
        sub.add_argument("--abandon-after", type=float)
        sub.add_argument("--timeout", type=float, default=180)
    for name in ("sessions", "watch"):
        sub = commands.add_parser(name)
        sub.add_argument("--agent", required=True)
        sub.add_argument("--seconds", type=int, default=300)
        sub.add_argument("--interval", type=int, default=5)
    stop = commands.add_parser("stop")
    stop.add_argument("--agent", required=True)
    stop.add_argument("--session", required=True)
    logs = commands.add_parser("logs")
    logs.add_argument("--agent", required=True)
    logs.add_argument("--version", default="1")
    logs.add_argument("--session", required=True)
    logs.add_argument("--seconds", type=float, default=60)
    logs.add_argument("--all", action="store_true")
    commands.add_parser("agents")
    args = parser.parse_args()

    foundry = Foundry()
    if args.command in ("wake", "probe"):
        timeout = args.abandon_after if args.abandon_after else args.timeout
        print(f"[{clock()}] POST invocations agent={args.agent} session={args.session or '(new)'}")
        result = foundry.invoke(args.agent, json.loads(args.body), args.session, timeout)
        print(f"[{clock()}] {json.dumps(result)[:1500]}")
    elif args.command == "sessions":
        print_sessions(foundry.sessions(args.agent))
    elif args.command == "watch":
        seen: dict[str, str] = {}
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            for item in foundry.sessions(args.agent):
                key, status = item.get("agent_session_id", ""), item.get("status", "")
                if seen.get(key) != status:
                    seen[key] = status
                    print(
                        f"[{clock()}] {key[:16]} -> {status}"
                        f" (created {fmt_time(item.get('created_at'))},"
                        f" last {fmt_time(item.get('last_accessed_at'))},"
                        f" stopped {fmt_time(item.get('stopped_at'))})",
                        flush=True,
                    )
            if seen and all(status not in ("creating", "active", "updating") for status in seen.values()):
                print(f"[{clock()}] no active sessions")
                return
            time.sleep(args.interval)
    elif args.command == "stop":
        print(f"[{clock()}] stop -> HTTP {foundry.stop(args.agent, args.session)}")
    elif args.command == "logs":
        foundry.logs(args.agent, args.version, args.session, args.seconds, args.all)
    elif args.command == "agents":
        response = httpx.get(f"{foundry.endpoint}/agents", params=API, headers=foundry.headers(), timeout=30)
        for item in response.json().get("data", []):
            latest = (item.get("versions") or {}).get("latest") or {}
            print(f"  {item.get('name')}  latest v{latest.get('version')} {latest.get('status', '')}")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as error:
        print(f"HTTP {error.response.status_code}: {error.response.text[:500]}", file=sys.stderr)
        raise SystemExit(1) from None
