"""Deterministic validation of an agent-generated workflow plugin file.

The create_workflow VALIDATE phase never trusts the agent's own judgement alone:
before the agent is even asked for a verdict, the runner imports the generated
file exactly the way :func:`agenthicc.workflows.loader.load_python_workflows`
will and checks the resulting plugin class against the real
:class:`~agenthicc.workflows.plugin.WorkflowPlugin` contract.  The resulting
:class:`ValidationReport` is fed into the agent's prompt as evidence, and the
runner overrides an ``approve_workflow`` call whenever the report is not ``ok``.

Importing the file executes it — the same trust model as the workflow loader
itself, which must import a plugin module to discover its classes.  The file was
just written inside this session by the tool-approved agent, and the import is
refused outright for any path resolving outside the workspace root.
"""

from __future__ import annotations

import dataclasses
import ast
import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.workflows.plugin import WorkflowPlugin

log = logging.getLogger(__name__)

#: Builtin workflow names a generated plugin must not claim — a project-local
#: plugin is registered last and would silently shadow the builtin.
_RESERVED_NAMES: frozenset[str] = frozenset({"code_plan", "create_workflow"})

#: Valid ``PhaseSpec.output_schema`` values understood by ``_parse_output_schema``.
_KNOWN_OUTPUT_SCHEMAS: frozenset[str] = frozenset({"plan", "review_result", "free_text"})

_FORBIDDEN_BROWSER_IMPORTS: tuple[str, ...] = (
    "cloakbrowser",
    "playwright",
    "agenthicc.tools.cloakbrowser",
    "agenthicc.tools.playwright",
)
# Generated workflow code must use the framework's capability-gated tools for
# side effects. Besides making the no-network smoke deterministic, rejecting
# these imports before import-time execution prevents a helper module from
# bypassing the parent session's workspace/network policy.
_FORBIDDEN_EXTERNAL_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "socket",
        "subprocess",
        "requests",
        "httpx",
        "urllib",
        "urllib3",
        "aiohttp",
        "websockets",
        "ssl",
        "dns",
        "ftplib",
        "telnetlib",
        "webbrowser",
        "playwright",
        "cloakbrowser",
        "lauren_mcp",
    }
)
_KNOWN_BROWSER_TOOLS: frozenset[str] = frozenset(
    {
        "cloakbrowser_status",
        "cloakbrowser_open",
        "cloakbrowser_snapshot",
        "cloakbrowser_click",
        "cloakbrowser_fill",
        "cloakbrowser_press",
        "cloakbrowser_wait_for",
        "cloakbrowser_screenshot",
        "cloakbrowser_close",
        "playwright_status",
        "playwright_open",
        "playwright_snapshot",
        "playwright_click",
        "playwright_fill",
        "playwright_press",
        "playwright_wait_for",
        "playwright_screenshot",
        "playwright_close",
    }
)

_INTEGRATION_AVAILABLE_STATES: frozenset[str] = frozenset(
    {"available", "connected", "enabled", "ok", "ready", "selected"}
)
_INTEGRATION_UNAVAILABLE_STATES: frozenset[str] = frozenset(
    {
        "binary_missing",
        "disabled",
        "disconnected",
        "error",
        "failed",
        "missing",
        "not_configured",
        "not_installed",
        "not_reported",
        "not_selected",
        "unavailable",
    }
)
_INTEGRATION_NOT_PROBED_STATES: frozenset[str] = frozenset({"not_probed", "unknown", "unprobed"})


def _check_browser_imports(source: str, errors: list[str]) -> None:
    """Reject generated code that bypasses the session-owned browser adapter."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            modules = []
        for module in modules:
            if any(
                module == item or module.startswith(f"{item}.")
                for item in _FORBIDDEN_BROWSER_IMPORTS
            ):
                errors.append(
                    f"Generated workflows must not import {module!r}; use the session-provided "
                    "browser_* tools so policy and checkpoint boundaries "
                    "remain enforced."
                )
        if isinstance(node, ast.Name) and (
            node.id.startswith("cloakbrowser_") or node.id.startswith("playwright_")
        ):
            if node.id not in _KNOWN_BROWSER_TOOLS:
                errors.append(
                    f"Unknown browser tool {node.id!r}; use the session-provided canonical "
                    "browser_* tool names documented by the browser inspection tools."
                )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if (
                value.startswith("cloakbrowser_") or value.startswith("playwright_")
            ) and value not in _KNOWN_BROWSER_TOOLS:
                errors.append(
                    f"Unknown browser tool {value!r}; use the session-provided canonical "
                    "browser_* tool names documented by the browser inspection tools."
                )


def _check_external_imports(source: str, errors: list[str]) -> None:
    """Reject direct network/process clients in generated workflow sources.

    Workflow-local helpers are still imported by the loader, so checking every
    package source—not only ``runner.py``—is important. Runtime side effects
    belong in capability-gated tools supplied by the parent session; direct
    imports would make validation and smoke execution non-deterministic.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            modules = []
        for module in modules:
            root = module.split(".", 1)[0]
            if root in _FORBIDDEN_EXTERNAL_IMPORT_ROOTS:
                errors.append(
                    f"Generated workflows must not import {module!r}; use the parent session's "
                    "capability-gated tools so network, browser, MCP, process, and workspace "
                    "policy remain enforced."
                )


def _check_direct_instruction_reads(source: str, errors: list[str]) -> None:
    """Reject generated workflows that create a second AGENTS.md reader."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return

    def literal_text(node: ast.AST) -> str:
        """Return literal string content nested in a simple path expression."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr) and all(
            isinstance(value, ast.Constant) and isinstance(value.value, str)
            for value in node.values
        ):
            parts: list[str] = []
            for value in node.values:
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    return ""
                parts.append(value.value)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal_text(node.left) + literal_text(node.right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"str", "Path"} and node.args:
                return literal_text(node.args[0])
        return ""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            values = node.args[:1]
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "read_text",
            "read_bytes",
        }:
            values = [node.func.value]
        else:
            values = []
        if any("AGENTS" in literal_text(value).upper() for value in values):
            errors.append(
                "generated workflows must use the framework-provided instruction context; "
                "do not read AGENTS.md directly or create a second instruction loader."
            )


