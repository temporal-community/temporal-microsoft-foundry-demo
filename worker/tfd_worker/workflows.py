"""Workflows: deep research with a human in the loop, plus probes for the wake paths."""

import asyncio
import re
from datetime import timedelta
from typing import Any

from agent_framework import Agent
from pydantic import BaseModel, Field
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.microsoft_agent_framework import TemporalChatClient

with workflow.unsafe.imports_passed_through():
    from .activities import busy, exit_process, where
    from .settings import MODEL_NAME
    from .shared import (
        Decision,
        ProbeRequest,
        ProbeResult,
        ReportRequest,
        ReportResult,
        ResearchDecision,
        ResearchRequest,
        ResearchResult,
        Stamp,
    )

# No schedule-to-start timeout anywhere: a task may wait for a sandbox to start.
QUICK = {"start_to_close_timeout": timedelta(seconds=30), "heartbeat_timeout": None}
LONG = {
    "start_to_close_timeout": timedelta(minutes=30),
    "heartbeat_timeout": timedelta(seconds=10),
}


def _note(**fields: Any) -> None:
    """Publish progress in the memo, readable with no worker running."""
    workflow.upsert_memo(dict(fields))


@workflow.defn
class ProbeWorkflow:
    """Needs a worker at several separate moments: start, a Signal, a timer, a fan-out."""

    def __init__(self) -> None:
        self._go = False

    @workflow.signal
    def go(self) -> None:
        self._go = True

    @workflow.run
    async def run(self, request: ProbeRequest) -> ProbeResult:
        stamps: list[Stamp] = [await workflow.execute_activity(where, "start", **QUICK)]
        if request.busy_seconds:
            _note(phase="busy")
            stamps.append(
                await workflow.execute_activity(
                    busy, args=["busy", request.busy_seconds], **LONG
                )
            )
        if request.wait_for_signal:
            _note(phase="waiting_for_signal")
            await workflow.wait_condition(lambda: self._go)
            stamps.append(await workflow.execute_activity(where, "after-signal", **QUICK))
        if request.timer_seconds:
            _note(phase="waiting_for_timer")
            await workflow.sleep(request.timer_seconds)
            stamps.append(await workflow.execute_activity(where, "after-timer", **QUICK))
        if request.fan_out:
            _note(phase="fan_out")
            stamps.extend(
                await asyncio.gather(
                    *(
                        workflow.execute_activity(
                            busy, args=[f"fan-out-{index}", request.fan_out_seconds], **LONG
                        )
                        for index in range(request.fan_out)
                    )
                )
            )
        _note(phase="completed")
        return ProbeResult(stamps=stamps)


@workflow.defn
class FaultProbeWorkflow:
    """One Activity whose first attempt kills its worker process (when faults are enabled)."""

    @workflow.run
    async def run(self) -> ProbeResult:
        first = await workflow.execute_activity(where, "before-fault", **QUICK)
        second = await workflow.execute_activity(
            exit_process,
            "after-fault",
            start_to_close_timeout=timedelta(seconds=20),
        )
        return ProbeResult(stamps=[first, second])


def _chat(summary: str) -> TemporalChatClient:
    # One client per agent so each model Activity names its agent in the UI.
    return TemporalChatClient(
        MODEL_NAME, start_to_close_timeout=timedelta(minutes=3), summary=summary
    )


