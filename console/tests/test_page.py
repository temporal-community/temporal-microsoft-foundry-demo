"""The page in a real browser, against the scripted server in tools/fake_server.py.

Needs Chromium for Playwright (``.venv/bin/python -m playwright install chromium``); the
tests skip themselves when it is not installed.

The page's Content-Security-Policy forbids eval, so everything handed to the browser here is
an arrow function, never an expression string.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]

sync_api = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    spec = importlib.util.spec_from_file_location("tfd_fake_server", ROOT / "tools" / "fake_server.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(module.build(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the scripted server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch()
        except sync_api.Error as error:  # no browser on this machine
            pytest.skip(f"Chromium for Playwright is not installed: {error}")
        yield chromium
        chromium.close()


class Visit:
    """One logged-in page, with everything it requested and every error it logged."""

    def __init__(self, browser: Any, base_url: str) -> None:
        self.base = base_url
        self.context = browser.new_context(viewport={"width": 1920, "height": 1080})
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.requests: list[str] = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", self._console)
        self.page.on("request", lambda request: self.requests.append(request.url))

    def _console(self, message: Any) -> None:
        # A 401 before login, or after a wrong code, is the page working as intended.
        if message.type == "error" and "status of 401" not in message.text:
            self.errors.append(message.text)

    def login(self) -> None:
        self.page.goto(self.base)
        self.page.wait_for_selector("[data-testid=access-code]:visible")
        self.page.fill("[data-testid=access-code]", "booth")
        self.page.click("[data-testid=login-button]")
        self.page.wait_for_selector("[data-testid=question-input]:visible")

    def open_stage(self, stage: str) -> None:
        self.page.goto(f"{self.base}/#/run/research-{stage}")
        self.page.reload()
        self.page.wait_for_selector("[data-testid=phase]:visible")
        self.page.wait_for_selector("[data-testid=timeline] li")

    def count(self, testid: str) -> int:
        return int(self.page.locator(f"[data-testid={testid}]").count())

    def visible(self, testid: str) -> bool:
        return bool(self.page.locator(f"[data-testid={testid}]").first.is_visible())

    def attribute(self, testid: str, name: str) -> str | None:
        value = self.page.locator(f"[data-testid={testid}]").first.get_attribute(name)
        return None if value is None else str(value)

    def close(self) -> None:
        self.context.close()


@pytest.fixture()
def visit(browser: Any, base_url: str) -> Iterator[Visit]:
    current = Visit(browser, base_url)
    yield current
    current.close()


RENDER = """(text) => {
  const box = document.createElement('div');
  document.body.append(box);
  window.tfdRenderMarkdown(text, box);
  const result = {
    html: box.innerHTML,
    text: box.textContent,
    tags: [...box.querySelectorAll('*')].map((node) => node.tagName.toLowerCase()),
    links: [...box.querySelectorAll('a')].map((a) => ({
      href: a.getAttribute('href'), text: a.textContent, target: a.target, rel: a.rel,
    })),
    handlers: [...box.querySelectorAll('*')].flatMap((node) => node.getAttributeNames()).filter((name) => name.startsWith('on')),
  };
  box.remove();
  return result;
}"""

SAMPLE = """\
## Where the agents run