@dataclasses.dataclass(frozen=True)
class ValidationReport:
    """Outcome of deterministically validating one generated workflow package.

    :param path: The resolved path that was validated (empty when unresolvable).
    :param ok: True only when :attr:`errors` is empty.
    :param errors: Blocking problems; the workflow cannot be accepted.
    :param warnings: Non-blocking observations worth reporting to the agent.
    :param plugin_names: ``WorkflowPlugin.name`` of every plugin found in the file.
    :param phase_names: Phase names of the first matching plugin, in order.
    """

    path: str
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    plugin_names: tuple[str, ...] = ()
    phase_names: tuple[str, ...] = ()
    cache_contract: str = "legacy"
    categories: dict[str, str] = dataclasses.field(default_factory=dict)
    evidence: dict[str, object] = dataclasses.field(default_factory=dict)
    skipped_checks: tuple[str, ...] = ()

    def render(self) -> str:
        """Render the report as the text block shown to the validating agent."""
        lines: list[str] = ["[DETERMINISTIC VALIDATION REPORT]"]
        lines.append(f"file: {self.path or '(unresolved)'}")
        lines.append(f"result: {'PASS' if self.ok else 'FAIL'}")
        if self.plugin_names:
            lines.append(f"workflows found: {', '.join(self.plugin_names)}")
        if self.phase_names:
            lines.append(f"phases: {' → '.join(self.phase_names)}")
        lines.append(f"cache contract: {self.cache_contract}")
        if self.categories:
            lines.append(
                "categories: "
                + ", ".join(f"{name}={status}" for name, status in sorted(self.categories.items()))
            )
        if self.errors:
            lines.append("")
            lines.append(f"errors ({len(self.errors)}) — these MUST be fixed:")
            lines.extend(f"  {i}. {err}" for i, err in enumerate(self.errors, 1))
        if self.warnings:
            lines.append("")
            lines.append(f"warnings ({len(self.warnings)}):")
            lines.extend(f"  {i}. {warn}" for i, warn in enumerate(self.warnings, 1))
        if self.skipped_checks:
            lines.append("")
            lines.append("skipped checks: " + ", ".join(self.skipped_checks))
        if self.evidence:
            lines.append("")
            lines.append(
                "evidence: " + json.dumps(self.evidence, sort_keys=True, separators=(",", ":"))
            )
        if self.ok and not self.warnings:
            lines.append("")
            lines.append("The workflow imports cleanly and its phase graph is consistent.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible structured validation result."""
        return {
            "path": self.path,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "plugin_names": list(self.plugin_names),
            "phase_names": list(self.phase_names),
            "cache_contract": self.cache_contract,
            "categories": dict(self.categories),
            "evidence": dict(self.evidence),
            "skipped_checks": list(self.skipped_checks),
        }


def _fail(path: str, *errors: str) -> ValidationReport:
    """Return a not-ok report carrying *errors* only."""
    text = " ".join(errors).lower()
    categories = {"source": "fail", "result": "fail"}
    markers = (
        ("browser", "browser"),
        ("mcp", "mcp"),
        ("workspace", "workspace"),
        ("draft", "manifest"),
        ("manifest", "manifest"),
        ("symlink", "manifest"),
        ("runner.py", "manifest"),
        ("syntax", "source"),
    )
    for marker, category in markers:
        if marker in text:
            categories[category] = "fail"
            break
    return ValidationReport(
        path=path,
        ok=False,
        errors=tuple(errors),
        categories=categories,
    )


def _import_plugins(
    path: Path,
) -> tuple[list[type[WorkflowPlugin]], dict[str, object], str]:
    """Import *path* and return ``(plugin_classes, module_namespace, error_message)``.

    Mirrors :func:`agenthicc.workflows.loader.load_python_workflows` but returns
    the failure text instead of swallowing it, so the agent sees the real
    ``ImportError`` / ``NameError`` / ``ValueError`` raised by its own file.  The
    namespace is returned so runner checks can look at every class the file
    defined — the module itself is removed from ``sys.modules`` again.
    """
    import inspect  # noqa: PLC0415

    from agenthicc.workflows.plugin import WorkflowPlugin as _Plugin  # noqa: PLC0415
    from agenthicc.workflows.loader import load_python_workflow_modules  # noqa: PLC0415

    try:
        modules = load_python_workflow_modules(path)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — generated code, any failure
        return [], {}, f"{type(exc).__name__}: {exc}"

    found: list[type[WorkflowPlugin]] = []
    namespace: dict[str, object] = {}
    seen: set[type[WorkflowPlugin]] = set()
    for module in modules:
        namespace.update(vars(module))
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                obj is not _Plugin
                and issubclass(obj, _Plugin)
                and getattr(obj, "name", "") != ""
                and obj not in seen
            ):
                seen.add(obj)
                found.append(obj)
    return found, namespace, ""


def _check_phases(
    plugin: type[WorkflowPlugin],
    errors: list[str],
    warnings: list[str],
) -> tuple[str, ...]:
    """Validate ``plugin.phases`` structurally; return the ordered phase names."""
    from agenthicc.workflows.plugin import PhaseRole, PhaseSpec  # noqa: PLC0415

    phases = plugin.phases
    if not isinstance(phases, list):
        errors.append(f"{plugin.__name__}.phases must be a list of PhaseSpec objects.")
        return ()

    if not phases:
        overrides_runner = _has_custom_runner(plugin)
        message = (
            f"{plugin.__name__}.phases is empty, so the workflow has nothing to run."
            if not overrides_runner
            else (
                f"{plugin.__name__}.phases is empty; the custom runner drives every phase, "
                "but the TUI phase counter will show 0 phases."
            )
        )
        (warnings if overrides_runner else errors).append(message)
        return ()

    names: list[str] = []
    for index, phase in enumerate(phases):
        label = f"{plugin.__name__}.phases[{index}]"
        if not isinstance(phase, PhaseSpec):
            errors.append(f"{label} is {type(phase).__name__}, not a PhaseSpec.")
            continue
        if not phase.name.strip():
            errors.append(f"{label} has an empty name; every phase needs a unique name.")
            continue
        if phase.name in names:
            errors.append(f"{label} repeats the phase name {phase.name!r}; names must be unique.")
            continue
        names.append(phase.name)
        if phase.max_turns <= 0:
            errors.append(f"{label} ({phase.name}) has max_turns={phase.max_turns}; must be >= 1.")
        if phase.output_schema is not None and phase.output_schema not in _KNOWN_OUTPUT_SCHEMAS:
            warnings.append(
                f"{label} ({phase.name}) has output_schema={phase.output_schema!r}; "
                f"known values are {sorted(_KNOWN_OUTPUT_SCHEMAS)}."
            )
        known_roles = {
            value
            for attr, value in vars(PhaseRole).items()
            if not attr.startswith("_") and isinstance(value, str)
        }
        if phase.agent_type not in known_roles:
            warnings.append(
                f"{label} ({phase.name}) has agent_type={phase.agent_type!r}, which is not a "
                f"builtin role ({sorted(known_roles)}); it must be a discovered agent."
            )

    known = set(names)
    for phase in phases:
        if not isinstance(phase, PhaseSpec) or phase.name not in known:
            continue
        for field_name in ("next", "on_reject", "on_error"):
            target = getattr(phase, field_name)
            if target is not None and target not in known:
                errors.append(
                    f"{plugin.__name__} phase {phase.name!r} sets {field_name}={target!r}, "
                    f"which is not one of its phases ({sorted(known)})."
                )

    reachable: set[str] = {names[0]} if names else set()
    frontier: list[str] = list(reachable)
    by_name = {p.name: p for p in phases if isinstance(p, PhaseSpec)}
    while frontier:
        current = by_name.get(frontier.pop())
        if current is None:
            continue
        for field_name in ("next", "on_reject", "on_error"):
            target = getattr(current, field_name)
            if isinstance(target, str) and target in known and target not in reachable:
                reachable.add(target)
                frontier.append(target)
    for orphan in sorted(known - reachable):
        warnings.append(
            f"{plugin.__name__} phase {orphan!r} is unreachable from the first phase "
            f"({names[0]!r}); no next/on_reject/on_error edge leads to it."
        )
    return tuple(names)


