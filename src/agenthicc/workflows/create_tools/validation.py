"""Check the exact generated modules before isolated loader discovery."""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from agenthicc.plugins.authored import _source_bytes
from agenthicc.plugins.authored_types import content_digest
from agenthicc.plugins.deps import missing_requirements
from agenthicc.workflows.authoring.import_sandbox import inspect_generated_python
from agenthicc.workflows.authoring.verdict import Finding, ValidationReport
from agenthicc.workflows.create_tools.plan import MAX_MODULE_BYTES

if TYPE_CHECKING:
    from agenthicc.workflows.create_tools.plan import ToolPlan

_Function = ast.FunctionDef | ast.AsyncFunctionDef
_Definition = _Function | ast.ClassDef
_CAPABILITIES = {
    "tool_read": {"read"},
    "tool_write": {"write"},
    "tool_execute": {"execute"},
    "tool_git_read": {"git_read"},
    "tool_git_write": {"git_write"},
    "tool_network": {"network"},
    "tool_search": {"search"},
    "tool_read_search": {"read", "search"},
    "tool_network_read": {"network", "read"},
    "tool_network_write": {"network", "write"},
    "tool_network_search": {"network", "search"},
}
_FILE_OPS = {
    "open",
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
    "unlink",
    "iterdir",
    "glob",
    "mkdir",
    "rename",
}
_PRIVATE_CLIENTS = {"httpx.AsyncClient", "httpx.Client", "requests.Session"}
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_NOT_LITERAL = object()
_CAPABILITY_MODULE = "agenthicc.tools.capabilities."


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _imports(tree: ast.Module) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(
                {
                    entry.asname or entry.name.split(".")[0]: (
                        entry.name if entry.asname else entry.name.split(".")[0]
                    )
                    for entry in node.names
                }
            )
        if isinstance(node, ast.ImportFrom) and node.module:
            names.update(
                {entry.asname or entry.name: f"{node.module}.{entry.name}" for entry in node.names}
            )
    return names


def _qualified(node: ast.AST, names: dict[str, str]) -> str:
    name = _name(node)
    first, separator, suffix = name.partition(".")
    return names.get(first, first) + separator + suffix


def _binding(node: ast.stmt) -> tuple[str, ast.expr | None]:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return _name(node.targets[0]), node.value
    if isinstance(node, ast.AnnAssign):
        return _name(node.target), node.value
    return "", None


def _literal(value: ast.expr | None) -> object:
    if value is not None:
        try:
            return ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            return _NOT_LITERAL
    return None


def _tool_decorator(node: _Definition, imports: dict[str, str]) -> ast.Call | None:
    return next(
        (
            item
            for item in node.decorator_list
            if isinstance(item, ast.Call)
            and _qualified(item.func, imports) in {"lauren_ai.tool", "lauren_ai._tools.tool"}
        ),
        None,
    )


def _capability_decorator(node: ast.expr, imports: dict[str, str]) -> set[str] | None:
    if not isinstance(node, ast.Call):
        name = _qualified(node, imports)
        return (
            _CAPABILITIES.get(name.removeprefix(_CAPABILITY_MODULE))
            if name.startswith(_CAPABILITY_MODULE)
            else None
        )
    if _qualified(node.func, imports) not in {
        "lauren_ai.set_metadata",
        "lauren_ai._tools.set_metadata",
    }:
        return None
    if len(node.args) != 2 or node.keywords or _literal(node.args[0]) != "capabilities":
        return None
    values = node.args[1]
    if (
        not isinstance(values, ast.Call)
        or _qualified(values.func, imports) != "frozenset"
        or len(values.args) != 1
        or values.keywords
        or not isinstance(values.args[0], ast.Set)
    ):
        return None
    qualified = {_qualified(item, imports) for item in values.args[0].elts}
    permitted = {
        f"{_CAPABILITY_MODULE}ToolCapability.{capability.upper()}": capability
        for capabilities in _CAPABILITIES.values()
        for capability in capabilities
    }
    if not qualified or qualified - permitted.keys():
        return None
    return {permitted[name] for name in qualified}