Managed runtimes release idle compute (**documented at [2 minutes to 4 hours](https://learn.microsoft.com/x)**),
and *function hosts* cap one invocation.

- first bullet with `code`
- second bullet
  continued on the next line

1. one
2. two
"""

ALLOWED_TAGS = {"p", "ul", "ol", "li", "h3", "h4", "h5", "strong", "em", "code", "a"}

HOSTILE = [
    '<img src=x onerror="window.pwned = 1">',
    "<script>window.pwned = 1</script>",
    '<iframe srcdoc="<script>parent.pwned = 1</script>"></iframe>',
    "[click me](javascript:window.pwned=1)",
    "[click me](JaVaScRiPt:window.pwned=1)",
    "[data](data:text/html,<script>window.pwned=1</script>)",
    "[vb](vbscript:msgbox(1))",
    '[quote](https://example.com/"onmouseover="window.pwned=1)',
    "**bold with <b onclick='window.pwned=1'>markup</b> and a [link](https://example.com/a)**",
    "# Heading with <svg onload='window.pwned=1'>",
    "- item with `<script>window.pwned=1</script>` in code",
    '1. <a href="javascript:window.pwned=1">raw anchor</a>',
    "</div></article><script>window.pwned=1</script>",
    "&lt;script&gt;window.pwned=1&lt;/script&gt;",
]


def test_model_text_never_becomes_markup(visit: Visit) -> None:
    visit.login()
    for text in HOSTILE:
        result = visit.page.evaluate(RENDER, text)
        assert set(result["tags"]) <= ALLOWED_TAGS, (text, result["tags"])
        assert result["handlers"] == [], text
        for link in result["links"]:
            assert link["href"].startswith(("https://", "http://")), (text, link)
            assert link["target"] == "_blank"
            assert "noopener" in link["rel"] and "noreferrer" in link["rel"]
    visit.page.wait_for_timeout(300)  # time for an onerror or onload to have run, had one existed
    assert visit.page.evaluate("() => window.pwned === undefined")
    assert visit.errors == []


def test_markdown_subset_renders_as_expected(visit: Visit) -> None:
    visit.login()
    result = visit.page.evaluate(RENDER, SAMPLE)
    assert result["tags"] == ["h4", "p", "strong", "a", "em", "ul", "li", "code", "li", "ol", "li", "li"]
    assert result["links"] == [
        {
            "href": "https://learn.microsoft.com/x",
            "text": "2 minutes to 4 hours",
            "target": "_blank",
            "rel": "noopener noreferrer",
        }
    ]
    assert "second bullet continued on the next line" in result["text"]
    assert "**" not in result["text"]


def test_markdown_survives_pathological_text(visit: Visit) -> None:
    visit.login()
    # Nested emphasis once hung the renderer; unbalanced and repeated markers must stay fast.
    for text in [
        "**a [b](https://example.com) **c** d**" * 400,
        "*" * 5000,
        "[" * 3000 + "](" * 3000,
        "`" * 4001,
        "**[x](https://e.com)**\n" * 2000,
    ]:
        started = time.monotonic()
        result = visit.page.evaluate(RENDER, text)
        assert time.monotonic() - started < 5, text[:40]
        assert set(result["tags"]) <= ALLOWED_TAGS
    assert visit.errors == []


def test_a_finished_run_shows_every_part_the_recorder_looks_for(visit: Visit) -> None:
    visit.login()
    visit.open_stage("done")
    page = visit.page
    page.wait_for_selector('[data-testid=phase][data-value="completed"]')
    assert visit.attribute("phase", "data-status") == "COMPLETED"
    assert visit.visible("report") and visit.visible("follow-up") and visit.visible("plan")
    assert visit.count("research-item") == 4
    assert visit.count("wake-badge") == 3  # the new run, the approval, the timer: one badge each
    assert visit.count("sandbox-card") == 3
    assert visit.attribute("sandbox-count", "data-value") == "0"
    assert page.locator("[data-testid=sandbox-count]").inner_text().strip() == "0"
    assert visit.count("approve-button") == 0 and visit.count("stop-sandbox-button") == 0
    link = page.locator("[data-testid=temporal-ui-link]")
    assert link.evaluate("(a) => a.tagName") == "A"
    assert link.get_attribute("href") == "/temporal-ui/namespaces/default/workflows/research-done"
    assert link.get_attribute("target") == "_blank"
    assert visit.errors == []


def test_the_approval_wait_shows_the_plan_and_no_sandbox(visit: Visit) -> None:
    visit.login()
    visit.open_stage("approval")
    page = visit.page
    page.wait_for_selector('[data-testid=phase][data-value="awaiting_approval"]')
    page.wait_for_selector("[data-testid=approve-button]:visible")
    assert visit.visible("reject-button")
    assert visit.attribute("sandbox-count", "data-value") == "0"
    assert visit.count("wake-badge") == 1
    assert not visit.visible("report") and not visit.visible("follow-up")
    assert page.locator("[data-testid=plan] ol textarea").count() == 4
    # Sending the decision locks the plan at once, before the next poll.
    page.locator("[data-testid=plan] ol textarea").nth(0).fill("An edited sub-question")
    page.click("[data-testid=approve-button]")
    page.wait_for_selector("[data-testid=approve-button]", state="detached")
    assert "An edited sub-question" in page.locator("[data-testid=plan]").inner_text()
    assert visit.attribute("phase", "data-value") == "awaiting_approval"  # the workflow's own word
    assert visit.errors == []


def test_a_wake_in_progress_is_shown_without_a_badge(visit: Visit) -> None:
    visit.login()
    visit.open_stage("waking")
    page = visit.page
    page.wait_for_selector(".tl.waking")
    assert visit.count("wake-badge") == 1  # only the completed wake carries the badge
    assert visit.count("approve-button") == 0  # the decision is already in Temporal's history
    assert "waking Foundry" in page.locator("#count-line").inner_text()
    assert visit.attribute("sandbox-count", "data-value") == "0"


def test_working_sandboxes_offer_the_presenter_actions(visit: Visit) -> None:
    visit.login()
    visit.open_stage("researching")
    page = visit.page
    page.wait_for_selector('[data-testid=phase][data-value="researching"]')
    page.wait_for_selector("[data-testid=crash-worker-button]")
    assert visit.count("crash-worker-button") == 1
    # The hard stop is offered only when TFD_SHOW_STOP_BUTTON is set: a stopped sandbox
    # takes far longer to recover from than a crashed worker.
    assert visit.count("stop-sandbox-button") == 0
    assert visit.attribute("sandbox-count", "data-value") == "1"
    assert visit.count("research-item") == 4
    assert not visit.visible("report")


def test_a_sandbox_running_a_step_reads_working_before_it_reports(visit: Visit) -> None:
    # A worker reports what it is doing every 15 s. Temporal's record of the run is current,
    # so a sandbox with a step running on it must not read "idle, about to be released".
    visit.login()
    visit.open_stage("unreported")
    page = visit.page
    page.wait_for_selector('[data-testid=phase][data-value="researching"]')
    page.wait_for_selector("[data-testid=sandbox-card] .chip.working")
    assert "1 working" in page.locator("#count-line").inner_text()


def test_the_count_is_blank_until_the_server_has_answered(browser: Any, base_url: str) -> None:
    # The recorder and the booth both read this number; "0" before any answer would be a claim.
    context = browser.new_context()
    page = context.new_page()
    page.goto(base_url)
    assert page.locator("[data-testid=sandbox-count]").get_attribute("data-value") == ""
    context.close()


def test_a_failed_run_says_why(visit: Visit) -> None:
    visit.login()
    visit.open_stage("failed")
    page = visit.page
    page.wait_for_selector('[data-testid=phase][data-status="FAILED"]')
    assert "over its quota" in page.locator("#status-line").inner_text()
    assert visit.errors == []


def test_the_page_asks_only_its_own_server(visit: Visit) -> None:
    visit.login()
    visit.open_stage("done")
    visit.page.wait_for_timeout(1500)
    assert visit.requests, "nothing was requested"
    foreign = [url for url in visit.requests if not url.startswith(visit.base)]
    assert foreign == []


def test_a_wrong_code_is_refused_and_the_page_says_so(visit: Visit) -> None:
    page = visit.page
    page.goto(visit.base)
    page.wait_for_selector("[data-testid=access-code]:visible")
    page.fill("[data-testid=access-code]", "not-the-code")
    page.click("[data-testid=login-button]")
    page.wait_for_function("() => document.getElementById('login-error').textContent.length > 0")
    assert not page.locator("[data-testid=question-input]").is_visible()
    assert page.evaluate("() => fetch('/api/runs').then((response) => response.status)") == 401


def test_scrolling_a_section_to_the_top_clears_the_pinned_bar(visit: Visit) -> None:
    # The recorder scrolls with scrollIntoView; the pinned run bar must not cover what it shows.
    visit.login()
    visit.open_stage("done")
    page = visit.page
    page.wait_for_selector("[data-testid=follow-up]:visible")
    page.wait_for_timeout(400)
    for testid in ["report", "follow-up", "research-item", "plan"]:
        target = page.get_by_test_id(testid).first
        target.evaluate("(el) => el.scrollIntoView({ behavior: 'instant', block: 'start' })")
        top = target.evaluate("(el) => el.getBoundingClientRect().top")
        bar = page.evaluate("() => document.getElementById('run-bar').getBoundingClientRect().bottom")
        assert top >= bar - 1, (testid, top, bar)
    # Only the work column scrolls: the header, the count and the timeline stay where they are.
    assert page.evaluate("() => document.scrollingElement.scrollTop + document.body.scrollTop") == 0
    assert page.evaluate("() => document.querySelector('header.top').getBoundingClientRect().top") == 0


def test_long_sub_questions_wrap_on_a_laptop(browser: Any, base_url: str) -> None:
    current = Visit(browser, base_url)
    try:
        current.page.set_viewport_size({"width": 1280, "height": 720})
        current.login()
        current.open_stage("approval")
        current.page.wait_for_selector("[data-testid=approve-button]:visible")
        current.page.wait_for_timeout(300)
        clipped = current.page.evaluate(
            "() => [...document.querySelectorAll('[data-testid=plan] ol textarea')]"
            ".filter((area) => area.scrollHeight > area.clientHeight + 2 || area.scrollWidth > area.clientWidth + 2).length"
        )
        assert clipped == 0
    finally:
        current.close()


def test_a_request_foundry_refused_is_not_shown_as_done(visit: Visit) -> None:
    visit.login()
    visit.open_stage("refused")
    page = visit.page
    page.wait_for_selector("[data-testid=sandbox-card] .problem")
    card = page.locator("[data-testid=sandbox-card]", has=page.locator(".problem")).inner_text()
    assert "crash request failed (HTTP 403)" in card
    assert "stop failed (HTTP 403)" in card
    assert "worker crashed" not in card
    # The sandbox is still up, so it still counts and can be asked again.
    assert visit.attribute("sandbox-count", "data-value") == "1"
    assert visit.count("crash-worker-button") == 1


def test_a_lost_connection_is_shown_and_mends_itself(visit: Visit) -> None:
    visit.login()
    visit.open_stage("researching")
    page = visit.page
    assert page.locator("body").get_attribute("data-offline") in (None, "0")
    visit.context.set_offline(True)
    page.wait_for_selector('body[data-offline="1"]')
    visit.context.set_offline(False)
    page.wait_for_selector('body[data-offline="0"]')


def test_a_failed_read_is_reported_and_then_forgotten(visit: Visit) -> None:
    visit.login()
    visit.open_stage("researching")
    page = visit.page
    target = "**/api/runs/research-researching"
    page.route(
        target,
        lambda route: route.fulfill(
            status=502, content_type="application/json", body='{"error": "the console could not reach Temporal"}'
        ),
    )
    page.wait_for_function(
        "() => document.getElementById('run-error').textContent.includes('could not reach Temporal')"
    )
    # The sandbox reads still work; the failing read keeps the notice up regardless.
    page.wait_for_timeout(1500)
    assert page.locator("body").get_attribute("data-offline") == "1"
    page.unroute(target)
    page.wait_for_function("() => document.getElementById('run-error').textContent === ''")
    page.wait_for_selector('body[data-offline="0"]')