def _check_phase_capabilities(
    plugin: type[WorkflowPlugin],
    errors: list[str],
) -> None:
    """Validate capability allowlists against the live capability taxonomy."""
    from agenthicc.tools.capabilities import ToolCapability  # noqa: PLC0415

    known = {capability.value for capability in ToolCapability}
    phases = plugin.phases if isinstance(plugin.phases, list) else ()
    for phase in phases:
        if not hasattr(phase, "name"):
            continue
        for field_name in ("allowed_capabilities", "allowed_capabilities_override"):
            raw = getattr(phase, field_name)
            if raw is None:
                continue
            if not isinstance(raw, (set, frozenset)):
                errors.append(
                    f"phase {phase.name!r} {field_name} must be a set of ToolCapability values."
                )
                continue
            unknown = sorted(
                str(value.value if isinstance(value, ToolCapability) else value)
                for value in raw
                if not (
                    isinstance(value, ToolCapability) or isinstance(value, str) and value in known
                )
            )
            if unknown:
                errors.append(
                    f"phase {phase.name!r} {field_name} contains unknown capability values: "
                    f"{unknown}. Use the live ToolCapability taxonomy."
                )


def _default_build_runner_func() -> object:
    """Return the undecorated default ``WorkflowPlugin.build_runner`` function.

    Used to detect whether a generated plugin supplies its own runner, which
    changes an empty ``phases`` list from an error into a warning.
    """
    from agenthicc.workflows.plugin import WorkflowPlugin  # noqa: PLC0415

    return WorkflowPlugin.build_runner.__func__  # type: ignore[attr-defined]


def _has_custom_runner(plugin: type[WorkflowPlugin]) -> bool:
    """True when *plugin* overrides ``build_runner`` with its own implementation."""
    own = getattr(plugin.build_runner, "__func__", None)
    return own is not None and own is not _default_build_runner_func()


def _overrides_plugin_method(plugin: type[WorkflowPlugin], method_name: str) -> bool:
    """Return whether *plugin* supplies a concrete method for a framework hook.

    Looking through the MRO (rather than only at ``plugin.__dict__``) permits a
    downstream project to share a checkpoint codec through its own small base
    class while still distinguishing it from the no-op hooks inherited from
    :class:`WorkflowPlugin`.
    """
    from agenthicc.workflows.plugin import WorkflowPlugin  # noqa: PLC0415

    for base in plugin.__mro__:
        if base is WorkflowPlugin:
            return False
        if method_name in base.__dict__:
            return callable(getattr(plugin, method_name, None))
    return False


def _check_checkpoint_contract(
    plugin: type[WorkflowPlugin],
    errors: list[str],
) -> None:
    """Require codecs for a plugin-owned runner and its custom context.

    The generic declarative runner serializes the framework's
    ``WorkflowContext`` automatically. A plugin-owned state machine, however,
    can carry arbitrary state and therefore must explicitly define how that
    state is persisted and restored. This check makes generated workflows
    checkpoint-aware by construction instead of allowing them to discover the
    limitation only after a user presses Esc.
    """
    if not _has_custom_runner(plugin):
        return
    missing = [
        method
        for method in (
            "checkpoint_context_to_payload",
            "checkpoint_context_from_payload",
        )
        if not _overrides_plugin_method(plugin, method)
    ]
    if missing:
        errors.append(
            f"{plugin.__name__} defines a custom runner but is missing checkpoint codec "
            f"method(s): {', '.join(f'{name}()' for name in missing)}. Custom runner "
            "contexts must be JSON-serializable, omit session memory, and reattach the "
            "memory argument during restore so Esc pause/resume cannot restart at phase one."
        )


def _check_checkpoint_topology_contract(
    plugin: type[WorkflowPlugin],
    source: str,
    errors: list[str],
    *,
    strict: bool,
) -> None:
    """Validate the phase-coordinate contract for generated workflows.

    A fixed ``PhaseSpec`` list inherits the framework resolver.  A runner that
    filters or computes its active phases must expose a resolver based on
    persisted context; otherwise a numeric checkpoint cursor has no stable
    meaning after a restart.
    """
    if not strict:
        return
    from agenthicc.workflows.checkpoint import (  # noqa: PLC0415
        CheckpointValidationError,
        resolve_workflow_checkpoint_topology,
    )

    resolver_overridden = _overrides_plugin_method(plugin, "resolve_checkpoint_topology")
    dynamic_markers = (
        "active_phase_names",
        "selected_phases",
        "phase_filter",
        "skipped_phases",
        "profiled",
        "profile=",
    )
    appears_dynamic = any(marker in source for marker in dynamic_markers)
    try:
        topology = resolve_workflow_checkpoint_topology(
            plugin,
            {"kind": "WorkflowContext", "fields": {}},
        )
    except (CheckpointValidationError, TypeError, ValueError) as exc:
        if not resolver_overridden:
            errors.append(
                "generated workflows with a dynamic or unresolvable phase topology must "
                "override resolve_checkpoint_topology(context_payload) and derive the active "
                f"phase order from persisted context ({type(exc).__name__}: {exc})."
            )
        else:
            errors.append(
                "resolve_checkpoint_topology(context_payload) must resolve a safe active graph "
                f"from checkpoint data ({type(exc).__name__}: {exc})."
            )
        return

    declared_names = {
        str(getattr(phase, "name", ""))
        for phase in (plugin.phases if isinstance(plugin.phases, list) else ())
    }
    unknown = sorted(set(topology.phase_names).difference(declared_names))
    if unknown:
        errors.append(
            "resolve_checkpoint_topology() returned phases not declared by the plugin: "
            f"{unknown}. The active topology must be an ordered view of the canonical plan."
        )
    if appears_dynamic and not resolver_overridden:
        errors.append(
            "this workflow appears to select or skip phases dynamically but inherits the "
            "fixed-list checkpoint resolver; implement resolve_checkpoint_topology() and "
            "persist the selector/plan version in the typed context."
        )


