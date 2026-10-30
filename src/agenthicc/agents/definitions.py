"""Agent definitions written in Markdown (AGX-09 §5.1).

A definition is a Markdown file whose frontmatter declares the agent and whose
body is its system prompt::

    ---
    name: reviewer
    description: Reviews a diff for correctness and security.
    mode: subagent            # primary | subagent | all
    model: claude-sonnet-4-6  # or a provider profile name; empty = the session's
    tools: [read_file, grep_files, git_diff]
    capabilities: [read, search, git_read]
    max_steps: 30
    ceiling: Plan             # may only restrict the session's mode
    color: cyan
    hidden: false             # hide from the @ picker
    ---

    You are a careful code reviewer…

The frontmatter is a strict subset of YAML: ``key: value`` lines, inline
``[a, b]`` or block ``- item`` lists, quoted or plain strings, booleans and
integers. Every problem — a missing field, an unknown key, a value of the wrong
type — is a :class:`DefinitionError` naming the file and line; nothing unknown
is silently ignored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.subagents.types import SubagentPermissionConstraints

from agenthicc.result_contracts import JSONMapping, parse_output_schema

__all__ = [
    "AGENT_MODES",
    "CAPABILITY_NAMES",
    "CEILINGS",
    "FIELDS",
    "AgentSpec",
    "DefinitionError",
    "parse_definition",
    "render_definition",
]

#: Where an agent can be used.
AGENT_MODES = ("primary", "subagent", "all")
#: Modes a definition may restrict the session to (never widen it).
CEILINGS = ("Safe", "Plan", "Yolo")
#: Capability classes a ``capabilities`` allow-list may name.
CAPABILITY_NAMES = (
    "read",
    "search",
    "git_read",
    "write",
    "git_write",
    "execute",
    "network",
    "control",
)
#: Every frontmatter key a definition may set.
FIELDS = (
    "name",
    "description",
    "mode",
    "model",
    "effort",
    "context",
    "output",
    "tools",
    "capabilities",
    "max_steps",
    "ceiling",
    "color",
    "hidden",
    "override",
    "permissions",
)

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$")
_COLOR = re.compile(r"^[a-z_]{1,32}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.*-]{1,128}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
_MAX_CONTEXT_TOKENS = 10_000_000

Scalar = str | int | bool
FrontValue = Scalar | list[Scalar] | JSONMapping


class DefinitionError(ValueError):
    """A problem with one definition file, at a line (1 is the first line)."""

    def __init__(self, path: Path | None, line: int, message: str) -> None:
        self.path = path
        self.line = line
        self.message = message
        where = f"{path}:{line}" if path is not None else f"line {line}"
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One agent definition, validated."""

    name: str
    description: str
    #: The system prompt (the Markdown body); empty adds nothing to the base prompt.
    prompt: str = ""
    mode: str = "all"
    #: A model ID or provider profile; empty uses the session model or subagent default.
    model: str = ""
    #: Tool names (or ``group.*`` patterns) the agent may use; ``None`` means
    #: every tool the session allows.
    tools: tuple[str, ...] | None = None
    #: Capability classes the agent may use; ``None`` means every one the mode allows.
    capabilities: tuple[str, ...] | None = None
    max_steps: int | None = None
    #: A mode the agent restricts the session to; empty adds no restriction.
    ceiling: str = ""
    color: str = ""
    hidden: bool = False
    #: Optional per-type constraints that can only narrow inherited permissions.
    permissions: SubagentPermissionConstraints | None = None
    #: ``builtin``, ``user`` or ``project``.
    source: str = "builtin"
    path: Path | None = None
    #: Explicit permission to shadow a built-in subagent type.
    override: bool = False
    #: Reasoning effort passed through provider request options for subagents.
    effort: str = ""
    #: Per-worker memory budget in tokens; clamped to the selected model window.
    context: int | None = None
    #: Optional JSON Schema object required from a structured worker result.
    output_schema: JSONMapping | None = None

    @property
    def primary(self) -> bool:
        """Whether the agent can be the one the session talks to."""
        return self.mode in ("primary", "all")

    @property
    def subagent(self) -> bool:
        """Whether the agent can be invoked as a subagent (``@agent:NAME``)."""
        return self.mode in ("subagent", "all")


def _split(text: str, path: Path | None) -> tuple[list[tuple[int, str]], str]:
    """Frontmatter lines (with their line numbers) and the body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DefinitionError(path, 1, "a definition starts with a '---' frontmatter line")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            front = [(number + 2, line) for number, line in enumerate(lines[1:index])]
            body = "\n".join(lines[index + 1 :]).strip("\n")
            return front, body.strip()
    raise DefinitionError(path, 1, "the frontmatter has no closing '---' line")


def _strip_comment(value: str) -> str:
    """*value* without a trailing ``# comment`` outside quotes."""
    quote = ""
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def _scalar(raw: str, path: Path | None, line: int) -> Scalar:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value.startswith(("[", "{", "|", ">", "&", "*", "!")):
        raise DefinitionError(path, line, f"unsupported value {value!r}")
    return value


