"""Drive real runs through the page, as a visitor would, and screenshot each state.

    .venv/bin/python tools/live_run.py                               # local harness on :8080
    .venv/bin/python tools/live_run.py --base https://... --code ... # a deployed console

Three runs, all through the real page and the real API:

1. started from the form with the default follow-up: plan, sandboxes back to zero, an edited
   sub-question, approval, research, brief, timer, follow-up;
2. started through the API with a 20 s follow-up (the form offers only the booth choices) and
   approved on the page;
3. started from the form and rejected.

``--only actions`` is a fourth walk, never part of the default: it starts a run, presses
"Crash the worker" and then "Stop the sandbox" on the first sandbox that comes up, and
rejects the plan. On a deployed console that really kills a worker and stops a session.

Exits non-zero if a state does not arrive in time or the page logs an error. Against a
deployed console this spends real model tokens: three runs.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
EDITED = "What do platform teams measure to decide whether an agent runtime is ready for production?"


class Walk:
    def __init__(self, page: Page, out: Path, patience: float) -> None:
        self.page = page
        self.out = out
        self.patience = patience
        self.started = time.monotonic()
        self.shots = 0

    def say(self, text: str) -> None:
        print(f"[{time.monotonic() - self.started:6.1f} s] {text}", flush=True)

    def shot(self, name: str) -> None:
        self.shots += 1
        self.page.wait_for_timeout(700)  # let a smooth scroll finish
        self.page.screenshot(path=self.out / f"{self.shots:02d}-{name}.png")

    def wait(self, selector: str, what: str, seconds: float | None = None) -> None:
        self.page.wait_for_selector(selector, timeout=(seconds or self.patience) * 1000)
        self.say(what)

    def phase(self, value: str, seconds: float | None = None) -> None:
        self.wait(f'[data-testid=phase][data-value="{value}"]', f"phase {value}", seconds)

    def zero(self, seconds: float | None = None) -> None:
        self.wait('[data-testid=sandbox-count][data-value="0"]', "no sandbox is up", seconds)

    def text(self, selector: str) -> str:
        return self.page.locator(selector).first.inner_text()

    def texts(self, selector: str) -> list[str]:
        return self.page.locator(selector).all_inner_texts()

    def summary(self) -> dict[str, Any]:
        return {
            "phase": self.page.locator("[data-testid=phase]").get_attribute("data-value"),
            "status": self.page.locator("[data-testid=phase]").get_attribute("data-status"),
            "wakes": self.texts("[data-testid=wake-badge]"),
            "sandboxes": [" ".join(text.split()) for text in self.texts("[data-testid=sandbox-card]")],
            "research": len(self.texts("[data-testid=research-item]")),
            "run": self.text("#run-id"),
        }


def login(walk: Walk, base: str, code: str) -> None:
    page = walk.page
    page.goto(base)
    page.wait_for_selector("[data-testid=access-code]:visible")
    page.fill("[data-testid=access-code]", code)
    page.click("[data-testid=login-button]")
    walk.wait("[data-testid=question-input]:visible", "logged in")


def new_question(walk: Walk) -> None:
    walk.page.click("#new-run-button")
    walk.wait("[data-testid=question-input]:visible", "back at the form")
    walk.zero(90)


def full_run(walk: Walk) -> dict[str, Any]:
    page = walk.page
    walk.shot("ready")
    page.click("[data-testid=preset-question] >> nth=1")
    page.click("[data-testid=start-button]")
    walk.wait("[data-testid=phase]:visible", "run started from the form")
    walk.shot("started")
    walk.wait("[data-testid=approve-button]:visible", "the plan is on the page")
    walk.shot("plan")
    walk.zero(120)
    walk.shot("approval-at-zero")

    box = page.locator("[data-testid=plan] ol textarea").nth(1)
    box.fill(EDITED)
    page.locator("[data-testid=plan] .plan-note input").fill("Keep it practical.")
    walk.shot("plan-edited")
    page.click("[data-testid=approve-button]")
    walk.say("approved with sub-question 2 rewritten")
    page.wait_for_timeout(1500)
    walk.shot("approved-waking")

    walk.wait("[data-testid=research-item]", "researchers are on the page")
    walk.shot("researching")
    questions = walk.texts("[data-testid=research-item] .research-q")
    if EDITED not in questions:
        raise SystemExit(f"the edited sub-question did not reach the researchers: {questions}")
    walk.say("the edited sub-question reached researcher 2")

    walk.wait("[data-testid=report]:visible", "the brief is on the page")
    walk.shot("brief")
    walk.phase("waiting_for_follow_up")
    walk.zero(120)
    walk.shot("timer-at-zero")
    walk.wait("[data-testid=follow-up]:visible", "the follow-up is on the page", 240)
    walk.phase("completed")
    walk.shot("follow-up")
    walk.zero(120)
    walk.shot("done-at-zero")
    page.evaluate("() => document.getElementById('work').scrollTo({top: 0, behavior: 'instant'})")
    walk.shot("done-top")
    return walk.summary()


def api_run(walk: Walk, base: str) -> dict[str, Any]:
    page = walk.page
    response = page.request.post(
        f"{base}/api/runs",
        data={
            "question": "What changed for teams running AI agents on Azure this year?",
            "follow_up_after_seconds": 20,
        },
        headers={"Origin": base},
    )
    if not response.ok:
        raise SystemExit(f"POST /api/runs failed: {response.status} {response.text()}")
    run_id = response.json()["id"]
    walk.say(f"run {run_id} started through the API with a 20 s follow-up")
    page.goto(f"{base}/#/run/{run_id}")
    page.reload()
    walk.wait("[data-testid=approve-button]:visible", "the plan is on the page")
    page.click("[data-testid=approve-button]")
    walk.say("approved unchanged")
    walk.wait("[data-testid=report]:visible", "the brief is on the page")
    walk.phase("waiting_for_follow_up")
    walk.shot("api-run-timer")
    walk.wait("[data-testid=follow-up]:visible", "the follow-up is on the page")
    walk.phase("completed")
    walk.shot("api-run-follow-up")
    return walk.summary()


def rejected_run(walk: Walk) -> dict[str, Any]:
    page = walk.page
    page.fill("[data-testid=question-input]", "Which agent frameworks are enterprises standardising on?")
    page.click("#follow-up-choice button >> nth=0")
    page.click("[data-testid=start-button]")
    walk.wait("[data-testid=reject-button]:visible", "the plan is on the page")
    walk.zero(120)
    page.click("[data-testid=reject-button]")
    walk.say("rejected")
    page.wait_for_timeout(1500)
    walk.shot("rejected-waking")
    walk.phase("rejected")
    walk.shot("rejected")
    walk.zero(120)
    walk.shot("rejected-at-zero")
    return walk.summary()


def actions_run(walk: Walk) -> dict[str, Any]:
    page = walk.page
    page.fill("[data-testid=question-input]", "What do teams get wrong about memory for AI agents?")
    page.click("#follow-up-choice button >> nth=0")
    page.click("[data-testid=start-button]")
    walk.wait("[data-testid=crash-worker-button]:visible", "a sandbox is up and offers the presenter actions")
    card = page.locator("[data-testid=sandbox-card]", has=page.locator("[data-testid=crash-worker-button]")).first
    name = card.locator(".sandbox-id").inner_text()
    card.locator("[data-testid=crash-worker-button]").click()
    walk.say(f"asked sandbox {name} to crash its worker")
    page.wait_for_function(
        "(name) => [...document.querySelectorAll('[data-testid=sandbox-card]')]"
        ".some((card) => card.textContent.includes(name) && /crash/i.test(card.textContent))",
        arg=name,
        timeout=20_000,
    )
    walk.shot("crash-requested")
    stop = page.locator("[data-testid=sandbox-card]", has_text=name).locator("[data-testid=stop-sandbox-button]")
    if stop.count():
        stop.first.click()
        walk.say(f"asked for sandbox {name} to be stopped")
        page.wait_for_function(
            "(name) => [...document.querySelectorAll('[data-testid=sandbox-card]')]"
            ".some((card) => card.textContent.includes(name) && !card.querySelector('[data-testid=stop-sandbox-button]'))",
            arg=name,
            timeout=60_000,
        )
        walk.say(f"sandbox {name} no longer offers the actions")
    walk.shot("stop-requested")
    walk.wait("[data-testid=reject-button]:visible", "the plan is on the page")
    page.click("[data-testid=reject-button]")
    walk.phase("rejected")
    walk.shot("after-actions")
    return walk.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--code", default="booth")
    parser.add_argument("--out", type=Path, default=HERE.parent / "screenshots" / "live")
    parser.add_argument("--size", default="1920x1080")
    parser.add_argument("--patience", type=float, default=180.0, help="seconds to wait for each state")
    parser.add_argument("--only", choices=["full", "api", "rejected", "actions"], action="append")
    args = parser.parse_args()
    width, height = (int(part) for part in args.size.lower().split("x"))
    args.out.mkdir(parents=True, exist_ok=True)
    for stale in args.out.glob("*.png"):
        stale.unlink()
    base = args.base.rstrip("/")
    which = args.only or ["full", "api", "rejected"]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        problems: list[str] = []

        def on_console(message: Any) -> None:
            if message.type == "error" and "status of 401" not in message.text:
                problems.append(message.text)

        page.on("console", on_console)
        page.on("pageerror", lambda error: problems.append(str(error)))
        walk = Walk(page, args.out, args.patience)
        results: dict[str, Any] = {}
        try:
            login(walk, base, args.code)
            if "full" in which:
                results["full"] = full_run(walk)
                new_question(walk)
            if "api" in which:
                results["api"] = api_run(walk, base)
                new_question(walk)
            if "rejected" in which:
                results["rejected"] = rejected_run(walk)
            if "actions" in which:
                if len(which) > 1:
                    new_question(walk)
                results["actions"] = actions_run(walk)
        except Exception:
            page.screenshot(path=args.out / "99-where-it-stopped.png")
            raise
        finally:
            browser.close()

    for name, result in results.items():
        print(f"\n{name}: {result['run']} ended in phase {result['phase']} ({result['status']})")
        for wake in result["wakes"]:
            print(f"  wake: {wake}")
        for box in result["sandboxes"]:
            print(f"  sandbox: {box}")
    if problems:
        print("\npage errors:", *problems, sep="\n  ")
        return 1
    print(f"\n{walk.shots} screenshots in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