def _check_runner(
    plugin: type[WorkflowPlugin],
    namespace: dict[str, object],
    errors: list[str],
    warnings: list[str],
    phase_count: int,
) -> None:
    """Check the workflow's own runner, the shape create_workflow asks for.

    A generated workflow is expected to ship a state-machine runner: it is the
    only shape that expresses retries, branches, loops, accumulated context, and
    phase-local transition tools. Shipping none is a warning rather than an error,
    because a single-phase unconditional workflow legitimately needs no runner —
    but the runner it *does* ship must be usable.
    """
    from agenthicc.workflows.base_runner import BaseWorkflowRunner  # noqa: PLC0415

    if not _has_custom_runner(plugin):
        if phase_count > 1:
            warnings.append(
                f"{plugin.__name__} ships no runner: build_runner() is inherited, so the "
                "generic declarative runner drives its "
                f"{phase_count} phases. Retries, conditional routing, accumulated context, "
                "and phase-local transition tools cannot be expressed that way. Add a "
                "state-machine runner unless every phase really is one unconditional turn."
            )
        return

    runners = [
        obj
        for obj in namespace.values()
        if isinstance(obj, type)
        and obj is not BaseWorkflowRunner
        and issubclass(obj, BaseWorkflowRunner)
        and obj.__module__ == plugin.__module__
    ]
    if not runners:
        warnings.append(
            f"{plugin.__name__}.build_runner() is overridden but the file defines no "
            "BaseWorkflowRunner subclass of its own; it must return a runner that lives in "
            "this workflow package."
        )
        return

    for runner in runners:
        for method in ("run", "resume"):
            if not callable(getattr(runner, method, None)):
                errors.append(f"{runner.__name__} does not implement {method}().")
        if getattr(runner, "__abstractmethods__", frozenset()):
            missing = ", ".join(sorted(runner.__abstractmethods__))
            errors.append(f"{runner.__name__} is abstract — {missing} still needs implementing.")

    _check_checkpoint_contract(plugin, errors)


def _check_resume_does_not_restart(source: str, errors: list[str]) -> None:
    """Reject a generated resume method that calls the fresh-run entry point.

    This is deliberately an AST check rather than a substring search: comments,
    documentation, and a helper with a similarly named local method must not
    trigger it. A custom runner's ``resume`` method must dispatch its restored
    typed context through the same phase loop; ``return await self.run(...)``
    would silently restart at phase one after a crash.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "resume":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                continue
            receiver = child.func.value
            if (
                isinstance(receiver, ast.Name)
                and receiver.id == "self"
                and child.func.attr == "run"
            ):
                errors.append(
                    f"{node.name}() must resume the restored typed context through its phase "
                    "dispatch loop; calling self.run() silently restarts the workflow at phase one."
                )
                break


def _check_error_recovery_contract(source: str, errors: list[str], *, strict: bool) -> None:
    """Validate the generated runner's handoff to framework error recovery."""
    if not strict:
        return
    if "attach_context" not in source:
        errors.append(
            "custom workflow runners must attach their typed context to the workflow handle "
            "before the first provider/tool call so workflow errors can be checkpointed."
        )
    if 'mark_terminal("failed"' in source or "mark_terminal('failed'" in source:
        errors.append(
            "generated runners must not independently terminalize ordinary errors with "
            "mark_terminal('failed'); let the framework failure finalizer choose an "
            "error-paused checkpoint or diagnostic-only failure."
        )
    if 'save_checkpoint(reason="failed"' in source or "save_checkpoint(reason='failed'" in source:
        errors.append(
            "generated runners must not write terminal failed checkpoints for ordinary phase "
            "errors; the framework failure finalizer owns error persistence."
        )
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in {"run", "resume"}:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Try):
                continue
            for handler in child.handlers:
                broad_exception = handler.type is None or (
                    isinstance(handler.type, ast.Name)
                    and handler.type.id in {"Exception", "BaseException"}
                )
                if not broad_exception:
                    continue
                has_raise = any(isinstance(item, ast.Raise) for item in ast.walk(handler))
                calls_finalizer = any(
                    isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Attribute)
                    and item.func.attr == "finalize_failure"
                    for item in ast.walk(handler)
                )
                if not has_raise and not calls_finalizer:
                    errors.append(
                        f"{node.name}() must re-raise workflow exceptions (or call "
                        "finalize_failure()); silently returning from an exception handler "
                        "prevents the framework from creating an error checkpoint."
                    )

    # Resumability is determined by the framework's durable typed-context
    # capability, not by a generated runner's exception classification. Keep
    # this contract enforceable so a generated workflow cannot accidentally
    # reproduce the old "error -> terminal failed -> fresh INIT" path by
    # opting out at the call site. The keyword remains accepted by the runtime
    # for source compatibility with older integrations, but is intentionally a
    # no-op for ordinary workflow failures.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not any(
            isinstance(item, ast.keyword)
            and item.arg == "recoverable"
            and isinstance(item.value, ast.Constant)
            and item.value.value is False
            for item in node.keywords
        ):
            continue
        errors.append(
            "generated workflows must not pass recoverable=False for ordinary workflow "
            "exceptions; typed context and durable checkpoint capability determine resume."
        )
        break


