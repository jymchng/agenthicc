"""Deterministic validation evidence and the authoring verdict."""

from __future__ import annotations

from dataclasses import asdict, dataclass
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

    def to_dict(self) -> dict[str, object]:
        return {"digest": self.digest, "findings": [asdict(item) for item in self.findings]}

    @classmethod
    def from_payload(cls, value: object) -> ValidationReport:
        if not isinstance(value, dict):
            raise ValueError("Invalid authoring validation report.")
        digest = value.get("digest")
        items = value.get("findings")
        if not isinstance(digest, str) or not isinstance(items, list):
            raise ValueError("Invalid authoring validation report.")
        findings: list[Finding] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid authoring finding.")
            code, severity, path = item.get("code"), item.get("severity"), item.get("path")
            line, message = item.get("line"), item.get("message")
            if (
                not isinstance(code, str)
                or severity not in ("error", "warning")
                or not isinstance(path, str)
                or (line is not None and type(line) is not int)
                or not isinstance(message, str)
            ):
                raise ValueError("Invalid authoring finding.")
            findings.append(
                Finding(code, "error" if severity == "error" else "warning", path, line, message)
            )
        return cls(digest, tuple(findings))


def decide(report: ValidationReport, agent_action: _Action) -> _Action:
    """Approve only when both deterministic validation and agent review pass."""
    if agent_action == "approve" and report.errors == 0:
        return "approve"
    return "reject"
