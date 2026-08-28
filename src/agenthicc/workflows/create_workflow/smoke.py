"""Bounded, no-network smoke checks for generated workflow packages.

The smoke boundary is intentionally conservative. It validates the generated
package's executable shape and checkpoint contract without invoking a real
provider, browser, MCP server, shell, or network. Static source checks run
before the normal loader boundary; the only generated code executed afterward
is the bounded custom-runner entry point under a fake harness, with external
service imports rejected first.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import dataclasses
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TYPE_CHECKING, Callable, Coroutine, Protocol, TypeVar, cast

if TYPE_CHECKING:
    from agenthicc.workflows.create_workflow.validation import ValidationReport


_SmokeResult = TypeVar("_SmokeResult")


class _SmokeRunner(Protocol):
    """Minimal runner surface exercised without importing generated types."""

    run_phase: Callable[..., Coroutine[Any, Any, object]]

    async def run(self, intent: str) -> object: ...


class _SmokePlugin(Protocol):
    """Minimal generated-plugin surface required by the smoke harness."""

    def build_runner(self, config: object, approval_svc: object | None) -> _SmokeRunner: ...

    def checkpoint_context_to_payload(self, context: object) -> dict[str, object]: ...

    def checkpoint_context_from_payload(
        self, payload: dict[str, object], memory: object | None = None
    ) -> object: ...


__all__ = ["SmokeCheck", "SmokeReport", "run_generated_workflow_smoke"]

_FORBIDDEN_IMPORT_ROOTS = frozenset(
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


@dataclasses.dataclass(frozen=True, slots=True)
class SmokeCheck:
    """One deterministic smoke assertion."""

    category: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"category": self.category, "status": self.status, "detail": self.detail}


@dataclasses.dataclass(frozen=True, slots=True)
class SmokeReport:
    """Structured result of the generated-workflow smoke contract."""

    path: str
    ok: bool
    checks: tuple[SmokeCheck, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def skipped(self) -> tuple[SmokeCheck, ...]:
        return tuple(check for check in self.checks if check.status == "skipped")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
            "errors": list(self.errors),
            "skipped": [check.to_dict() for check in self.skipped],
        }

    def render(self) -> str:
        lines = ["[GENERATED WORKFLOW SMOKE REPORT]", f"path: {self.path}"]
        lines.append(f"result: {'PASS' if self.ok else 'FAIL'}")
        for check in self.checks:
            lines.append(f"- {check.category}: {check.status} — {check.detail}")
        lines.extend(f"error: {error}" for error in self.errors)
        return "\n".join(lines)


def _source_paths(path: Path) -> tuple[Path, ...]:
    if path.is_file():
        return (path,)
    if path.is_dir():
        return tuple(
            sorted(
                item
                for item in path.rglob("*.py")
                if item.is_file() and "__pycache__" not in item.parts
            )
        )
    return ()


def _forbidden_imports(source: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            modules = []
        for module in modules:
            root = module.split(".", 1)[0]
            if root in _FORBIDDEN_IMPORT_ROOTS:
                found.add(module)
    return tuple(sorted(found))


def _has_call(source: str, attribute: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        for node in ast.walk(tree)
    )


def _has_name(source: str, name: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(isinstance(node, ast.Name) and node.id == name for node in ast.walk(tree))


def _run_async(factory: Callable[[], Coroutine[Any, Any, _SmokeResult]]) -> _SmokeResult:
    """Run a smoke coroutine even when validation already owns an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="agenthicc-smoke") as pool:
        future: Future[_SmokeResult] = pool.submit(lambda: asyncio.run(factory()))
        return future.result()