def _check_phase_lifecycle_contract(source: str, errors: list[str], *, strict: bool) -> None:
    """Require the shared annotation/boundary contract for new custom runners."""
    if not strict:
        return
    required_markers = {
        "PhaseAnnotation": "a PhaseAnnotation built from the canonical phase plan",
        "publish_phase_annotation": "publish_phase_annotation() before every phase turn",
        "update_phase": "the WorkflowRunHandle phase projection",
        "checkpoint_phase_boundary": "checkpoint_phase_boundary() after every transition",
        "save_checkpoint": "the existing durable checkpoint path",
        "reconcile_phase_cursor": "pre-prompt resume reconciliation",
        "plan_version": "the persisted phase-plan revision or plugin fingerprint",
        "phase_attempts": "per-phase retry/attempt state",
        "completed_phases": "completed-phase evidence",
        "phase_history": "auditable phase-boundary history",
        "last_boundary": "the latest durable boundary marker",
    }
    missing = [
        description for marker, description in required_markers.items() if marker not in source
    ]
    if missing:
        errors.append(
            "custom workflow lifecycle contract is incomplete; missing "
            + ", ".join(missing)
            + ". Use describe_phase_lifecycle() and show_phase_lifecycle_template()."
        )
    if "update_workflow_phase" not in source and "publish_phase_annotation" not in source:
        errors.append(
            "custom workflow lifecycle must reach the AppState workflow-phase projection "
            "through publish_phase_annotation() or an equivalent direct call."
        )
    if "PhaseBoundaryError" not in source or "raise" not in source:
        errors.append(
            "custom workflow runners must propagate PhaseBoundaryError from a failed "
            "boundary checkpoint to the framework failure finalizer."
        )
    if "phase_index" not in source or "total_phases" not in source:
        errors.append(
            "phase annotations must derive phase_index and total_phases from the declared "
            "PhaseSpec plan; a hard-coded progress counter is not sufficient."
        )


def _string_values(value: object) -> tuple[str, ...]:
    """Read a bounded string collection from generated plugin metadata."""
    if isinstance(value, str):
        values: Iterable[object] = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        values = value
    else:
        return ()
    result = {
        item.strip().lower()[:128] for item in values if isinstance(item, str) and item.strip()
    }
    return tuple(sorted(result))


def _declared_integrations(
    plugin: type[WorkflowPlugin],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return required, optional, and fallback integration declarations.

    These attributes are intentionally optional so existing plugins remain
    compatible.  ``integration_fallbacks`` is normally a mapping whose keys
    are required integration names; accepting a sequence as well keeps the
    metadata easy to write and harmlessly ignores its descriptions.
    """
    required = _string_values(getattr(plugin, "required_integrations", ()))
    optional = _string_values(getattr(plugin, "optional_integrations", ()))
    fallback_value = getattr(plugin, "integration_fallbacks", ())
    if isinstance(fallback_value, Mapping):
        fallbacks = _string_values(tuple(fallback_value.keys()))
    else:
        fallbacks = _string_values(fallback_value)
    legacy_fallbacks = _string_values(getattr(plugin, "fallback_integrations", ()))
    return required, optional, tuple(sorted(set(fallbacks) | set(legacy_fallbacks)))


def _availability_state(value: object) -> tuple[bool | None, str]:
    """Normalize safe integration status data to ``(available, state)``."""
    if isinstance(value, bool):
        return value, "available" if value else "unavailable"
    if isinstance(value, str):
        state = value.strip().lower()[:64]
        if state in _INTEGRATION_AVAILABLE_STATES:
            return True, state
        if state in _INTEGRATION_UNAVAILABLE_STATES:
            return False, state
        if state in _INTEGRATION_NOT_PROBED_STATES:
            return None, state
        return None, state or "unknown"
    if isinstance(value, Mapping):
        explicit = value.get("available")
        if isinstance(explicit, bool):
            return explicit, "available" if explicit else "unavailable"
        for key in ("status", "state", "dependency_status", "connection_state"):
            if key in value:
                result = _availability_state(value[key])
                if result[1] not in {"unknown", ""}:
                    return result
        if value.get("enabled") is False:
            return False, "disabled"
    return None, "unknown"


def _integration_status(
    integration: str,
    statuses: Mapping[str, object] | None,
) -> tuple[bool | None, str]:
    """Resolve one declared integration from a redacted session summary."""
    if statuses is None:
        return None, "not_probed"
    name = integration.strip().lower()
    direct = {str(key).strip().lower(): value for key, value in statuses.items()}
    if name in direct:
        return _availability_state(direct[name])

    if name in {"cloakbrowser", "playwright"}:
        browser = direct.get("browser")
        if not isinstance(browser, Mapping):
            return False, "not_configured"
        backend = (
            str(
                browser.get(
                    "optional_dependency",
                    browser.get("selected_backend", browser.get("backend", "")),
                )
            )
            .strip()
            .lower()
        )
        if backend and name not in backend:
            return False, "backend_not_selected"
        if browser.get("selected") is False or browser.get("configured") is False:
            return False, "not_configured"
        return _availability_state(browser)

    if name == "mcp" or name.startswith("mcp:"):
        raw_servers = direct.get("mcp")
        if not isinstance(raw_servers, Iterable) or isinstance(raw_servers, (str, bytes, Mapping)):
            return False, "not_configured"
        requested_server = name.partition(":")[2]
        found = False
        for item in raw_servers:
            if not isinstance(item, Mapping):
                continue
            server = str(item.get("server", "")).strip().lower()
            if requested_server and server != requested_server:
                continue
            found = True
            available, state = _availability_state(item)
            if available is True:
                return True, state
            if available is False:
                return False, state
        return (False, "not_configured") if not found else (None, "not_probed")

    # Once the caller supplied an explicit session summary, an undeclared
    # integration is not evidence of availability.  Fail closed for required
    # integrations rather than allowing generated code to assume an adapter
    # exists merely because its name is unfamiliar to the current catalog.
    return False, "not_reported"


def _check_optional_integrations(
    plugin: type[WorkflowPlugin],
    errors: list[str],
    warnings: list[str],
    *,
    available_integrations: Mapping[str, object] | None,
) -> dict[str, object]:
    """Validate declared optional integrations against a safe session summary."""
    required, optional, fallbacks = _declared_integrations(plugin)
    declarations = tuple(sorted(set(required) | set(optional) | set(fallbacks)))
    evidence: dict[str, object] = {
        "required": list(required),
        "optional": list(optional),
        "fallbacks": list(fallbacks),
        "states": {},
    }
    if not declarations:
        evidence["status"] = "not_declared"
        return evidence

    states: dict[str, str] = {}
    degraded = False
    not_probed = False
    missing_required = False
    for integration in declarations:
        available, state = _integration_status(integration, available_integrations)
        states[integration] = state
        if available is False:
            if integration in required and integration not in fallbacks:
                missing_required = True
                guidance = {
                    "cloakbrowser": "run `uv sync --extra cloakbrowser` and configure a usable browser backend",
                    "playwright": "run `uv sync --extra playwright` and install its browser runtime",
                    "mcp": "configure and connect at least one MCP server",
                }.get(integration, f"provide the '{integration}' integration")
                errors.append(
                    f"required integration {integration!r} is unavailable ({state}); {guidance}, "
                    "or declare an explicit integration_fallbacks entry."
                )
            else:
                degraded = True
                warnings.append(
                    f"optional integration {integration!r} is unavailable ({state}); "
                    "the declared fallback/degraded path must remain usable."
                )
        elif available is None:
            not_probed = True

    evidence["states"] = states
    evidence["status"] = (
        "fail"
        if missing_required
        else "degraded"
        if degraded
        else "not_probed"
        if not_probed
        else "pass"
    )
    return evidence


def _check_dynamic_stable_prompt_ast(source: str, errors: list[str]) -> None:
    """Reject changing expressions embedded in stable prompt constants."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    stable_names = {
        "CACHE_CONTRACT",
        "STABLE_SYSTEM_PROMPT",
        "STABLE_PROMPT",
        "WORKFLOW_CACHE_PROMPT",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if not names.intersection(stable_names):
            continue
        is_literal_strip = (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "strip"
            and len(node.value.args) == 0
            and isinstance(node.value.func.value, ast.Constant)
            and isinstance(node.value.func.value.value, str)
        )
        if isinstance(node.value, (ast.JoinedStr, ast.BinOp)) or (
            isinstance(node.value, ast.Call) and not is_literal_strip
        ):
            errors.append(
                "stable prompt constants must contain immutable literal policy only; "
                f"{sorted(names.intersection(stable_names))[0]} contains a dynamic expression. "
                "Move phase state, artifacts, summaries, questions, and answers to dynamic context."
            )


def _ast_decorator_name(node: ast.expr) -> str:
    """Return the simple name used by a decorator expression."""
    candidate: ast.expr = node.func if isinstance(node, ast.Call) else node
    return candidate.id if isinstance(candidate, ast.Name) else ""


def _check_transition_tool_contract(
    source: str,
    errors: list[str],
    *,
    strict: bool,
) -> bool:
    """Catch phase-tool decorator/import mistakes hidden inside factories.

    Importing a generated module does not execute local factory bodies. Static
    inspection therefore has to reject the two common failures directly:
    importing ``tool_control`` from ``lauren_ai._tools`` and calling the bare
    metadata decorator as ``@tool_control()``.
    """
    validation_errors = errors if strict else []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    control_names: set[str] = {"tool_control"}
    correct_import = False
    saw_control_reference = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "tool_control" for alias in node.names
        ):
            alias = next(alias for alias in node.names if alias.name == "tool_control")
            local_name = alias.asname or alias.name
            control_names.add(local_name)
            saw_control_reference = True
            if node.module == "agenthicc.tools.capabilities":
                correct_import = True
            elif node.module == "lauren_ai._tools":
                validation_errors.append(
                    "tool_control must be imported from agenthicc.tools.capabilities, "
                    "not lauren_ai._tools. lauren_ai._tools exports tool only."
                )
            else:
                validation_errors.append(
                    f"tool_control is imported from {node.module or '(relative module)'}, "
                    "but generated workflows must use agenthicc.tools.capabilities."
                )

    decorated_transition = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = [_ast_decorator_name(decorator) for decorator in node.decorator_list]
        control_indexes = [index for index, name in enumerate(names) if name in control_names]
        if not control_indexes:
            continue
        decorated_transition = True
        saw_control_reference = True
        for index in control_indexes:
            decorator = node.decorator_list[index]
            if isinstance(decorator, ast.Call):
                validation_errors.append(
                    f"{node.name} uses @tool_control() but tool_control is a bare decorator; "
                    "write @tool_control without parentheses."
                )
        tool_indexes = [
            index
            for index, name in enumerate(names)
            if name == "tool" or name in {"_tool", "lauren_tool"}
        ]
        if tool_indexes and min(control_indexes) > min(tool_indexes):
            validation_errors.append(
                f"{node.name} must place @tool_control above @tool() so CONTROL metadata "
                "survives decoration."
            )

    if saw_control_reference and not correct_import:
        # The import-specific branch above supplies the actionable detail for
        # known wrong imports; this covers an unqualified/aliased reference.
        if not any("tool_control must be imported" in error for error in validation_errors):
            validation_errors.append(
                "transition tools must import tool_control from agenthicc.tools.capabilities."
            )
    return decorated_transition