def _definition_execution(node: _Definition, imports: dict[str, str]) -> bool:
    for item in node.decorator_list:
        key = _qualified(item.func if isinstance(item, ast.Call) else item, imports)
        if _capability_decorator(item, imports) is not None:
            continue
        if isinstance(item, ast.Call):
            if key not in {"lauren_ai.tool", "lauren_ai._tools.tool"} or item.args:
                return True
            if any(not isinstance(argument.value, ast.Constant) for argument in item.keywords):
                return True
        elif key not in {
            "staticmethod",
            "classmethod",
            "property",
        }:
            return True
    if isinstance(node, ast.ClassDef):
        return (
            bool(node.keywords)
            or any(
                _qualified(base, imports)
                not in {"typing.TypedDict", "typing_extensions.TypedDict", "object"}
                for base in node.bases
            )
            or any(_runs_on_import(item, imports) for item in node.body)
        )
    defaults = (*node.args.defaults, *node.args.kw_defaults)
    annotations = [
        node.returns,
        *(
            arg.annotation
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        ),
    ]
    return any(_literal(value) is _NOT_LITERAL for value in defaults) or any(
        isinstance(child, ast.Call)
        for annotation in annotations
        if annotation is not None
        for child in ast.walk(annotation)
    )


def _runs_on_import(node: ast.stmt, imports: dict[str, str]) -> bool:
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
        return False
    if isinstance(node, ast.Expr):
        return not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
    if isinstance(node, _DEFINITIONS):
        return _definition_execution(node, imports)
    name, value = _binding(node)
    if name == "TOOLS":
        return not isinstance(value, ast.List) or any(
            not isinstance(item, ast.Name) for item in value.elts
        )
    if name and "." not in name:
        return (
            any(isinstance(child, ast.Call) for child in ast.walk(node))
            or _literal(value) is _NOT_LITERAL
        )
    return True


