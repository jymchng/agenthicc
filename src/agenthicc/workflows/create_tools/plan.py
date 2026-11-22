"""Validate the complete tool plan before granting access to staging."""

from dataclasses import asdict, dataclass
import ast
import keyword
import re
from typing import TYPE_CHECKING, TypedDict

from agenthicc.tools.capabilities import ToolCapability

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_MODULES = 12
MAX_MODULE_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_CAPABILITIES = frozenset(
    item.value
    for item in ToolCapability
    if item not in (ToolCapability.CONTROL, ToolCapability.UNDECLARED)
)


class ToolParamInput(TypedDict):
    name: str
    type_hint: str
    default: str
    description: str


class ToolPlanInput(TypedDict):
    name: str
    description: str
    module: str | None
    parameters: list[ToolParamInput] | None
    capabilities: list[str]
    dependencies: list[str] | None
    network_hosts: list[str] | None
    requires_confirmation: bool | None
    class_form: bool | None
    update: bool | None


@dataclass(frozen=True, slots=True)
class ToolParam:
    name: str
    type_hint: str
    default: str
    description: str


@dataclass(frozen=True, slots=True)
class ToolPlan:
    name: str
    module: str
    description: str
    parameters: tuple[ToolParam, ...]
    capabilities: tuple[str, ...]
    dependencies: tuple[str, ...]
    network_hosts: tuple[str, ...]
    requires_confirmation: bool
    class_form: bool
    update: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ToolPlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or keyword.iskeyword(value):
        raise ToolPlanError("invalid_plan", f"{field} must be a lowercase Python identifier.")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolPlanError("invalid_plan", f"{field} must be nonempty text.")
    return value.strip()


def _strings(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ToolPlanError("invalid_plan", f"{field} must be a list of strings.")
    return tuple(dict.fromkeys(_text(item, field) for item in value))


def _flag(value: object, field: str) -> bool:
    if value is None:
        return False
    if type(value) is not bool:
        raise ToolPlanError("invalid_plan", f"{field} must be a boolean.")
    return value


def _parameters(value: object) -> tuple[ToolParam, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ToolPlanError("invalid_plan", "parameters must be a list.")
    result: list[ToolParam] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ToolPlanError("invalid_plan", "Each parameter must be an object.")
        name = _identifier(raw.get("name"), "parameter name")
        if name in seen or name == "self":
            raise ToolPlanError("invalid_plan", f"Invalid or repeated parameter {name!r}.")
        seen.add(name)
        annotation = _text(raw.get("type_hint", "str"), "type_hint")
        default = raw.get("default", "")
        if not isinstance(default, str):
            raise ToolPlanError("invalid_plan", "Parameter defaults must be Python literal text.")
        try:
            syntax = ast.parse(annotation, mode="eval")
            if any(isinstance(node, ast.Call) for node in ast.walk(syntax)):
                raise ValueError("Type hints must be concrete.")
            if default:
                ast.literal_eval(default)
        except (ValueError, SyntaxError) as exc:
            raise ToolPlanError(
                "invalid_plan", f"Invalid type hint or default for {name}."
            ) from exc
        result.append(
            ToolParam(name, annotation, default, _text(raw.get("description"), "description"))
        )
    return tuple(result)


def decode_plan(raw: "Mapping[object, object]") -> ToolPlan:
    """Decode model input or the equivalent saved plan fields."""
    name = _identifier(raw.get("name"), "name")
    capabilities = _strings(raw.get("capabilities"), "capabilities")
    if not capabilities or set(capabilities) - _CAPABILITIES:
        raise ToolPlanError(
            "invalid_capabilities",
            f"{name} needs capabilities from {', '.join(sorted(_CAPABILITIES))}.",
        )
    hosts = _strings(raw.get("network_hosts"), "network_hosts")
    for host in hosts:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
            raise ToolPlanError("invalid_plan", "Network hosts must be bare hostnames.")
    if hosts and "network" not in capabilities:
        raise ToolPlanError("invalid_capabilities", f"{name} must declare the network capability.")
    return ToolPlan(
        name,
        _identifier(raw.get("module") or name, "module"),
        _text(raw.get("description"), "description"),
        _parameters(raw.get("parameters")),
        capabilities,
        _strings(raw.get("dependencies"), "dependencies"),
        hosts,
        _flag(raw.get("requires_confirmation"), "requires_confirmation"),
        _flag(raw.get("class_form"), "class_form"),
        _flag(raw.get("update"), "update"),
    )


def plan_set(
    raw: object,
    reserved: frozenset[str],
    prior: "Mapping[str, tuple[str, str]]",
) -> tuple[ToolPlan, ...]:
    """Validate collisions, including updates to an owned module."""
    if not isinstance(raw, list) or not raw:
        raise ToolPlanError("invalid_plan", "Submit at least one tool plan.")
    plans: list[ToolPlan] = []
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ToolPlanError("invalid_plan", "Each tool plan must be an object.")
        plan = decode_plan(item)
        owned = prior.get(plan.name)
        if plan.name in names or plan.name in reserved or (owned is not None and not plan.update):
            raise ToolPlanError("name_collision", f"Tool {plan.name!r} is already in use.")
        relative = f".agenthicc/tools/{plan.module}.py"
        if plan.update and (owned is None or owned[0] != relative):
            raise ToolPlanError(
                "name_collision", f"There is no authored version of {plan.name!r} in {relative}."
            )
        names.add(plan.name)
        plans.append(plan)
    modules = {f".agenthicc/tools/{item.module}.py" for item in plans}
    if len(modules) > MAX_MODULES:
        raise ToolPlanError("module_limit", f"Submit at most {MAX_MODULES} modules.")
    omitted = {name for name, (path, _) in prior.items() if path in modules} - names
    if omitted:
        raise ToolPlanError(
            "name_collision",
            f"Include every tool in an updated module: {', '.join(sorted(omitted))}.",
        )
    return tuple(plans)
