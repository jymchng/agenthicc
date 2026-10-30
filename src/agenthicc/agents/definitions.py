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

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AGENT_MODES",
    "AgentSpec",
    "CAPABILITY_NAMES",
    "CEILINGS",
    "DefinitionError",
    "FIELDS",
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
    "tools",
    "capabilities",
    "max_steps",
    "ceiling",
    "color",
    "hidden",
)

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$")
_COLOR = re.compile(r"^[a-z_]{1,32}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.*-]{1,128}$")

Scalar = str | int | bool


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
    #: A model ID or provider profile name; empty means the session's model.
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
    #: ``builtin``, ``user`` or ``project``.
    source: str = "builtin"
    path: Path | None = None

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
) -> tuple[dict[str, Scalar | list[Scalar]], dict[str, int]]:
    """Frontmatter values and the line each key is on."""
    values: dict[str, Scalar | list[Scalar]] = {}
    lines: dict[str, int] = {}
    current_list: list[Scalar] | None = None
    for number, raw in front:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") or stripped == "-":
            if current_list is None or not raw[:1].isspace():
                raise DefinitionError(path, number, "a list item outside a list")
            current_list.append(_scalar(_strip_comment(stripped[1:]), path, number))
            continue
        if raw[:1].isspace():
            raise DefinitionError(path, number, "unexpected indentation")
        match = _KEY.match(stripped)
        if match is None:
            raise DefinitionError(path, number, f"expected 'key: value', found {stripped!r}")
        key, rest = match.group(1), _strip_comment(match.group(2))
        if key in values:
            raise DefinitionError(path, number, f"{key!r} is set twice")
        lines[key] = number
        current_list = None
        if not rest:
            current_list = []
            values[key] = current_list
        elif rest.startswith("[") and rest.endswith("]"):
            values[key] = _inline_list(rest, path, number)
        else:
            values[key] = _scalar(rest, path, number)
    return values, lines


def _text(
    values: dict[str, Scalar | list[Scalar]],
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
    values: dict[str, Scalar | list[Scalar]],
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
        source=source,
        path=path,
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
    lines.append("---")
    lines.append("")
    if spec.prompt:
        lines.append(spec.prompt)
    return "\n".join(lines).rstrip("\n") + "\n"