def _annotation(node: ast.expr | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace(" ", "")
    return ast.unparse(node).replace(" ", "")


def _default(value: ast.expr | None) -> str:
    return ast.dump(value) if value is not None else ""


def _signature(function: _Function, *, class_form: bool) -> dict[str, tuple[str, str]]:
    args = function.args
    regular = list(
        zip(
            args.args,
            [None] * (len(args.args) - len(args.defaults)) + list(args.defaults),
            strict=True,
        )
    )
    if class_form and regular and regular[0][0].arg == "self":
        regular.pop(0)
    return {
        arg.arg: (
            _annotation(arg.annotation),
            _default(value),
        )
        for arg, value in (*regular, *zip(args.kwonlyargs, args.kw_defaults, strict=True))
    }


def _body_checks(
    node: _Definition, function: _Function, plan: ToolPlan, imports: dict[str, str], path: str
) -> list[Finding]:
    findings: list[Finding] = []

    def report(code: str, message: str, *, warning: bool = False) -> None:
        findings.append(
            Finding(code, "warning" if warning else "error", path, function.lineno, message)
        )

    expected = {
        item.name: (
            _annotation(ast.parse(item.type_hint, mode="eval").body),
            _default(ast.parse(item.default, mode="eval").body) if item.default else "",
        )
        for item in plan.parameters
    }
    signature = _signature(function, class_form=plan.class_form)
    if (
        signature != expected
        or function.args.vararg
        or function.args.kwarg
        or function.args.posonlyargs
    ):
        report(
            "tool.signature_mismatch",
            f"Parameters and defaults for {plan.name} must match its plan.",
        )
    annotations = (_annotation(function.returns), *(value[0] for value in signature.values()))
    if any(re.search(r"\bAny\b", value) for value in annotations):
        report("tool.any_type", f"{plan.name} must use concrete annotations.")
    result = annotations[0]
    if not result or (
        not result.startswith("dict[str,")
        and imports.get(result, "") not in {"typing.TypedDict", "typing_extensions.TypedDict"}
        and result not in imports.get("__typed_dicts__", "").split(",")
    ):
        report("tool.return_type", f"{plan.name} must return an annotated JSON object.")
    doc = ast.get_docstring(function) or ""
    if not doc or any(
        not re.search(rf"^\s*{item.name}:\s*\S", doc, re.MULTILINE) for item in plan.parameters
    ):
        report("tool.docstring", f"Document each parameter of {plan.name} in its Args section.")
    declarations = [
        caps
        for decorator in node.decorator_list
        if (caps := _capability_decorator(decorator, imports)) is not None
    ]
    calls = {
        _qualified(call.func, imports) for call in ast.walk(function) if isinstance(call, ast.Call)
    }
    needed: set[str] = set()
    if any(
        name.startswith(("httpx.", "requests.", "urllib.request."))
        or name.endswith(".agenthicc_http_client")
        for name in calls
    ):
        needed.add("network")
    if any(
        name.startswith("subprocess.") or name in {"os.system", "os.popen", "exec", "eval"}
        for name in calls
    ):
        needed.add("execute")
    file_access = {name.split(".")[-1] for name in calls} & _FILE_OPS
    if file_access and not any(name.endswith(".WorkspaceView") for name in calls):
        report(
            "tool.capability_mismatch", "Use WorkspaceView to resolve paths for filesystem access."
        )
    if calls & _PRIVATE_CLIENTS:
        report("tool.private_http_client", "Use the shared agenthicc_http_client.")
    if declarations != [set(plan.capabilities)] or needed - set(plan.capabilities):
        report(
            "tool.capability_mismatch",
            f"Capability declarations for {plan.name} must match its plan and usage.",
        )
    if (file_access or "network" in needed) and not (
        any("limit" in item.name for item in plan.parameters)
        or any(
            isinstance(node.slice, ast.Slice) and node.slice.upper is not None
            for node in ast.walk(function)
            if isinstance(node, ast.Subscript)
        )
    ):
        report(
            "tool.unbounded_output",
            f"Bound file and network results from {plan.name}.",
            warning=True,
        )
    return findings


def _static_checks(source: bytes, path: str, plans: tuple[ToolPlan, ...]) -> tuple[Finding, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return (
            Finding("tool.syntax", "error", path, exc.lineno, "Module does not parse as Python."),
        )
    findings: list[Finding] = []
    names = _imports(tree)
    names["__typed_dicts__"] = ",".join(
        item.name
        for item in tree.body
        if isinstance(item, ast.ClassDef)
        and any(
            _qualified(base, names) in {"typing.TypedDict", "typing_extensions.TypedDict"}
            for base in item.bases
        )
    )
    for stmt in tree.body:
        binding = stmt.name if isinstance(stmt, _DEFINITIONS) else _binding(stmt)[0]
        if binding in names or binding in {
            "frozenset",
            "staticmethod",
            "classmethod",
            "property",
            "object",
        }:
            findings.append(
                Finding(
                    "tool.import_side_effect",
                    "error",
                    path,
                    stmt.lineno,
                    "A module definition shadows an imported or reserved name.",
                )
            )
        elif _runs_on_import(stmt, names):
            findings.append(
                Finding(
                    "tool.import_side_effect",
                    "error",
                    path,
                    stmt.lineno,
                    "Move executable code to a tool body.",
                )
            )
    bindings = dict(_binding(item) for item in tree.body)
    export = bindings.get("TOOLS")
    exports = [_name(item) for item in export.elts] if isinstance(export, ast.List) else []
    definitions = {node.name: node for node in tree.body if isinstance(node, _DEFINITIONS)}
    planned = {item.name: item for item in plans}
    checked: set[str] = set()
    for exported in exports:
        node = definitions.get(exported)
        decorator = _tool_decorator(node, names) if node is not None else None
        attrs = (
            {kw.arg: _literal(kw.value) for kw in decorator.keywords}
            if decorator is not None
            else {}
        )
        declared_name = attrs.get("name", exported)
        if (
            not isinstance(declared_name, str)
            or declared_name not in planned
            or declared_name in checked
            or node is None
            or decorator is None
        ):
            findings.append(
                Finding(
                    "tool.name_mismatch",
                    "error",
                    path,
                    node.lineno if node is not None else None,
                    "Export must declare a unique planned tool name.",
                )
            )
            continue
        plan = planned[declared_name]
        checked.add(declared_name)
        if class_form := isinstance(node, ast.ClassDef):
            function = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "run"
                ),
                None,
            )
        else:
            function = node
        if (
            function is None
            or class_form != plan.class_form
            or attrs.get("requires_confirmation", False) is not plan.requires_confirmation
        ):
            findings.append(
                Finding(
                    "tool.signature_mismatch",
                    "error",
                    path,
                    node.lineno,
                    f"Tool form or confirmation differs from the plan for {plan.name}.",
                )
            )
            continue
        findings.extend(_body_checks(node, function, plan, names, path))
    if checked != set(planned) or len(exports) != len(planned):
        findings.append(
            Finding(
                "tool.export_mismatch",
                "error",
                path,
                None,
                "TOOLS must export the complete plan exactly once.",
            )
        )
    dependencies = _literal(bindings.get("DEPENDENCIES"))
    expected = {dependency for plan in plans for dependency in plan.dependencies}
    if dependencies is None:
        dependencies = []
    if (
        not isinstance(dependencies, (list, tuple))
        or any(not isinstance(item, str) for item in dependencies)
        or set(dependencies) != expected
    ):
        findings.append(
            Finding(
                "tool.dependency",
                "error",
                path,
                None,
                "DEPENDENCIES must match the plans in this module.",
            )
        )
    elif missing_requirements(list(dependencies)):
        findings.append(
            Finding(
                "tool.dependency", "warning", path, None, "A declared dependency is not installed."
            )
        )
    return tuple(findings)


