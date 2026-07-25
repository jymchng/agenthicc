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
    "parse_authoring_response",
    "parse_workflow_response",
    "validate_command_candidate",
    "validate_tool_candidate",
    "validate_workflow_candidate",
]

MAX_WORKFLOW_SOURCE_BYTES = 100_000
_WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
_AUTHORING_BLOCK_RE = re.compile(
    r"<(workflow|tool|command)\b([^>]*)>(.*?)</\1>", re.IGNORECASE | re.DOTALL
)
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
            "path": self.published_path or self.staged_path,
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
    """JSON-safe result of one authoring workflow run."""

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
    summary: str = ""

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
            "summary": self.summary,
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


def _class_method(node: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return one method declared directly on *node*."""

    for statement in node.body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name == name
        ):
            return statement
    return None


def _has_call_to(node: ast.AST, owner: str, method: str) -> bool:
    """Return whether an AST subtree calls ``owner.method(...)``."""

    for item in ast.walk(node):
        if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Attribute):
            continue
        value = item.func.value
        if isinstance(value, ast.Name) and value.id == owner:
            return item.func.attr == method
        if (
            owner == "super"
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "super"
        ):
            return item.func.attr == method
    return False


def _validate_custom_runner(
    tree: ast.Module,
    plugin: ast.ClassDef,
    findings: list[ValidationFinding],
) -> None:
    """Require generated workflows to own an explicit runner/context boundary."""

    has_runner_import = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "agenthicc.workflows.default.runner"
        and any(alias.name == "WorkflowRunner" for alias in node.names)
        for node in tree.body
    )
    if not has_runner_import:
        findings.append(
            ValidationFinding(
                "runner-import",
                "Workflow source must import WorkflowRunner from "
                "agenthicc.workflows.default.runner.",
            )
        )

    runner_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and "WorkflowRunner" in {_func_name(base) for base in node.bases}
    ]
    if not runner_classes:
        findings.append(
            ValidationFinding(
                "runner-class",
                "Workflow source must define a custom WorkflowRunner subclass.",
            )
        )
        return
    if len(runner_classes) != 1:
        findings.append(
            ValidationFinding(
                "runner-count",
                "Workflow source must define exactly one custom WorkflowRunner subclass.",
            )
        )

    runner = runner_classes[0]
    run_method = _class_method(runner, "run")
    resume_method = _class_method(runner, "resume")
    if not isinstance(run_method, ast.AsyncFunctionDef) or not isinstance(
        resume_method, ast.AsyncFunctionDef
    ):
        findings.append(
            ValidationFinding(
                "runner-methods",
                "Custom WorkflowRunner must override async run() and async resume().",
            )
        )
    elif not (
        _has_call_to(run_method, "super", "run") and _has_call_to(resume_method, "super", "resume")
    ):
        findings.append(
            ValidationFinding(
                "runner-context",
                "Custom runner run()/resume() must preserve WorkflowRunner context "
                "by delegating to super().",
            )
        )

    factory = _class_method(plugin, "build_runner")
    if factory is None:
        findings.append(
            ValidationFinding(
                "runner-factory",
                "WorkflowPlugin must override build_runner() to return its custom runner.",
            )
        )
        return
    runner_name = runner.name
    if not any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == runner_name
        for item in ast.walk(factory)
    ):
        findings.append(
            ValidationFinding(
                "runner-factory",
                f"build_runner() must construct the custom runner {runner_name}.",
            )
        )


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
            if keyword.arg in {"name", "next", "on_reject", "terminal_wait_policy"}:
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
        policy = values.get("terminal_wait_policy", "foreground")
        if policy not in {"foreground", "background"}:
            findings.append(
                ValidationFinding(
                    "terminal-wait-policy",
                    "terminal_wait_policy must be 'foreground' or 'background'.",
                )
            )
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


def parse_authoring_response(text: str, artifact_kind: str) -> WorkflowCandidate:
    """Parse one strict authoring envelope without importing its source.

    Accepted forms are a JSON object containing ``name`` and ``code``, or a
    ``<workflow>``, ``<tool>``, or ``<command>`` block containing a Python
    fenced code block. A plain Python response remains a compatibility
    fallback; contract validation will reject it when a module name cannot be
    recovered from the envelope.
    """

    if artifact_kind not in {"workflow", "tool", "command"}:
        raise ValueError(f"unsupported authoring artifact kind: {artifact_kind!r}")
    raw = text.strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    block = _AUTHORING_BLOCK_RE.search(raw)
    if block:
        if block.group(1).lower() != artifact_kind:
            raise ValueError(
                f"authoring response envelope kind {block.group(1).lower()!r} "
                f"does not match {artifact_kind!r}"
            )
        candidates.insert(0, block.group(3).strip())

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

    code_match = _FENCE_RE.search(block.group(3) if block else raw)
    code = code_match.group(1).strip() if code_match else ""
    markers = {
        "workflow": ("WorkflowPlugin", "PhaseSpec"),
        "tool": ("TOOLS", "@tool", "from lauren_ai"),
        "command": ("COMMAND", "Command(", "from agenthicc.commands"),
    }[artifact_kind]
    if not code and any(marker in raw for marker in markers):
        code = raw
    if not code:
        raise ValueError(f"authoring response did not contain {artifact_kind} Python source")

    name = ""
    description = ""
    if block:
        name_match = _NAME_ATTR_RE.search(block.group(2))
        description_match = _DESCRIPTION_ATTR_RE.search(block.group(2))
        name = name_match.group(2).strip() if name_match else ""
        description = description_match.group(2).strip() if description_match else ""
    return _candidate_from_source(name, code, description)


def parse_workflow_response(text: str) -> WorkflowCandidate:
    """Parse the strict workflow envelope returned by the authoring agent."""

    return parse_authoring_response(text, "workflow")


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
    _validate_custom_runner(tree, plugin, findings)
    return ValidationReport(tuple(findings))


def _extension_source_findings(
    candidate: WorkflowCandidate, artifact_label: str
) -> tuple[list[ValidationFinding], ast.Module | None]:
    """Return shared static findings and a parsed extension module."""

    findings: list[ValidationFinding] = []
    if not _WORKFLOW_NAME_RE.fullmatch(candidate.name):
        findings.append(
            ValidationFinding(
                "artifact-name",
                f"{artifact_label} name must start with a letter and contain only lowercase "
                "letters, digits, and underscores (2-64 characters).",
            )
        )
    source = candidate.code.strip()
    if not source:
        findings.append(ValidationFinding("source-empty", f"{artifact_label} source is empty."))
        return findings, None
    if len(source.encode("utf-8")) > MAX_WORKFLOW_SOURCE_BYTES:
        findings.append(
            ValidationFinding("source-too-large", f"{artifact_label} source exceeds 100 KiB.")
        )
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        findings.append(
            ValidationFinding("syntax", f"{artifact_label} source is not valid Python: {exc}.")
        )
        return findings, None

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"eval", "exec", "compile", "__import__"}
        ):
            findings.append(
                ValidationFinding(
                    "unsafe-call",
                    f"{artifact_label} source may not call {node.func.id}() directly.",
                )
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr == "system"
        ):
            findings.append(
                ValidationFinding(
                    "unsafe-call", f"{artifact_label} source may not call os.system()."
                )
            )
        if isinstance(node, ast.Import) and any(
            alias.name.split(".", 1)[0] in {"subprocess", "ctypes"} for alias in node.names
        ):
            findings.append(
                ValidationFinding(
                    "unsafe-import",
                    f"{artifact_label} source may not import subprocess or ctypes.",
                )
            )
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".", 1)[0] in {"subprocess", "ctypes"}:
                findings.append(
                    ValidationFinding(
                        "unsafe-import",
                        f"{artifact_label} source may not import {node.module}.",
                    )
                )
    return findings, tree


def _top_level_assignment(tree: ast.Module, name: str) -> ast.expr | None:
    """Return a single top-level assignment value, or ``None`` when absent."""

    value: ast.expr | None = None
    for statement in tree.body:
        targets: list[ast.expr]
        assigned: ast.expr | None
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            assigned = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            assigned = statement.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if value is not None:
                return None
            value = assigned
    return value


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def validate_tool_candidate(candidate: WorkflowCandidate) -> ValidationReport:
    """Validate the executable ``TOOLS`` plugin contract without importing it."""

    findings, tree = _extension_source_findings(candidate, "Tool module")
    if tree is None:
        return ValidationReport(tuple(findings))

    has_tool_import = any(
        isinstance(node, ast.ImportFrom)
        and node.module in {"lauren_ai", "lauren_ai._tools"}
        and any(alias.name == "tool" for alias in node.names)
        for node in tree.body
    )
    if not has_tool_import:
        findings.append(
            ValidationFinding(
                "tool-import",
                "Tool module must import tool from lauren_ai or lauren_ai._tools.",
            )
        )

    tools_value = _top_level_assignment(tree, "TOOLS")
    if not isinstance(tools_value, (ast.List, ast.Tuple)):
        findings.append(
            ValidationFinding("tools-export", "Tool module must export TOOLS as a list.")
        )
        return ValidationReport(tuple(findings))
    if not tools_value.elts:
        findings.append(
            ValidationFinding("tools-empty", "TOOLS must contain at least one callable.")
        )

    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    exported_names: list[str] = []
    for item in tools_value.elts:
        if not isinstance(item, ast.Name):
            findings.append(
                ValidationFinding(
                    "tool-entry", "Every TOOLS entry must reference a top-level callable."
                )
            )
            continue
        if item.id in exported_names:
            findings.append(
                ValidationFinding("tool-duplicate", f"Duplicate tool export: {item.id!r}.")
            )
            continue
        exported_names.append(item.id)
        definition = definitions.get(item.id)
        if definition is None:
            findings.append(
                ValidationFinding("tool-entry", f"TOOLS references undefined callable {item.id!r}.")
            )
            continue
        decorators = definition.decorator_list
        if not any(_decorator_name(decorator) == "tool" for decorator in decorators):
            findings.append(
                ValidationFinding(
                    "tool-decorator",
                    f"Exported tool {item.id!r} must use Lauren's @tool decorator.",
                )
            )
    return ValidationReport(tuple(findings))


def _command_call_details(node: ast.AST) -> tuple[str | None, list[ValidationFinding]]:
    findings: list[ValidationFinding] = []
    if not isinstance(node, ast.Call) or _func_name(node.func) != "Command":
        return None, [
            ValidationFinding("command-entry", "Every command export must be a Command(... ) call.")
        ]

    name_node: ast.expr | None = node.args[0] if node.args else None
    description_node: ast.expr | None = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "name":
            name_node = keyword.value
        elif keyword.arg == "description":
            description_node = keyword.value
        elif keyword.arg in {"handler", "menu_factory"} and isinstance(keyword.value, ast.Name):
            # The reference is checked by the caller once all definitions are known.
            pass
    name = _extract_literal_string(name_node) if name_node is not None else None
    if name is None or not name.startswith("/") or any(char.isspace() for char in name):
        findings.append(
            ValidationFinding(
                "command-name",
                "Command names must be literal slash-prefixed strings without whitespace.",
            )
        )
    description = (
        _extract_literal_string(description_node) if description_node is not None else None
    )
    if description is None:
        findings.append(
            ValidationFinding(
                "command-description", "Every Command must have a literal description."
            )
        )
    return name, findings


def validate_command_candidate(candidate: WorkflowCandidate) -> ValidationReport:
    """Validate ``COMMAND``/``COMMANDS`` exports without executing the module."""

    findings, tree = _extension_source_findings(candidate, "Command module")
    if tree is None:
        return ValidationReport(tuple(findings))

    has_command_import = any(
        isinstance(node, ast.ImportFrom)
        and node.module in {"agenthicc.commands", "agenthicc.commands.command"}
        and any(alias.name == "Command" for alias in node.names)
        for node in tree.body
    )
    if not has_command_import:
        findings.append(
            ValidationFinding(
                "command-import",
                "Command module must import Command from agenthicc.commands.",
            )
        )

    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    command_names: list[str] = []
    export_count = 0
    for export_name in ("COMMAND", "COMMANDS"):
        value = _top_level_assignment(tree, export_name)
        if value is None:
            continue
        export_count += 1
        nodes = [value]
        if export_name == "COMMANDS":
            if not isinstance(value, (ast.List, ast.Tuple)):
                findings.append(
                    ValidationFinding(
                        "commands-export", "COMMANDS must be a list or tuple of Command objects."
                    )
                )
                continue
            if not value.elts:
                findings.append(ValidationFinding("commands-empty", "COMMANDS must not be empty."))
            nodes = list(value.elts)
        for node in nodes:
            name, entry_findings = _command_call_details(node)
            findings.extend(entry_findings)
            if name is not None:
                if name in command_names:
                    findings.append(
                        ValidationFinding(
                            "command-duplicate", f"Duplicate command export: {name!r}."
                        )
                    )
                command_names.append(name)
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in {"handler", "menu_factory"} and isinstance(
                        keyword.value, ast.Name
                    ):
                        if keyword.value.id not in definitions:
                            findings.append(
                                ValidationFinding(
                                    "command-handler",
                                    f"Command references undefined {keyword.arg} {keyword.value.id!r}.",
                                )
                            )
    if export_count == 0:
        findings.append(
            ValidationFinding("command-export", "Command module must export COMMAND or COMMANDS.")
        )
    return ValidationReport(tuple(findings))


def source_sha256(source: str) -> str:
    """Return the stable digest used by staged-artifact publication."""

    return hashlib.sha256(source.encode("utf-8")).hexdigest()
