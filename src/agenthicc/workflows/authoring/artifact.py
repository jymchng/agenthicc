"""Workflow-authoring artifacts and static validation (PRD-147)."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass

__all__ = [
    "AuthoringArtifact",
    "AuthoringResumeContext",
    "AuthoringResult",
    "ValidationFinding",
    "ValidationReport",
    "WorkflowCandidate",
    "parse_workflow_response",
    "validate_workflow_candidate",
]

MAX_WORKFLOW_SOURCE_BYTES = 100_000
_WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
_WORKFLOW_BLOCK_RE = re.compile(r"<workflow\b([^>]*)>(.*?)</workflow>", re.IGNORECASE | re.DOTALL)
_NAME_ATTR_RE = re.compile(r"\bname\s*=\s*(['\"])(.*?)\1", re.DOTALL)
_DESCRIPTION_ATTR_RE = re.compile(r"\bdescription\s*=\s*(['\"])(.*?)\1", re.DOTALL)


@dataclass(frozen=True)
class WorkflowCandidate:
    """One workflow source candidate returned by the authoring agent."""

    name: str
    code: str
    description: str = ""


@dataclass(frozen=True)
class ValidationFinding:
    """One deterministic finding from workflow source validation."""

    code: str
    message: str
    blocking: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Static validation result for a workflow candidate."""

    findings: tuple[ValidationFinding, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(item.blocking for item in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "findings": [item.to_dict() for item in self.findings],
        }


@dataclass(frozen=True)
class AuthoringArtifact:
    """Published or staged artifact metadata returned to callers."""

    name: str
    state: str
    staged_path: str
    published_path: str | None
    sha256: str
    validation: ValidationReport
    manifest_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state,
            "staged_path": self.staged_path,
            "published_path": self.published_path,
            "sha256": self.sha256,
            "validation": self.validation.to_dict(),
            "manifest_path": self.manifest_path,
        }


@dataclass(frozen=True)
class AuthoringResumeContext:
    """Durable identity used to resume an interrupted authoring run."""

    run_id: str
    intent: str = ""


@dataclass(frozen=True)
class AuthoringResult:
    """JSON-safe result of one ``create_workflow`` run."""

    workflow: str
    run_id: str
    status: str
    artifact: AuthoringArtifact | None = None
    approval: str = "not-requested"
    activation: str | None = None
    error: str | None = None
    attempts: int = 0
    artifact_kind: str = "workflow"
    artifacts: tuple[AuthoringArtifact, ...] = ()

    def __post_init__(self) -> None:
        if self.artifact is not None and not self.artifacts:
            object.__setattr__(self, "artifacts", (self.artifact,))

    @property
    def fail_reason(self) -> str:
        """Compatibility field consumed by the headless workflow adapter."""

        return self.error or ""

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow": self.workflow,
            "run_id": self.run_id,
            "status": self.status,
            "artifact": self.artifact.to_dict() if self.artifact is not None else None,
            "approval": self.approval,
            "activation": self.activation,
            "error": self.error,
            "attempts": self.attempts,
            "artifact_kind": self.artifact_kind,
            "artifacts": [item.to_dict() for item in self.artifacts],
        }


def _extract_literal_string(node: ast.AST) -> str | None:
    value: object
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _class_attribute(node: ast.ClassDef, name: str) -> str | None:
    for statement in node.body:
        targets: list[ast.expr] = []
        value: ast.expr | None
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return _extract_literal_string(value) if value is not None else None
    return None