def _check_prompt_cache_contract(
    sources: dict[Path, str],
    plugin: type[WorkflowPlugin],
    errors: list[str],
    *,
    strict: bool,
) -> str:
    """Validate the generated-workflow cache/questioning authoring contract."""

    source = "\n".join(sources.values())
    has_custom_runner = _has_custom_runner(plugin)
    if not has_custom_runner:
        return "generic-runner"

    has_cache_contract = "CACHE_CONTRACT" in source
    has_runner_helper = "run_phase(" in source or "build_workflow_prompt_contract" in source
    has_stable_argument = "stable_system_prompt" in source
    has_question_tool = "ask_user" in source or "make_questions_tool" in source
    has_question_policy = all(
        marker in source.lower() for marker in ("clarifying", "ambiguous", "do not guess")
    )
    has_workspace_policy = "workspace_access" in source
    has_conversation_identity = "conversation_id" in source

    contract_error_start = len(errors)
    _check_dynamic_stable_prompt_ast(source, errors)
    _check_direct_instruction_reads(source, errors)
    has_transition_decorator = _check_transition_tool_contract(
        source,
        errors,
        strict=strict,
    )
    if "insert(0" in source or "_messages.insert" in source:
        errors.append(
            "workflow code must not insert messages at the beginning of shared conversation "
            "history; append dynamic context through the supported runner API."
        )
    if "_run_agent_turn" in source and "build_workflow_prompt_contract" not in source:
        errors.append(
            "custom workflows must use CodePlanRunner.run_phase() or "
            "build_workflow_prompt_contract(); direct _run_agent_turn calls bypass the "
            "cache/checkpoint contract."
        )
    if "WorkspaceScope.create" in source or "WorkspaceAccessPolicy(" in source:
        errors.append(
            "generated custom workflows must inherit the parent WorkflowConfig workspace "
            "policy; do not construct a second WorkspaceScope or WorkspaceAccessPolicy."
        )

    if not strict:
        return "contract-native" if has_cache_contract and has_runner_helper else "legacy"

    if not has_cache_contract:
        errors.append(
            "generated custom workflows must declare a literal CACHE_CONTRACT stable system "
            "prompt. Include the user-questioning and cache-stability policy."
        )
    if not has_runner_helper:
        errors.append(
            "generated custom workflows must use CodePlanRunner.run_phase() or the shared "
            "build_workflow_prompt_contract() helper."
        )
    if not has_stable_argument:
        errors.append(
            "every generated custom workflow must pass CACHE_CONTRACT as "
            "stable_system_prompt=...; phase-specific instructions remain dynamic."
        )
    if not has_question_tool:
        errors.append(
            "generated workflows must use the existing ask_user/make_questions_tool contract "
            "for clarifying questions."
        )
    if not has_question_policy:
        errors.append(
            "CACHE_CONTRACT must instruct the workflow agent to ask clarifying questions "
            "for missing or ambiguous requirements and not guess."
        )
    if not has_workspace_policy:
        errors.append(
            "generated custom workflows must document and inherit WorkflowConfig.workspace_access "
            "for every phase; use the standard run_phase() path instead of creating a second scope."
        )
    if not has_conversation_identity:
        errors.append(
            "generated custom workflows must preserve the parent session's conversation_id "
            "across every phase, retry, and resume; never create a second conversation."
        )
    if not has_transition_decorator:
        errors.append(
            "generated custom workflows must mark every phase handoff callable with the "
            "bare @tool_control decorator imported from agenthicc.tools.capabilities. "
            "Use describe_transition_tool_pattern() for the exact pattern."
        )
    if len(errors) > contract_error_start:
        return "invalid"
    return "contract-native"