@workflow.defn
class ReportFollowUpWorkflow:
    """A report agent, a human approval, a durable wait, then a follow-up agent.

    Both agents run inside the Workflow. Each model call is an Activity, so the Workflow needs
    a worker three separate times, with nothing running in between.
    """

    def __init__(self) -> None:
        self._decision: Decision | None = None

    @workflow.signal
    def decide(self, decision: Decision) -> None:
        if self._decision is None:
            self._decision = decision

    @workflow.run
    async def run(self, request: ReportRequest) -> ReportResult:
        _note(phase="writing_report")
        reporter = Agent(
            client=_chat("report"),
            name="report",
            instructions=(
                "Write a short briefing on the subject you are given, from your own "
                "knowledge: three to five tight paragraphs. Say which facts should be "
                "verified, and end with two concrete checks to run later."
            ),
        )
        report = (await reporter.run(f"Subject: {request.subject}")).text

        _note(phase="awaiting_approval", report=report)
        await workflow.wait_condition(lambda: self._decision is not None)
        decision = self._decision
        assert decision is not None
        if not decision.approved:
            _note(phase="rejected")
            return ReportResult(
                subject=request.subject,
                report=report,
                approved=False,
                decision_note=decision.note,
            )

        _note(phase="waiting_for_follow_up")
        await workflow.sleep(
            timedelta(seconds=request.follow_up_after_seconds), summary="Wait before the follow-up"
        )

        _note(phase="writing_follow_up")
        follower = Agent(
            client=_chat("follow-up"),
            name="follow-up",
            instructions=(
                "Time has passed since the briefing below was written and approved. Write a "
                "brief follow-up for its owner: revisit each check it listed and recommend one "
                "to three next actions. Do not claim that any check was run."
            ),
        )
        prompt = f"Briefing:\n{report}"
        if decision.note:
            prompt += f"\n\nThe approver's note: {decision.note}"
        follow_up = (await follower.run(prompt)).text
        _note(phase="completed", follow_up=follow_up)
        return ReportResult(
            subject=request.subject,
            report=report,
            approved=True,
            decision_note=decision.note,
            follow_up=follow_up,
        )


# ---------------------------------------------------------------------------------------
# Deep research
# ---------------------------------------------------------------------------------------

MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=6,
)
# Low reasoning effort keeps each model call to a few seconds, which suits a live audience.
FAST = {"reasoning": {"effort": "low"}}
# Foundry's hosted web search: the provider runs the search inside the model call.
WEB_SEARCH = {"type": "web_search"}

PLANNER = (
    "You plan research. Given a question, write one sentence on how you will approach it "
    "and a short list of focused sub-questions that together answer it. Each sub-question "
    "is one plain sentence of at most 18 words, answerable with a web search, and does not "
    "overlap with the others."
)
RESEARCHER = (
    "You research one sub-question with web search. Search first. Then write three to five "
    "bullet points of findings. Each bullet is one or two sentences and ends with its source "
    "as a markdown link, like [Microsoft Learn](https://example.com/page). Prefer primary "
    "sources from the last twelve months, give dates, and say so when sources disagree. No "
    "introduction and no conclusion."
)
WRITER = (
    "You write a research brief from the findings you are given, and only from them. "
    "Markdown, 250 to 320 words. Start with a short title as a level-one heading (# Title), "
    "then a two-sentence answer with no label in front of it, then three short sections of "
    "evidence under level-two headings, then a level-two heading Open questions with two or "
    "three bullet points. Cite sources as markdown links, like "
    "[Microsoft Learn](https://example.com/page), never as bare URLs."
)
FOLLOW_UP = (
    "Time has passed since the brief below was written. Search the web for anything new on "
    "its open questions. Write an update of at most 120 words in markdown, with no heading: "
    "one short paragraph on what changed, with sources as markdown links, or saying that "
    "nothing material changed, then one or two next actions as bullet points."
)


class Plan(BaseModel):
    summary: str = Field(description="One sentence on how the question will be approached")
    subquestions: list[str] = Field(description="Focused, non-overlapping sub-questions")


def _agent_client(summary: str) -> TemporalChatClient:
    return TemporalChatClient(
        MODEL_NAME,
        start_to_close_timeout=timedelta(minutes=3),
        retry_policy=MODEL_RETRY,
        summary=summary,
    )


_LINK = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^)\s]+)\)")


def _with_sources(report: str, findings: list[str], limit: int = 8) -> str:
    """Make sure the brief shows where it came from.

    The writer is told to keep the researchers' links and usually does. When it does not,
    list the sources the researchers cited under the brief.
    """
    if len(_LINK.findall(report)) >= 3:
        return report
    seen: dict[str, str] = {}
    for text in findings:
        for label, url in _LINK.findall(text):
            seen.setdefault(url, label)
    if not seen:
        return report
    lines = [f"- [{label}]({url})" for url, label in list(seen.items())[:limit]]
    return report.rstrip() + "\n\n## Sources\n\n" + "\n".join(lines)


def _clean(questions: list[str], limit: int) -> list[str]:
    cleaned = [question.strip() for question in questions if question and question.strip()]
    return cleaned[: max(1, limit)]