def _fake_config() -> SimpleNamespace:
    """Build the smallest session-shaped config a generated runner needs."""

    class _Handle:
        """In-memory handle that proves generated boundary calls are durable-shaped."""

        def __init__(self) -> None:
            self.run_id = "smoke-run"
            self.calls: list[tuple[str, object]] = []
            self.checkpoint_supported = True

        def attach_context(self, context: object) -> None:
            self.calls.append(("attach", context))

        def update_phase(
            self,
            phase: str | None,
            index: int,
            iteration: int,
            *,
            persist: bool = True,
        ) -> None:
            self.calls.append(("update", (phase, index, iteration, persist)))

        def save_checkpoint(self, *, reason: str = "") -> SimpleNamespace:
            self.calls.append(("checkpoint", reason))
            return SimpleNamespace(revision=len(self.calls))

        def mark_terminal(self, _status: str, *, error: str = "") -> None:
            self.calls.append(("terminal", error))

        def is_pause_requested(self) -> bool:
            return False

    class _Execution:
        def effective_model(self) -> str:
            return "smoke-model"

        def effective_usable_budget(self) -> int:
            return 16_000

    handle = _Handle()
    return SimpleNamespace(
        agent_runner=SimpleNamespace(
            _transport=SimpleNamespace(_config=SimpleNamespace(model="smoke-model"))
        ),
        cfg=SimpleNamespace(execution=_Execution()),
        workflow_handle=handle,
        session_memory=object(),
        conversation_id="smoke-conversation",
        params=None,
    )


def _tool_arguments(tool: object) -> dict[str, object]:
    """Create bounded fixture arguments from a transition tool signature."""
    try:
        signature = inspect.signature(cast(Callable[..., object], tool))
    except (TypeError, ValueError):
        return {}
    values: dict[str, object] = {}
    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls"} or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        annotation = str(parameter.annotation).lower()
        values[parameter.name] = [] if "list" in annotation else "smoke fixture"
    return values


async def _exercise_custom_runner(plugin: _SmokePlugin) -> tuple[object, object, bool, str]:
    """Run a custom runner against fake turns and return context/checkpoint data."""
    from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities  # noqa: PLC0415
    from agenthicc.runners.prompt_contract import tool_name  # noqa: PLC0415

    config = _fake_config()
    build_runner = getattr(plugin, "build_runner")
    runner = build_runner(config, None)
    calls: dict[str, int] = {}

    async def fake_run_phase(**kwargs: object) -> None:
        prompt = str(kwargs.get("system_prompt", "")).lower()
        phase = next(
            (
                name
                for name in ("plan", "design", "verify", "generate", "review", "report")
                if name in prompt
            ),
            "phase",
        )
        calls[phase] = calls.get(phase, 0) + 1
        # The first fake response is prose-only. The phase must remain in its
        # inner loop; only the second response calls a control tool.
        if calls[phase] == 1:
            return
        tools = kwargs.get("tools")
        if not isinstance(tools, list):
            return
        control = next(
            (
                candidate
                for candidate in tools
                if ToolCapability.CONTROL in get_tool_capabilities(candidate)
                and tool_name(candidate) != "ask_user"
            ),
            None,
        )
        if control is None:
            return
        result = control(**_tool_arguments(control))
        if inspect.isawaitable(result):
            await result

    runner.run_phase = fake_run_phase
    context = await runner.run("bounded smoke intent")
    handle = config.workflow_handle
    boundary_reasons = [
        str(value)
        for name, value in handle.calls
        if name == "checkpoint" and str(value).startswith("phase_boundary:")
    ]
    if not boundary_reasons:
        raise ValueError(
            "fake handle observed no completed-phase boundary checkpoint; "
            "phase-entry persistence alone is insufficient"
        )
    codec = getattr(plugin, "checkpoint_context_to_payload")
    payload = codec(context)
    json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if not isinstance(payload, dict) or "shared_memory" in payload:
        raise ValueError("checkpoint payload contains runtime session memory")
    restored = getattr(plugin, "checkpoint_context_from_payload")(
        payload,
        memory=config.session_memory,
    )
    if getattr(restored, "shared_memory", None) is not config.session_memory:
        raise ValueError("checkpoint restore did not reattach supplied session memory")
    if getattr(restored, "run_id", None) != getattr(context, "run_id", None):
        raise ValueError("checkpoint restore changed run identity")
    return (
        context,
        restored,
        bool(calls),
        "fake provider, transition tools, and completed-phase boundary checkpoints exercised",
    )


