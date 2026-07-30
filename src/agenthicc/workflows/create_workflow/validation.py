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
import importlib.util
import logging
import sys
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
    }
)


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
                    "cloakbrowser_* tools so policy and checkpoint boundaries remain enforced."
                )
        if isinstance(node, ast.Name) and node.id.startswith("cloakbrowser_"):
            if node.id not in _KNOWN_BROWSER_TOOLS:
                errors.append(
                    f"Unknown browser tool {node.id!r}; use the session-provided canonical "
                    "cloakbrowser_* tool names documented by describe_cloakbrowser_tools()."
                )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value.startswith("cloakbrowser_") and value not in _KNOWN_BROWSER_TOOLS:
                errors.append(
                    f"Unknown browser tool {value!r}; use the session-provided canonical "
                    "cloakbrowser_* tool names documented by describe_cloakbrowser_tools()."
                )


@dataclasses.dataclass(frozen=True)
class ValidationReport:
    """Outcome of deterministically validating one generated workflow file.

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

    def render(self) -> str:
        """Render the report as the text block shown to the validating agent."""
        lines: list[str] = ["[DETERMINISTIC VALIDATION REPORT]"]
        lines.append(f"file: {self.path or '(unresolved)'}")
        lines.append(f"result: {'PASS' if self.ok else 'FAIL'}")
        if self.plugin_names:
            lines.append(f"workflows found: {', '.join(self.plugin_names)}")
        if self.phase_names:
            lines.append(f"phases: {' → '.join(self.phase_names)}")
        if self.errors:
            lines.append("")
            lines.append(f"errors ({len(self.errors)}) — these MUST be fixed:")
            lines.extend(f"  {i}. {err}" for i, err in enumerate(self.errors, 1))
        if self.warnings:
            lines.append("")
            lines.append(f"warnings ({len(self.warnings)}):")
            lines.extend(f"  {i}. {warn}" for i, warn in enumerate(self.warnings, 1))
        if self.ok and not self.warnings:
            lines.append("")
            lines.append("The file imports cleanly and its phase graph is consistent.")
        return "\n".join(lines)


def _fail(path: str, *errors: str) -> ValidationReport:
    """Return a not-ok report carrying *errors* only."""
    return ValidationReport(path=path, ok=False, errors=tuple(errors))


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

    module_name = f"_agenthicc_create_workflow_validate_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
    except (OSError, ValueError) as exc:
        return [], {}, f"{type(exc).__name__}: {exc}"
    if spec is None or spec.loader is None:
        return [], {}, f"Python could not build an import spec for {path}."

    module = importlib.util.module_from_spec(spec)
    # Registered before exec_module so decorators can resolve the module during
    # class creation, exactly like the real loader does.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — generated code, any failure
        return [], {}, f"{type(exc).__name__}: {exc}"
    finally:
        sys.modules.pop(module_name, None)

    found: list[type[WorkflowPlugin]] = [
        obj
        for _name, obj in inspect.getmembers(module, inspect.isclass)
        if obj is not _Plugin and issubclass(obj, _Plugin) and getattr(obj, "name", "") != ""
    ]
    return found, dict(vars(module)), ""


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
            "this workflow file."
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


def validate_workflow_file(
    path: str,
    *,
    expected_name: str = "",
    root: Path | None = None,
) -> ValidationReport:
    """Deterministically validate the workflow plugin file at *path*.

    :param path: Path the generation phase reported writing, absolute or relative
        to *root*.
    :param expected_name: When non-empty, the approved workflow name the file must
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
            "No workflow file path was recorded, so there is nothing to validate. "
            "Write the workflow file and call mark_generation_complete(summary, path).",
        )

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return _fail(raw, f"The path {raw!r} could not be resolved: {type(exc).__name__}: {exc}")

    shown = str(resolved)
    if not resolved.is_relative_to(base):
        return _fail(
            shown,
            f"{shown} is outside the workspace root {base}; the workflow file must be written "
            "inside the project, normally at .agenthicc/workflows/<name>.py.",
        )
    if not resolved.exists():
        return _fail(
            shown,
            f"No file exists at {shown}. Write the complete workflow source to that path "
            "with the write tools before marking generation complete.",
        )
    if not resolved.is_file():
        return _fail(shown, f"{shown} is a directory, not a Python file.")
    if resolved.suffix != ".py":
        return _fail(
            shown,
            f"{shown} does not end in .py, so the workflow loader will skip it. "
            "Write the workflow to .agenthicc/workflows/<name>.py.",
        )
    if resolved.name.startswith("_"):
        return _fail(
            shown,
            f"{resolved.name} starts with an underscore, and the workflow loader skips those "
            "files. Rename it to <name>.py.",
        )

    try:
        source = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(shown, f"{shown} could not be read as UTF-8 text: {type(exc).__name__}: {exc}")
    if not source.strip():
        return _fail(shown, f"{shown} is empty; the complete workflow source was never written.")

    try:
        compile(source, shown, "exec")
    except SyntaxError as exc:
        return _fail(
            shown,
            f"{shown} has a syntax error on line {exc.lineno}: {exc.msg}. "
            "Fix the source and rewrite the file.",
        )

    source_errors: list[str] = []
    _check_browser_imports(source, source_errors)
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
    if expected_name and resolved.stem != expected_name:
        warnings.append(
            f"The file is named {resolved.name} but the workflow is {expected_name!r}; "
            f"{expected_name}.py is the conventional filename."
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
    _check_runner(target, namespace, errors, warnings, len(phase_names))

    return ValidationReport(
        path=shown,
        ok=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        plugin_names=plugin_names,
        phase_names=phase_names,
    )
