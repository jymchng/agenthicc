"""Deterministic validation evidence and the authoring verdict."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Finding", "FindingSeverity", "ValidationReport", "decide"]

FindingSeverity = Literal["error", "warning"]
_Action = Literal["approve", "reject"]


@dataclass(frozen=True, slots=True)
class Finding:
    """A bounded diagnostic about one candidate or its complete set."""

    code: str
    severity: FindingSeverity
    path: str
    line: int | None
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The validated candidate digest and findings that govern approval."""

    digest: str
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> int:
        return sum(finding.severity == "error" for finding in self.findings)


def decide(report: ValidationReport, agent_action: _Action) -> _Action:
    """Approve only when both deterministic validation and agent review pass."""
    if agent_action == "approve" and report.errors == 0:
        return "approve"
    return "reject"