def _inline_list(raw: str, path: Path | None, line: int) -> list[Scalar]:
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    current = ""
    quote = ""
    for char in inner:
        if quote:
            current += char
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current += char
        elif char == ",":
            items.append(current)
            current = ""
        else:
            current += char
    if quote:
        raise DefinitionError(path, line, "an unclosed quote in the list")
    items.append(current)
    return [_scalar(item, path, line) for item in items if item.strip()]


def _parse_front(
    front: list[tuple[int, str]], path: Path | None
) -> tuple[dict[str, FrontValue], dict[str, int]]:
    """Frontmatter values and the line each top-level or permission key is on."""
    values: dict[str, FrontValue] = {}
    lines: dict[str, int] = {}
    current_list: list[Scalar] | None = None
    current_list_indent = 0
    permissions: dict[str, Scalar | list[Scalar]] | None = None
    for number, raw in front:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if stripped.startswith("- ") or stripped == "-":
            if current_list is None or indent < current_list_indent:
                raise DefinitionError(path, number, "a list item outside a list")
            current_list.append(_scalar(_strip_comment(stripped[1:]), path, number))
            continue
        if indent == 2 and permissions is not None:
            match = _KEY.match(stripped)
            if match is None:
                raise DefinitionError(path, number, f"expected 'key: value', found {stripped!r}")
            key, rest = match.group(1), _strip_comment(match.group(2))
            if key in permissions:
                raise DefinitionError(path, number, f"permissions.{key!s} is set twice")
            lines[f"permissions.{key}"] = number
            if not rest:
                nested_list: list[Scalar] = []
                permissions[key] = nested_list
                current_list = nested_list
                current_list_indent = 4
            elif rest.startswith("[") and rest.endswith("]"):
                permissions[key] = _inline_list(rest, path, number)
                current_list = None
            else:
                permissions[key] = _scalar(rest, path, number)
                current_list = None
            continue
        if indent:
            if current_list is None or indent < current_list_indent:
                raise DefinitionError(path, number, "unexpected indentation")
        else:
            match = _KEY.match(stripped)
            if match is None:
                raise DefinitionError(path, number, f"expected 'key: value', found {stripped!r}")
            key, rest = match.group(1), _strip_comment(match.group(2))
            if key in values:
                raise DefinitionError(path, number, f"{key!r} is set twice")
            lines[key] = number
            current_list = None
            permissions = None
            if key == "output":
                try:
                    values[key] = parse_output_schema(rest)
                except ValueError as exc:
                    raise DefinitionError(path, number, f"output: {exc}") from exc
            elif not rest and key == "permissions":
                nested: dict[str, Scalar | list[Scalar]] = {}
                values[key] = nested
                permissions = nested
            elif not rest:
                current: list[Scalar] = []
                values[key] = current
                current_list = current
                current_list_indent = 1
            elif rest.startswith("[") and rest.endswith("]"):
                values[key] = _inline_list(rest, path, number)
            else:
                values[key] = _scalar(rest, path, number)
    return values, lines


def _text(
    values: dict[str, FrontValue],
    lines: dict[str, int],
    key: str,
    path: Path | None,
    *,
    limit: int,
) -> str:
    value = values.get(key, "")
    if isinstance(value, list) and not value:
        value = ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DefinitionError(path, lines.get(key, 1), f"{key} must be text")
    text = str(value).strip()
    if len(text) > limit:
        raise DefinitionError(path, lines.get(key, 1), f"{key} is longer than {limit} characters")
    return text