def _func_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _phase_specs(node: ast.ClassDef) -> tuple[list[dict[str, object]], list[ValidationFinding]]:
    findings: list[ValidationFinding] = []
    phase_node: ast.AST | None = None
    for statement in node.body:
        targets: list[ast.expr] = []
        value: ast.expr | None
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "phases" for target in targets):
            phase_node = value
            break

    if not isinstance(phase_node, (ast.List, ast.Tuple)):
        return [], [ValidationFinding("phases-missing", "Workflow phases must be a list or tuple.")]
    if not phase_node.elts:
        return [], [ValidationFinding("phases-empty", "Workflow phases must not be empty.")]

    phases: list[dict[str, object]] = []
    for item in phase_node.elts:
        if not isinstance(item, ast.Call) or _func_name(item.func) != "PhaseSpec":
            findings.append(
                ValidationFinding(
                    "phase-invalid",
                    "Every workflow phase must be a PhaseSpec(...) call.",
                )
            )
            continue
        values: dict[str, object] = {}
        if item.args:
            values["name"] = _extract_literal_string(item.args[0])
            if len(item.args) > 1:
                findings.append(
                    ValidationFinding(
                        "phase-positional",
                        "PhaseSpec accepts only its name as a positional argument; use keywords for the rest.",
                    )
                )
        for keyword in item.keywords:
            if keyword.arg in {"name", "next", "on_reject"}:
                values[keyword.arg] = (
                    _extract_literal_string(keyword.value)
                    if keyword.arg != "next" or not isinstance(keyword.value, ast.Constant)
                    else keyword.value.value
                )
            elif keyword.arg == "parallel_with" and isinstance(
                keyword.value, (ast.List, ast.Tuple)
            ):
                values[keyword.arg] = [
                    _extract_literal_string(element) for element in keyword.value.elts
                ]
        phase_name = values.get("name")
        if not isinstance(phase_name, str) or not phase_name:
            findings.append(
                ValidationFinding("phase-name-missing", "Every PhaseSpec needs a name.")
            )
            continue
        phases.append(values)

    names = [str(item["name"]) for item in phases]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        findings.append(
            ValidationFinding("phase-duplicate", f"Duplicate phase names: {', '.join(duplicates)}.")
        )
    known = set(names)
    for phase in phases:
        for field_name in ("next", "on_reject"):
            target = phase.get(field_name)
            if target is not None and (not isinstance(target, str) or target not in known):
                findings.append(
                    ValidationFinding(
                        "phase-reference",
                        f"Phase {phase['name']!r} references unknown {field_name} {target!r}.",
                    )
                )
        peers = phase.get("parallel_with", [])
        if isinstance(peers, list) and any(peer not in known for peer in peers):
            findings.append(
                ValidationFinding(
                    "parallel-reference",
                    f"Phase {phase['name']!r} references an unknown parallel phase.",
                )
            )
    return phases, findings


def _candidate_from_source(name: str, code: str, description: str) -> WorkflowCandidate:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return WorkflowCandidate(name=name, code=code, description=description)
    if not name:
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_name = _class_attribute(node, "name")
                if class_name:
                    name = class_name
                    break
    return WorkflowCandidate(name=name, code=code, description=description)