def validate_workflow_file(
    path: str,
    *,
    expected_name: str = "",
    root: Path | None = None,
    strict_cache_contract: bool = False,
    available_integrations: Mapping[str, object] | None = None,
) -> ValidationReport:
    """Deterministically validate a workflow file or package at *path*.

    :param path: Path the generation phase reported writing, absolute or relative
        to *root*.
    :param expected_name: When non-empty, the approved workflow name the path must
        define.
    :param root: Workspace root the file must live inside; defaults to the current
        working directory.  A path resolving outside it is refused without import.
    :returns: A :class:`ValidationReport`; ``ok`` is True only when no blocking
        error was found.
    """
    base = (root or Path.cwd()).resolve()

    raw = path.strip() if isinstance(path, str) else ""
    if not raw:
        return _fail(
            "",
            "No workflow package path was recorded, so there is nothing to validate. "
            "Write the package and call mark_generation_complete(summary, path).",
        )

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.is_symlink():
        return _fail(
            str(candidate),
            f"{candidate} is a symlink; generated workflow sources must be regular files "
            "inside the authorized workspace.",
        )
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return _fail(raw, f"The path {raw!r} could not be resolved: {type(exc).__name__}: {exc}")

    shown = str(resolved)
    if not resolved.is_relative_to(base):
        return _fail(
            shown,
            f"{shown} is outside the workspace root {base}; the workflow must be written "
            "inside the project, normally at .agenthicc/workflows/<name>/runner.py.",
        )
    if not resolved.exists():
        return _fail(
            shown,
            f"No file exists at {shown}; no workflow file or directory exists there. Write the complete workflow "
            "package to that path with the write tools before marking generation complete.",
        )
    is_package = resolved.is_dir()
    if not is_package and not resolved.is_file():
        return _fail(shown, f"{shown} is neither a workflow file nor a workflow directory.")
    if resolved.name.startswith("_"):
        return _fail(
            shown,
            f"{resolved.name} starts with an underscore, and the workflow loader skips those "
            "entries. Rename it to <name> or <name>.py.",
        )

    source_paths: list[Path]
    if is_package:
        for item in resolved.rglob("*"):
            if item.is_symlink():
                return _fail(
                    shown,
                    f"{item} is a symlink; generated workflow packages must not contain "
                    "symlinked files or directories.",
                )
        runner_path = resolved / "runner.py"
        if not runner_path.is_file():
            return _fail(
                shown,
                f"{shown} is a directory but has no runner.py workflow entry point. "
                "Write the runner to .agenthicc/workflows/<name>/runner.py.",
            )
        source_paths = sorted(
            item
            for item in resolved.rglob("*.py")
            if item.is_file() and "__pycache__" not in item.parts
        )
        if not source_paths:
            return _fail(shown, f"{shown} contains no Python source files.")
    else:
        if resolved.suffix != ".py":
            return _fail(
                shown,
                f"{shown} does not end in .py, so the workflow loader will skip it. "
                "Write a legacy workflow to .agenthicc/workflows/<name>.py or use "
                ".agenthicc/workflows/<name>/runner.py.",
            )
        source_paths = [resolved]

    try:
        sources = {
            source_path: source_path.read_text(encoding="utf-8") for source_path in source_paths
        }
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(
            shown,
            f"{shown} could not be read as UTF-8 text: {type(exc).__name__}: {exc}",
        )
    empty = next(
        (source_path for source_path, source in sources.items() if not source.strip()), None
    )
    if empty is not None:
        return _fail(shown, f"{empty} is empty; the complete workflow source was never written.")

    for source_path, source in sources.items():
        try:
            compile(source, str(source_path), "exec")
        except SyntaxError as exc:
            return _fail(
                shown,
                f"{source_path} has a syntax error on line {exc.lineno}: {exc.msg}. "
                "Fix the source and rewrite the workflow package.",
            )

    source_errors: list[str] = []
    for source in sources.values():
        _check_browser_imports(source, source_errors)
        _check_external_imports(source, source_errors)
    if source_errors:
        return _fail(shown, *source_errors)

    plugins, namespace, import_error = _import_plugins(resolved)
    if import_error:
        return _fail(
            shown,
            f"Importing {shown} failed with {import_error}. The workflow loader imports the "
            "file the same way, so it would be skipped entirely. Fix the failure.",
        )
    if not plugins:
        return _fail(
            shown,
            f"{shown} defines no WorkflowPlugin subclass with a non-empty name. Add "
            "'class <Name>(WorkflowPlugin):' with a name, description, and phases.",
        )

    errors: list[str] = []
    warnings: list[str] = []
    plugin_names = tuple(plugin.name for plugin in plugins)

    if expected_name and expected_name not in plugin_names:
        errors.append(
            f"The approved workflow name is {expected_name!r} but {shown} defines "
            f"{list(plugin_names)}. Set name = {expected_name!r} on the plugin class."
        )
    if (
        expected_name
        and resolved.name != expected_name
        and not is_package
        and resolved.stem != expected_name
    ):
        warnings.append(
            f"The file is named {resolved.name} but the workflow is {expected_name!r}; "
            f"{expected_name}.py is the conventional filename."
        )
    elif expected_name and is_package and resolved.name != expected_name:
        warnings.append(
            f"The workflow directory is named {resolved.name} but the workflow is "
            f"{expected_name!r}; {expected_name}/ is the conventional directory."
        )

    target = next(
        (plugin for plugin in plugins if plugin.name == expected_name),
        plugins[0],
    )

    for plugin in plugins:
        if plugin.name in _RESERVED_NAMES:
            errors.append(
                f"{plugin.__name__}.name is {plugin.name!r}, which is a builtin workflow; a "
                "project workflow with that name would shadow it. Choose a different name."
            )
        if not isinstance(plugin.description, str) or not plugin.description.strip():
            errors.append(
                f"{plugin.__name__}.description is empty; the workflow picker shows it. "
                "Add a one-line description."
            )
        if not isinstance(plugin.mode_bindings, list):
            errors.append(
                f"{plugin.__name__}.mode_bindings must be a list of mode names "
                f"(got {type(plugin.mode_bindings).__name__}). Use [] for manual invocation."
            )
        try:
            params = plugin.build_params({})
        except Exception as exc:  # noqa: BLE001 — generated code, any failure
            errors.append(
                f"{plugin.__name__}.build_params({{}}) raised {type(exc).__name__}: {exc}. "
                "It must work with an empty mapping, since TOML config is optional."
            )
        else:
            from agenthicc.workflows.plugin import WorkflowParams  # noqa: PLC0415

            if not isinstance(params, WorkflowParams):
                errors.append(
                    f"{plugin.__name__}.build_params({{}}) returned "
                    f"{type(params).__name__}, not a WorkflowParams."
                )
        if not callable(getattr(plugin, "build_runner", None)):
            errors.append(f"{plugin.__name__}.build_runner is not callable.")

    phase_names = _check_phases(target, errors, warnings)
    _check_phase_capabilities(target, errors)
    _check_runner(target, namespace, errors, warnings, len(phase_names))
    _check_checkpoint_topology_contract(
        target,
        "\n".join(sources.values()),
        errors,
        strict=strict_cache_contract,
    )
    if _has_custom_runner(target):
        _check_resume_does_not_restart("\n".join(sources.values()), errors)
        _check_error_recovery_contract(
            "\n".join(sources.values()),
            errors,
            strict=strict_cache_contract,
        )
        _check_phase_lifecycle_contract(
            "\n".join(sources.values()),
            errors,
            strict=strict_cache_contract,
        )
    cache_contract = _check_prompt_cache_contract(
        sources,
        target,
        errors,
        strict=strict_cache_contract,
    )
    source_text = "\n".join(sources.values())
    custom_runner = _has_custom_runner(target)
    integration_evidence = _check_optional_integrations(
        target,
        errors,
        warnings,
        available_integrations=available_integrations,
    )
    error_text = " ".join(errors).lower()

    def has_error(*markers: str) -> bool:
        return any(marker in error_text for marker in markers)

    categories = {
        "source": "pass",
        "plugin": "fail"
        if has_error(
            ".name",
            ".description",
            "mode_bindings",
            "build_params",
            "build_runner",
            "builtin workflow",
        )
        else "pass"
        if plugins
        else "fail",
        "phase_graph": "fail"
        if has_error("phase ", "phases[", "phase graph", "phase name")
        else "pass"
        if phase_names
        else "warning",
        "runner": "fail"
        if has_error("runner", "checkpoint codec", "resume(", "attach_context")
        else "pass"
        if custom_runner
        else "generic",
        "capability": "fail" if any("capability" in error.lower() for error in errors) else "pass",
        "cache_contract": "pass"
        if cache_contract in {"contract-native", "generic-runner"}
        else cache_contract,
        "question_policy": (
            "pass"
            if not custom_runner or "ask_user" in source_text
            else "fail"
            if strict_cache_contract
            else "warning"
        ),
        "transition_tools": "fail"
        if any("transition" in error.lower() for error in errors)
        else "pass",
        "workspace": "fail" if has_error("workspace") else "pass",
        "browser": "fail" if has_error("browser") else "pass",
        "mcp": "fail" if has_error("mcp") else "not_probed",
        "checkpoint": "pass"
        if not custom_runner
        and not has_error("checkpoint")
        or (
            _overrides_plugin_method(target, "checkpoint_context_to_payload")
            and _overrides_plugin_method(target, "checkpoint_context_from_payload")
            and not has_error("checkpoint")
        )
        else "fail",
        "phase_lifecycle": "fail"
        if any(
            marker in error_text
            for marker in (
                "phase lifecycle",
                "phaseannotation",
                "boundary checkpoint",
                "phase annotation",
            )
        )
        else "pass"
        if not custom_runner or strict_cache_contract
        else "warning",
        "resume": "fail"
        if has_error("resume")
        else "pass"
        if not custom_runner or "async def resume" in source_text
        else "fail",
        "failure_recovery": "fail"
        if has_error("failure", "swallow", "finalizer")
        else "pass"
        if not custom_runner or "attach_context" in source_text
        else "fail",
        "optional_dependencies": str(integration_evidence.get("status", "not_declared")),
        "manifest": "package" if is_package else "legacy",
    }
    if errors:
        categories["result"] = "fail"
    else:
        categories["result"] = "pass"

    return ValidationReport(
        path=shown,
        ok=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        plugin_names=plugin_names,
        phase_names=phase_names,
        cache_contract=cache_contract,
        categories=categories,
        evidence={
            "source_files": [str(item) for item in sorted(sources)],
            "source_file_count": len(sources),
            "plugin_count": len(plugins),
            "phase_count": len(phase_names),
            "custom_runner": custom_runner,
            "phase_capabilities": [
                {
                    "name": phase.name,
                    "allowed": [
                        str(value.value if hasattr(value, "value") else value)
                        for value in (phase.allowed_capabilities or ())
                    ],
                    "override": [
                        str(value.value if hasattr(value, "value") else value)
                        for value in (phase.allowed_capabilities_override or ())
                    ],
                }
                for phase in (target.phases if isinstance(target.phases, list) else ())
                if hasattr(phase, "name")
            ],
            "optional_integrations": integration_evidence,
        },
        skipped_checks=(
            ("optional browser/MCP runtime health is not probed by static validation",)
            if available_integrations is None
            else ()
        ),
    )