def _preview_tools(arguments: dict[str, object]) -> dict[str, object]:
    from agenthicc.commands import build_builtin_registry
    from agenthicc.kernel import AppState, EventProcessor
    from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
    from agenthicc.runners.reload import ReloadService
    from agenthicc.subagents.types import DEFAULT_REGISTRY
    from agenthicc.workflows.registry import WorkflowRegistry

    expected = arguments["modules"]
    assert isinstance(expected, dict)
    stage = Path("/workspace") / str(arguments["stage"])
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / ".agenthicc"
        tools = project / "tools"
        tools.mkdir(parents=True)
        for name, digest in expected.items():
            filename = f"{name}.py"
            source = _source_bytes(stage / filename)
            if content_digest({f".agenthicc/tools/{filename}": source}) != digest:
                return {"errors": [filename]}
            (tools / filename).write_bytes(source)
        service = ReloadService(
            processor=EventProcessor(AppState.create(), persist=False),
            project_dir=project,
            user_dir=root / "user",
            workflow_registry=WorkflowRegistry(),
            command_registry=build_builtin_registry(),
            subagent_registry=DEFAULT_REGISTRY.copy(),
        )
        trust = ExtensionTrust(store_path=root / "trust.json", trusted_roots=(tools,))
        with override_extension_trust(trust):
            report = asyncio.run(service.preview(frozenset({"tools"})))
        return {
            "names": [change.name for change in report.changes if change.kind == "added"],
            "errors": [Path(item.path).name for item in report.diagnostics],
        }


def validate_modules(
    workspace: Path, stage: Path, plans: tuple[ToolPlan, ...]
) -> tuple[dict[str, ValidationReport], bool]:
    """Scan the actual staging inventory and preview it without trusting its bytes."""
    expected = {plan.module for plan in plans}
    entries = {item.name: item for item in stage.iterdir()}
    unexpected = set(entries) - {f"{module}.py" for module in expected}
    reports: dict[str, ValidationReport] = {}
    for module in sorted(expected):
        name = f"{module}.py"
        destination = f".agenthicc/tools/{name}"
        findings: list[Finding] = []
        source = b""
        try:
            source = _source_bytes(entries[name])
            if len(source) > MAX_MODULE_BYTES:
                raise ValueError("Module exceeds the size limit.")
        except (KeyError, OSError, ValueError):
            findings.append(
                Finding(
                    "tool.syntax",
                    "error",
                    name,
                    None,
                    "Expected a regular Python module within the size limit.",
                )
            )
        if not findings:
            findings.extend(
                _static_checks(source, name, tuple(plan for plan in plans if plan.module == module))
            )
        if unexpected:
            findings.append(
                Finding(
                    "tool.export_mismatch",
                    "error",
                    name,
                    None,
                    "Staging contains entries outside the plan.",
                )
            )
        reports[module] = ValidationReport(content_digest({destination: source}), tuple(findings))
    if any(report.errors for report in reports.values()):
        return reports, False

    preview = inspect_generated_python(
        workspace,
        _preview_tools,
        {
            "stage": stage.relative_to(workspace).as_posix(),
            "modules": {module: report.digest for module, report in reports.items()},
        },
    )
    if preview.status == "static_only":
        return reports, True
    result = preview.result or {}
    names = result.get("names")
    if (
        preview.status == "failed"
        or result.get("errors")
        or not isinstance(names, list)
        or set(names) != {plan.name for plan in plans}
    ):
        reports = {
            module: ValidationReport(
                report.digest,
                (
                    *report.findings,
                    Finding(
                        "tool.load_preview",
                        "error",
                        f"{module}.py",
                        None,
                        preview.reason
                        or "The isolated tool loader did not discover the planned set.",
                    ),
                ),
            )
            for module, report in reports.items()
        }
    return reports, False