def parse_workflow_response(text: str) -> WorkflowCandidate:
    """Parse the strict workflow envelope returned by the authoring agent.

    Accepted forms are a JSON object containing ``name`` and ``code``, or a
    ``<workflow name="...">`` block containing a Python fenced code block.
    A plain Python response is accepted as a compatibility fallback when its
    class-level workflow name can be recovered by static validation.
    """

    raw = text.strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    block = _WORKFLOW_BLOCK_RE.search(raw)
    if block:
        candidates.insert(0, block.group(2).strip())

    for candidate_text in candidates:
        json_text = candidate_text
        fenced_json = re.search(r"```json\s*\n(.*?)```", candidate_text, re.IGNORECASE | re.DOTALL)
        if fenced_json:
            json_text = fenced_json.group(1).strip()
        try:
            value = json.loads(json_text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            code_value = value.get("code")
            if not isinstance(code_value, str):
                continue
            raw_name = value.get("name")
            name = raw_name if isinstance(raw_name, str) else ""
            raw_description = value.get("description")
            description = raw_description if isinstance(raw_description, str) else ""
            return _candidate_from_source(name.strip(), code_value.strip(), description.strip())

    code_match = _FENCE_RE.search(block.group(2) if block else raw)
    code = code_match.group(1).strip() if code_match else ""
    if not code and ("WorkflowPlugin" in raw or "PhaseSpec" in raw):
        code = raw
    if not code:
        raise ValueError("authoring response did not contain workflow Python source")

    name = ""
    description = ""
    if block:
        name_match = _NAME_ATTR_RE.search(block.group(1))
        description_match = _DESCRIPTION_ATTR_RE.search(block.group(1))
        name = name_match.group(2).strip() if name_match else ""
        description = description_match.group(2).strip() if description_match else ""
    return _candidate_from_source(name, code, description)


def validate_workflow_candidate(candidate: WorkflowCandidate) -> ValidationReport:
    """Validate a candidate without importing or executing its Python."""

    findings: list[ValidationFinding] = []
    name = candidate.name
    if not _WORKFLOW_NAME_RE.fullmatch(name):
        findings.append(
            ValidationFinding(
                "workflow-name",
                "Workflow name must start with a letter and contain only lowercase "
                "letters, digits, and underscores (2-64 characters).",
            )
        )
    source = candidate.code.strip()
    if not source:
        findings.append(ValidationFinding("source-empty", "Workflow source is empty."))
        return ValidationReport(tuple(findings))
    if len(source.encode("utf-8")) > MAX_WORKFLOW_SOURCE_BYTES:
        findings.append(ValidationFinding("source-too-large", "Workflow source exceeds 100 KiB."))
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        findings.append(ValidationFinding("syntax", f"Workflow source is not valid Python: {exc}."))
        return ValidationReport(tuple(findings))

    dangerous_names = {"eval", "exec", "compile", "__import__"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in dangerous_names
        ):
            findings.append(
                ValidationFinding(
                    "unsafe-call",
                    f"Workflow source may not call {node.func.id}() directly.",
                )
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr == "system"
            ):
                findings.append(
                    ValidationFinding("unsafe-call", "Workflow source may not call os.system().")
                )
        if isinstance(node, ast.Import):
            if any(alias.name in {"subprocess", "ctypes"} for alias in node.names):
                findings.append(
                    ValidationFinding(
                        "unsafe-import", "Workflow source may not import subprocess or ctypes."
                    )
                )
        if isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "ctypes"}:
            findings.append(
                ValidationFinding("unsafe-import", f"Workflow source may not import {node.module}.")
            )

    plugin_classes: list[ast.ClassDef] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = {_func_name(base) for base in node.bases}
            if "WorkflowPlugin" in bases:
                plugin_classes.append(node)
    if not plugin_classes:
        findings.append(
            ValidationFinding("plugin-class", "Source must define a WorkflowPlugin subclass.")
        )
        return ValidationReport(tuple(findings))
    if len(plugin_classes) != 1:
        findings.append(
            ValidationFinding(
                "plugin-count", "Source must define exactly one WorkflowPlugin subclass."
            )
        )

    plugin = plugin_classes[0]
    declared_name = _class_attribute(plugin, "name")
    if declared_name != name:
        findings.append(
            ValidationFinding(
                "workflow-name-mismatch",
                f"Envelope name {name!r} does not match class name {declared_name!r}.",
            )
        )
    _phases, phase_findings = _phase_specs(plugin)
    findings.extend(phase_findings)
    if not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "agenthicc.workflows.plugin"
        and any(alias.name == "WorkflowPlugin" for alias in node.names)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    ):
        findings.append(
            ValidationFinding(
                "plugin-import",
                "Source must import WorkflowPlugin from agenthicc.workflows.plugin.",
            )
        )
    return ValidationReport(tuple(findings))


def source_sha256(source: str) -> str:
    """Return the stable digest used by staged-artifact publication."""

    return hashlib.sha256(source.encode("utf-8")).hexdigest()