@workflow.defn
class DeepResearchWorkflow:
    """Plan, wait for a person, research in parallel, write, and optionally follow up later.

    Every agent runs inside the Workflow; each model call is an Activity. While the Workflow
    waits for the approval or for the follow-up timer, nothing needs to be running.
    """

    def __init__(self) -> None:
        self._decision: ResearchDecision | None = None
        self._research: list[dict[str, Any]] = []

    @workflow.signal
    def decide(self, decision: ResearchDecision) -> None:
        if self._decision is None:
            self._decision = decision

    async def _plan(self, request: ResearchRequest) -> dict[str, Any]:
        planner = Agent(client=_agent_client("planner"), name="planner", instructions=PLANNER)
        response = await planner.run(
            f"Question: {request.question}\n"
            f"Use at most {request.max_subquestions} sub-questions.",
            options={"response_format": Plan, **FAST},
        )
        plan = response.value
        if isinstance(plan, Plan):
            summary, questions = plan.summary, plan.subquestions
        else:  # a model that ignored the schema: one line per sub-question
            summary, questions = "", [line.lstrip("-*0123456789. ") for line in response.text.splitlines()]
        return {
            "summary": summary,
            "subquestions": _clean(questions, request.max_subquestions) or [request.question],
        }

    async def _research_one(self, index: int, question: str) -> None:
        item = self._research[index]
        researcher = Agent(
            client=_agent_client(f"researcher {index + 1}"),
            name=f"researcher-{index + 1}",
            instructions=RESEARCHER,
            tools=[WEB_SEARCH],
        )
        try:
            response = await researcher.run(
                f"Overall question: {question}\nYour sub-question: {item['subquestion']}",
                options=FAST,
            )
            item.update(status="done", findings=response.text)
        except Exception as error:  # one failed researcher must not sink the brief
            item.update(status="failed", error=str(error)[:300])
        _note(research=self._research)

    @workflow.run
    async def run(self, request: ResearchRequest) -> ResearchResult:
        _note(phase="planning", question=request.question)
        plan = await self._plan(request)

        if request.auto_approve:
            decision = ResearchDecision(approved=True)
        else:
            _note(phase="awaiting_approval", plan=plan)
            await workflow.wait_condition(lambda: self._decision is not None)
            assert self._decision is not None
            decision = self._decision
            if decision.subquestions:
                plan["subquestions"] = _clean(decision.subquestions, 8) or plan["subquestions"]
        _note(plan=plan, decision={"approved": decision.approved, "note": decision.note})
        if not decision.approved:
            _note(phase="rejected")
            return ResearchResult(question=request.question, approved=False, plan=plan)

        self._research = [
            {"index": index, "subquestion": question, "status": "running", "findings": "", "error": ""}
            for index, question in enumerate(plan["subquestions"])
        ]
        _note(phase="researching", research=self._research)
        await asyncio.gather(
            *(self._research_one(index, request.question) for index in range(len(self._research)))
        )

        _note(phase="writing")
        findings = "\n\n".join(
            f"## {item['subquestion']}\n{item['findings']}"
            for item in self._research
            if item["status"] == "done"
        )
        if not findings:
            raise ApplicationError("Every researcher failed.", non_retryable=True)
        writer = Agent(client=_agent_client("writer"), name="writer", instructions=WRITER)
        prompt = f"Question: {request.question}\n\nFindings:\n{findings}"
        if decision.note:
            prompt += f"\n\nThe reviewer who approved the plan added: {decision.note}"
        report = (await writer.run(prompt, options=FAST)).text
        report = _with_sources(
            report, [item["findings"] for item in self._research if item["status"] == "done"]
        )
        _note(report=report)

        follow_up = ""
        if request.follow_up_after_seconds > 0:
            wait = timedelta(seconds=request.follow_up_after_seconds)
            _note(
                phase="waiting_for_follow_up",
                follow_up_due=(workflow.now() + wait).isoformat(timespec="seconds"),
            )
            await workflow.sleep(wait, summary="Wait before the follow-up")
            _note(phase="writing_follow_up")
            follower = Agent(
                client=_agent_client("follow-up"),
                name="follow-up",
                instructions=FOLLOW_UP,
                tools=[WEB_SEARCH],
            )
            follow_up = (await follower.run(f"Brief:\n{report}", options=FAST)).text
            _note(follow_up=follow_up)

        _note(phase="completed")
        return ResearchResult(
            question=request.question,
            approved=True,
            plan=plan,
            research=self._research,
            report=report,
            follow_up=follow_up,
        )