def _names(
    values: dict[str, FrontValue],
    lines: dict[str, int],
    key: str,
    path: Path | None,
) -> tuple[str, ...] | None:
    if key not in values:
        return None
    value = values[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DefinitionError(path, lines[key], f"{key} must be a list of names")
    return tuple(str(item).strip() for item in value)


def parse_definition(text: str, *, path: Path | None = None, source: str = "builtin") -> AgentSpec:
    """The definition in *text*, or a :class:`DefinitionError` at its line."""
    front, body = _split(text, path)
    values, lines = _parse_front(front, path)
    for key in values:
        if key not in FIELDS:
            hint = ""
            close = [name for name in FIELDS if name[:3] == key[:3]]
            if close:
                hint = f" (did you mean {close[0]!r}?)"
            raise DefinitionError(path, lines[key], f"unknown key {key!r}{hint}")
    for required in ("name", "description"):
        if not values.get(required):
            raise DefinitionError(path, lines.get(required, 1), f"{required} is required")
    name = _text(values, lines, "name", path, limit=64)
    if not _NAME.match(name):
        raise DefinitionError(
            path,
            lines["name"],
            "name must be lowercase letters, digits, '-' or '_' (64 at most)",
        )
    description = _text(values, lines, "description", path, limit=500)
    mode = _text(values, lines, "mode", path, limit=16) or "all"
    if mode not in AGENT_MODES:
        raise DefinitionError(path, lines["mode"], f"mode must be one of {', '.join(AGENT_MODES)}")
    model = _text(values, lines, "model", path, limit=200)
    if model and model.casefold() != "inherit" and not _MODEL.fullmatch(model):
        raise DefinitionError(path, lines["model"], "model must be an identifier or 'inherit'")
    effort = _text(values, lines, "effort", path, limit=16).casefold()
    if effort and effort not in _EFFORTS:
        raise DefinitionError(path, lines["effort"], f"effort must be one of {', '.join(_EFFORTS)}")
    context: int | None = None
    if "context" in values:
        raw_context = values["context"]
        if (
            isinstance(raw_context, bool)
            or not isinstance(raw_context, int)
            or not 1 <= raw_context <= _MAX_CONTEXT_TOKENS
        ):
            raise DefinitionError(
                path,
                lines["context"],
                f"context must be a whole number from 1 to {_MAX_CONTEXT_TOKENS}",
            )
        context = raw_context
    tools = _names(values, lines, "tools", path)
    for tool in tools or ():
        if not _TOOL.match(tool):
            raise DefinitionError(path, lines["tools"], f"{tool!r} is not a tool name")
    capabilities = _names(values, lines, "capabilities", path)
    for capability in capabilities or ():
        if capability not in CAPABILITY_NAMES:
            raise DefinitionError(
                path,
                lines["capabilities"],
                f"{capability!r} is not a capability ({', '.join(CAPABILITY_NAMES)})",
            )
    max_steps: int | None = None
    if "max_steps" in values:
        raw_steps = values["max_steps"]
        if (
            isinstance(raw_steps, bool)
            or not isinstance(raw_steps, int)
            or not 1 <= raw_steps <= 1_000
        ):
            raise DefinitionError(
                path, lines["max_steps"], "max_steps must be a whole number from 1 to 1000"
            )
        max_steps = raw_steps
    ceiling = _text(values, lines, "ceiling", path, limit=16)
    if ceiling:
        canonical = next((item for item in CEILINGS if item.casefold() == ceiling.casefold()), "")
        if not canonical:
            raise DefinitionError(
                path, lines["ceiling"], f"ceiling must be one of {', '.join(CEILINGS)}"
            )
        ceiling = canonical
    color = _text(values, lines, "color", path, limit=32)
    if color and not _COLOR.match(color):
        raise DefinitionError(path, lines["color"], "color must be a colour name such as cyan")
    hidden = values.get("hidden", False)
    if not isinstance(hidden, bool):
        raise DefinitionError(path, lines["hidden"], "hidden must be true or false")
    override = values.get("override", False)
    if not isinstance(override, bool):
        raise DefinitionError(path, lines["override"], "override must be true or false")
    output_schema = values.get("output")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise DefinitionError(path, lines["output"], "output must be a JSON Schema object")
    permissions: SubagentPermissionConstraints | None = None
    raw_permissions = values.get("permissions")
    if raw_permissions is not None:
        if not isinstance(raw_permissions, dict):
            raise DefinitionError(
                path, lines["permissions"], "permissions must be a nested mapping"
            )
        permission_fields = {
            "deny_tools",
            "deny_capabilities",
            "require_approval",
            "workspace_paths",
        }
        for key in raw_permissions:
            if key not in permission_fields:
                raise DefinitionError(
                    path,
                    lines.get(f"permissions.{key}", lines["permissions"]),
                    f"unknown permissions key {key!r}",
                )

        def permission_names(key: str, *, capabilities: bool = False) -> frozenset[str]:
            raw_names = raw_permissions.get(key, [])
            line = lines.get(f"permissions.{key}", lines["permissions"])
            if not isinstance(raw_names, list) or not all(
                isinstance(item, str) for item in raw_names
            ):
                raise DefinitionError(path, line, f"permissions.{key} must be a list of names")
            names = frozenset(str(item).strip() for item in raw_names)
            for name in names:
                if not name:
                    raise DefinitionError(
                        path, line, f"permissions.{key} entries must not be empty"
                    )
                if capabilities and name not in CAPABILITY_NAMES:
                    raise DefinitionError(
                        path,
                        line,
                        f"{name!r} is not a capability ({', '.join(CAPABILITY_NAMES)})",
                    )
            return names

        denied_tools = permission_names("deny_tools")
        for tool in denied_tools:
            if not _TOOL.match(tool):
                raise DefinitionError(
                    path,
                    lines.get("permissions.deny_tools", lines["permissions"]),
                    f"permissions.deny_tools pattern {tool!r} is not a tool name",
                )
        denied_capabilities = permission_names("deny_capabilities", capabilities=True)
        required_approvals = permission_names("require_approval", capabilities=True)
        workspace_paths: tuple[str, ...] | None = None
        if "workspace_paths" in raw_permissions:
            raw_paths = raw_permissions["workspace_paths"]
            line = lines.get("permissions.workspace_paths", lines["permissions"])
            if not isinstance(raw_paths, list) or not all(
                isinstance(item, str) for item in raw_paths
            ):
                raise DefinitionError(
                    path, line, "permissions.workspace_paths must be a list of paths"
                )
            paths = tuple(str(item).strip() for item in raw_paths)
            if any(not item or "\x00" in item for item in paths):
                raise DefinitionError(
                    path, line, "permissions.workspace_paths entries must be non-empty paths"
                )
            workspace_paths = paths
        from agenthicc.subagents.types import (
            SubagentPermissionConstraints as _PermissionConstraints,
        )

        permissions = _PermissionConstraints(
            deny_tools=denied_tools,
            deny_capabilities=denied_capabilities,
            require_approval=required_approvals,
            workspace_paths=workspace_paths,
        )
    if permissions is not None and mode == "primary":
        raise DefinitionError(
            path,
            lines["permissions"],
            "permissions constraints apply only to subagent definitions",
        )
    return AgentSpec(
        name=name,
        description=description,
        prompt=body,
        mode=mode,
        model=model,
        tools=tools,
        capabilities=capabilities,
        max_steps=max_steps,
        ceiling=ceiling,
        color=color,
        hidden=hidden,
        permissions=permissions,
        source=source,
        path=path,
        override=override,
        effort=effort,
        context=context,
        output_schema=output_schema,
    )


def _quoted(value: str) -> str:
    """*value* as a frontmatter scalar: plain when it reads back unchanged."""
    plain = (
        bool(value)
        and value == value.strip()
        and re.fullmatch(r"[A-Za-z0-9_./@:+ ,()'-]+", value) is not None
        and value not in ("true", "false")
        and re.fullmatch(r"-?\d+", value) is None
        and not value.startswith(("-", "'"))
    )
    if plain:
        return value
    escaped = value.replace('"', "'")
    return f'"{escaped}"'


def _list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_quoted(value) for value in values) + "]"