async def _exercise_error_propagation(plugin: _SmokePlugin) -> bool:
    """Verify an injected phase error is not converted into success."""
    config = _fake_config()
    runner = getattr(plugin, "build_runner")(config, None)

    async def fail_run_phase(**_kwargs: object) -> None:
        raise RuntimeError("smoke injected provider failure")

    runner.run_phase = fail_run_phase
    try:
        result = await runner.run("bounded smoke error")
    except RuntimeError:
        return True
    return getattr(getattr(result, "state", None), "name", "") == "FAILED"


def run_generated_workflow_smoke(
    path: str | Path,
    *,
    expected_name: str = "",
    root: Path | None = None,
) -> SmokeReport:
    """Run a bounded executable-shape smoke check without external effects."""
    from agenthicc.workflows.create_workflow.validation import validate_workflow_file  # noqa: PLC0415

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (root or Path.cwd()) / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return SmokeReport(str(candidate), False, errors=(f"could not resolve path: {exc}",))
    workspace = (root or Path.cwd()).resolve()
    if not resolved.is_relative_to(workspace):
        return SmokeReport(
            str(resolved),
            False,
            errors=(f"workflow path is outside the workspace root {workspace}",),
        )
    if candidate.is_symlink():
        return SmokeReport(
            str(candidate),
            False,
            errors=("workflow smoke refuses a symlinked workflow path",),
        )
    if resolved.is_dir():
        for item in resolved.rglob("*"):
            if item.is_symlink():
                return SmokeReport(
                    str(resolved),
                    False,
                    errors=(f"workflow smoke refuses symlinked entry {item}",),
                )
    source_paths = _source_paths(resolved)
    if not source_paths:
        return SmokeReport(str(resolved), False, errors=("workflow has no Python source files",))
    sources: list[str] = []
    checks: list[SmokeCheck] = []
    for source_path in source_paths:
        try:
            source = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return SmokeReport(str(resolved), False, errors=(f"{source_path}: {exc}",))
        sources.append(source)
        forbidden = _forbidden_imports(source)
        if forbidden:
            return SmokeReport(
                str(resolved),
                False,
                checks=(
                    SmokeCheck(
                        "no_external_calls",
                        "fail",
                        f"forbidden external import(s): {', '.join(forbidden)}",
                    ),
                ),
                errors=("smoke execution cannot permit external service imports",),
            )

    combined = "\n".join(sources)
    checks.append(
        SmokeCheck(
            "no_external_calls",
            "pass",
            "no network, browser, MCP, shell, or subprocess imports are present",
        )
    )
    validation: ValidationReport = validate_workflow_file(
        str(resolved),
        expected_name=expected_name,
        root=root,
        strict_cache_contract=True,
    )
    checks.append(
        SmokeCheck(
            "loader_boundary",
            "pass" if validation.plugin_names else "fail",
            "normal workflow loader discovered the declared plugin"
            if validation.plugin_names
            else "normal workflow loader discovered no plugin",
        )
    )
    checks.append(
        SmokeCheck(
            "phase_graph",
            "pass"
            if validation.phase_names or validation.cache_contract == "contract-native"
            else "skipped",
            "declared phase graph is reachable by static validation"
            if validation.phase_names
            else "custom runner owns the phase graph",
        )
    )
    has_runner = "run_phase(" in combined or "build_workflow_prompt_contract" in combined
    checks.append(
        SmokeCheck(
            "run_phase_boundary",
            "pass" if has_runner or validation.cache_contract == "generic-runner" else "fail",
            "workflow routes agent turns through the supported helper"
            if has_runner
            else "generic runner owns the supported agent-turn boundary"
            if validation.cache_contract == "generic-runner"
            else "workflow does not use the supported agent-turn helper",
        )
    )
    has_transition = _has_name(combined, "tool_control") and _has_name(combined, "Event")
    checks.append(
        SmokeCheck(
            "event_transition",
            "pass" if has_transition else "skipped",
            "transition metadata and event-backed control shape are present"
            if has_transition
            else "declarative or human-only workflow has no custom transition fixture",
        )
    )
    has_resume = _has_call(combined, "resume") or "async def resume" in combined
    checks.append(
        SmokeCheck(
            "resume_shape",
            "pass" if has_resume else "skipped",
            "resume entry point is present for the custom runner"
            if has_resume
            else "generic runner owns resume behavior",
        )
    )
    has_codec = (
        "checkpoint_context_to_payload" in combined
        and "checkpoint_context_from_payload" in combined
    )
    checks.append(
        SmokeCheck(
            "checkpoint_payload",
            "pass" if has_codec or validation.cache_contract == "generic-runner" else "fail",
            "custom checkpoint codec names are present and runtime objects remain framework-owned"
            if has_codec
            else "generic runner checkpoint codec is framework-owned",
        )
    )
    errors = tuple(validation.errors)
    if errors:
        checks.append(SmokeCheck("static_validation", "fail", f"{len(errors)} validation error(s)"))
    else:
        checks.append(SmokeCheck("static_validation", "pass", "deterministic validator passed"))
    runtime_errors: list[str] = []
    if not errors:
        from agenthicc.workflows.loader import load_python_workflows  # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowPlugin  # noqa: PLC0415

        try:
            plugins = load_python_workflows(resolved, source="smoke")
            plugin = next(
                (item for item in plugins if not expected_name or item.name == expected_name),
                plugins[0] if plugins else None,
            )
            if plugin is None:
                raise ValueError("normal loader returned no plugin for smoke execution")
            custom = getattr(plugin.build_runner, "__func__", None) is not getattr(
                WorkflowPlugin.build_runner, "__func__", None
            )
            if custom:
                context, restored, transitions, detail = _run_async(
                    lambda: _exercise_custom_runner(cast(_SmokePlugin, plugin))
                )
                if not transitions:
                    raise ValueError("fake phase driver observed no transition-tool call")
                if (
                    getattr(context, "state", None) is None
                    or getattr(getattr(context, "state", None), "is_terminal", False) is not True
                ):
                    raise ValueError("fake phase driver did not reach a terminal state")
                if getattr(restored, "run_id", None) != getattr(context, "run_id", None):
                    raise ValueError("restored smoke context changed the run identity")
                checks.append(
                    SmokeCheck(
                        "fake_runtime",
                        "pass",
                        f"{detail}; prose-only first turns remained in phase loops",
                    )
                )
                checks.append(
                    SmokeCheck(
                        "phase_boundary_checkpoint",
                        "pass",
                        "fake handle observed at least one post-transition boundary checkpoint",
                    )
                )
                propagated = bool(
                    _run_async(lambda: _exercise_error_propagation(cast(_SmokePlugin, plugin)))
                )
                checks.append(
                    SmokeCheck(
                        "failure_finalization",
                        "pass" if propagated else "fail",
                        "injected provider failure was propagated to the framework boundary"
                        if propagated
                        else "injected provider failure was swallowed or treated as success",
                    )
                )
                if not propagated:
                    runtime_errors.append("generated runner swallowed an injected phase error")
            else:
                checks.append(
                    SmokeCheck(
                        "fake_runtime",
                        "pass",
                        "generic declarative runner owns phase execution and checkpointing",
                    )
                )
                checks.append(
                    SmokeCheck(
                        "failure_finalization",
                        "pass",
                        "generic runner failure handling remains framework-owned",
                    )
                )
        except Exception as exc:  # noqa: BLE001 — generated code is the smoke subject
            runtime_errors.append(f"fake runtime failed: {type(exc).__name__}: {exc}")
            checks.append(SmokeCheck("fake_runtime", "fail", runtime_errors[-1]))
    else:
        checks.append(SmokeCheck("fake_runtime", "skipped", "static validation failed"))
        checks.append(SmokeCheck("failure_finalization", "skipped", "static validation failed"))
    all_errors = (*errors, *runtime_errors)
    return SmokeReport(
        str(resolved),
        not all_errors and all(check.status != "fail" for check in checks),
        tuple(checks),
        all_errors,
    )
