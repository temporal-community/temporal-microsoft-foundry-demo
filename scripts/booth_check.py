"""Run the booth flow through the console's API and print what happened and when.

A pre-shift check, and the source of the timings in the booth notes: start a run from zero
sandboxes, wait for the plan, wait for the sandbox to be released, approve, and follow the
run to the end.

    TFD_BASE=https://host TFD_CODE=<access code> booth_check.py [--follow-up 60] [--question "..."]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime

import httpx


def clock() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default="")
    parser.add_argument("--follow-up", type=int, default=60)
    parser.add_argument("--approve-at-once", action="store_true", help="do not wait for zero")
    parser.add_argument(
        "--break",
        dest="fault",
        choices=["crash-worker", "stop"],
        help="three seconds into the research, crash the worker or stop the sandbox",
    )
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    base = os.environ["TFD_BASE"].rstrip("/")
    http = httpx.Client(base_url=base, timeout=30, headers={"Origin": base})
    login = http.post("/api/login", json={"code": os.environ["TFD_CODE"]})
    if login.status_code != 200:
        print(f"login failed: HTTP {login.status_code}")
        return 1
    config = http.get("/api/config").json()
    question = args.question or config["presets"][1]

    def up() -> int:
        return http.get("/api/sandboxes").json()["up"]

    started_waiting = time.time()
    while up() > 0 and time.time() - started_waiting < 300:
        time.sleep(2)
    print(f"[{clock()}] sandboxes up: {up()}  agent {config['agent_name']}")

    run_id = http.post(
        "/api/runs", json={"question": question, "follow_up_after_seconds": args.follow_up}
    ).json()["id"]
    t0 = time.time()
    print(f"[{clock()}] started {run_id}: {question}")
    marks: dict[str, float] = {}
    last_phase, last_up, approved, seen_wakes = "", -1, False, 0
    research_since, broken_at = 0.0, 0.0
    while time.time() - t0 < args.timeout:
        run = http.get(f"/api/runs/{run_id}").json()
        count = up()
        phase = run.get("phase", "")
        if phase != last_phase:
            marks.setdefault(phase, time.time() - t0)
            print(f"[{clock()}] +{time.time() - t0:5.1f}s phase {phase}")
            last_phase = phase
        if count != last_up:
            print(f"[{clock()}] +{time.time() - t0:5.1f}s sandboxes up: {count}")
            if count == 0:
                marks.setdefault(f"zero_during_{phase}", time.time() - t0)
            last_up = count
        wakes = run.get("wakes", [])
        for wake in wakes[seen_wakes:]:
            print(f"[{clock()}]          wake: {wake}")
        seen_wakes = len(wakes)
        if phase == "awaiting_approval" and not approved and (args.approve_at_once or count == 0):
            http.post(
                f"/api/runs/{run_id}/decision",
                json={"approved": True, "note": "Keep it practical.", "subquestions": None},
            )
            marks["approved"] = time.time() - t0
            print(f"[{clock()}] +{time.time() - t0:5.1f}s approved with {count} sandboxes up")
            approved = True
        if phase == "researching" and not research_since:
            research_since = time.time()
        if args.fault and research_since and not broken_at and time.time() - research_since >= 3:
            working = [
                box for box in http.get("/api/sandboxes").json()["sandboxes"]
                if box.get("state") == "working"
            ]
            if working:
                session = working[0]["session"]
                answer = http.post(f"/api/sandboxes/{session}/{args.fault}")
                broken_at = time.time()
                print(f"[{clock()}] +{time.time() - t0:5.1f}s {args.fault} on {session[:12]} -> HTTP {answer.status_code}")
        if run.get("status") not in (None, "RUNNING", "running") or phase in ("completed", "rejected"):
            break
        time.sleep(1)
    run = http.get(f"/api/runs/{run_id}").json()
    print(f"[{clock()}] finished: status {run.get('status')} phase {run.get('phase')}")
    print("marks (s after start):", {key: round(value, 1) for key, value in marks.items()})
    if broken_at:
        retried = [
            (item.get("agent") or item.get("label"), item.get("attempt"), item.get("sandbox"))
            for item in run.get("timeline", [])
            if (item.get("attempt") or 1) > 1
        ]
        print(f"after the {args.fault}: finished {time.time() - broken_at:.0f} s later; retried steps: {retried}")
    print("wakes:", [(w.get("cause"), w.get("latency_seconds"), w.get("sandbox")) for w in run.get("wakes", [])])
    print("sandboxes used:", run.get("sandboxes"))
    report = run.get("report") or ""
    print(f"brief: {len(report.split())} words, {report.count('](http')} links; follow-up: {len((run.get('follow_up') or '').split())} words")
    return 0 if run.get("phase") == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