def render_definition(spec: AgentSpec) -> str:
    """*spec* as a Markdown definition that :func:`parse_definition` reads back."""
    lines = ["---", f"name: {spec.name}", f"description: {_quoted(spec.description)}"]
    lines.append(f"mode: {spec.mode}")
    if spec.model:
        lines.append(f"model: {_quoted(spec.model)}")
    if spec.effort:
        lines.append(f"effort: {spec.effort}")
    if spec.context is not None:
        lines.append(f"context: {spec.context}")
    if spec.output_schema is not None:
        output = json.dumps(
            spec.output_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        lines.append(f"output: {output}")
    if spec.tools is not None:
        lines.append(f"tools: {_list(spec.tools)}")
    if spec.capabilities is not None:
        lines.append(f"capabilities: {_list(spec.capabilities)}")
    if spec.max_steps is not None:
        lines.append(f"max_steps: {spec.max_steps}")
    if spec.ceiling:
        lines.append(f"ceiling: {spec.ceiling}")
    if spec.color:
        lines.append(f"color: {spec.color}")
    if spec.hidden:
        lines.append("hidden: true")
    if spec.override:
        lines.append("override: true")
    if spec.permissions is not None:
        lines.append("permissions:")
        if spec.permissions.deny_tools:
            lines.append(f"  deny_tools: {_list(sorted(spec.permissions.deny_tools))}")
        if spec.permissions.deny_capabilities:
            lines.append(
                f"  deny_capabilities: {_list(sorted(spec.permissions.deny_capabilities))}"
            )
        if spec.permissions.require_approval:
            lines.append(f"  require_approval: {_list(sorted(spec.permissions.require_approval))}")
        if spec.permissions.workspace_paths is not None:
            lines.append(f"  workspace_paths: {_list(spec.permissions.workspace_paths)}")
    lines.append("---")
    lines.append("")
    if spec.prompt:
        lines.append(spec.prompt)
    return "\n".join(lines).rstrip("\n") + "\n"
