"""Screenshots of every state of the page, from the scripted server.

    .venv/bin/python tools/screenshots.py                      # 1920x1080 into screenshots/
    .venv/bin/python tools/screenshots.py --size 1440x900 --out screenshots/laptop

Starts tools/fake_server.py on port 8090, walks the login, the start form and each stage of
a run, and writes one PNG per state.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8090"
STAGES = [
    "starting",
    "planning",
    "approval",
    "waking",
    "researching",
    "retry",
    "refused",
    "writing",
    "waiting",
    "followup",
    "done",
    "failed",
    "rejecting",
    "rejected",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", default="1920x1080", help="viewport, WIDTHxHEIGHT")
    parser.add_argument("--out", type=Path, default=HERE.parent / "screenshots")
    args = parser.parse_args()
    width, height = (int(part) for part in args.size.lower().split("x"))
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.png"):
        stale.unlink()

    server = subprocess.Popen([sys.executable, str(HERE / "fake_server.py"), "8090"])
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{BASE}/healthz", timeout=1).read()
                break
            except OSError:
                time.sleep(0.2)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height})
            problems: list[str] = []

            def on_console(message: object) -> None:
                # A 401 before login and after a wrong code is the page working as intended.
                text = getattr(message, "text", "")
                if getattr(message, "type", "") == "error" and "status of 401" not in text:
                    problems.append(text)

            page.on("console", on_console)
            page.on("pageerror", lambda error: problems.append(str(error)))

            page.goto(BASE)
            page.wait_for_selector("[data-testid=access-code]:visible")
            page.screenshot(path=out / "01-login.png")
            page.fill("[data-testid=access-code]", "wrong")
            page.click("[data-testid=login-button]")
            # Arrow functions, not expression strings: the page's CSP forbids eval.
            page.wait_for_function("() => document.getElementById('login-error').textContent.length > 0")
            page.screenshot(path=out / "02-login-wrong-code.png")
            page.fill("[data-testid=access-code]", "booth")
            page.click("[data-testid=login-button]")
            page.wait_for_selector("[data-testid=question-input]:visible")
            page.wait_for_timeout(1500)
            page.screenshot(path=out / "03-ready.png")
            page.click("[data-testid=preset-question] >> nth=1")
            page.wait_for_timeout(200)
            page.screenshot(path=out / "04-ready-question.png")

            for number, stage in enumerate(STAGES, start=5):
                page.goto(f"{BASE}/#/run/research-{stage}")
                page.reload()
                page.wait_for_selector("[data-testid=phase]:visible")
                page.wait_for_timeout(2600)  # two polls, and the smooth scroll to the newest section
                page.screenshot(path=out / f"{number:02d}-run-{stage}.png")
                if stage in ("done", "researching"):
                    page.evaluate("() => document.getElementById('work').scrollTo({top: 0, behavior: 'instant'})")
                    page.wait_for_timeout(300)
                    page.screenshot(path=out / f"{number:02d}-run-{stage}-top.png")
            if problems:
                print("page errors:", *problems, sep="\n  ")
            browser.close()
    finally:
        server.terminate()
        server.wait()
    print(f"wrote {len(list(out.glob('*.png')))} screenshots at {width}x{height} to {out}")


if __name__ == "__main__":
    main()
