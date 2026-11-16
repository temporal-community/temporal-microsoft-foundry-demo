"""Workflow inputs and outputs, shared by the worker and the scripts that start workflows."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProbeRequest:
    """Steps of the probe workflow, run in this order; a zero or False skips the step."""

    busy_seconds: int = 0
    wait_for_signal: bool = False
    timer_seconds: int = 0
    fan_out: int = 0
    fan_out_seconds: int = 5


@dataclass
class Stamp:
    """Which worker ran a stage, and when."""

    stage: str
    worker: str
    at: str
    attempt: int = 1


@dataclass
class ProbeResult:
    stamps: list[Stamp] = field(default_factory=list)


@dataclass
class ReportRequest:
    subject: str
    follow_up_after_seconds: int = 300


@dataclass
class Decision:
    approved: bool
    note: str = ""


@dataclass
class ReportResult:
    subject: str
    report: str
    approved: bool
    decision_note: str = ""
    follow_up: str = ""


@dataclass
class ResearchRequest:
    question: str
    max_subquestions: int = 4
    follow_up_after_seconds: int = 0
    auto_approve: bool = False


@dataclass
class ResearchDecision:
    approved: bool
    note: str = ""
    subquestions: list[str] | None = None  # replaces the planner's list when given


@dataclass
class ResearchResult:
    question: str
    approved: bool
    plan: dict = field(default_factory=dict)
    research: list[dict] = field(default_factory=list)
    report: str = ""
    follow_up: str = ""
