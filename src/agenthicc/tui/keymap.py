"""One registry of named key actions per screen context (AGX-13).

Screens no longer hard-code keys, footer hints and help separately. Each
:class:`Action` names what a key does in one *context* (``composer``,
``agents.list`` …), its default keys, the short label shown in footer hints and
help, and the *conditions* under which it applies — named flags a screen
reports about its current state (``running``, ``needs_approval`` …). A screen
dispatches with :meth:`Keymap.resolve`, and draws hints with
:meth:`Keymap.hints` and help with :meth:`Keymap.help`, from the same entries,
so the three cannot disagree.

Users rebind keys in ``~/.agenthicc/keybindings.toml`` (see
:func:`load_keybindings`); ``"none"`` removes an action's keys. A leader key
(``leader = "ctrl+t"``) enables two-step bindings such as ``"<leader>a"``.

Within a context two actions may share a key only when their conditions are
mutually exclusive; anything else is a :class:`KeymapError`. A context may
inherit another's actions (:data:`PARENTS`): a key bound in the more specific
context shadows the inherited binding of that key.
"""

from __future__ import annotations

import os
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

__all__ = [
    "CONTEXTS",
    "Action",
    "Dispatcher",
    "HelpEntry",
    "Hint",
    "Keymap",
    "KeymapError",
    "KeymapLoad",
    "default_keybindings_path",
    "display_key",
    "load_keybindings",
    "normalize_key",
    "template",
]

#: Screen contexts. ``global`` actions apply in every context after its own.
CONTEXTS = (
    "global",
    "composer",
    "agents",
    "agents.list",
    "agents.peek",
    "agents.detail",
    "transcript",
    "subagents",
    "overlay",
)

#: Contexts that inherit another context's actions (after their own): the
#: agent operations in ``agents`` apply on the list, in the peek panel and in
#: the detail view; the peek panel also keeps the list's navigation.
PARENTS: Mapping[str, str] = {
    "agents.list": "agents",
    "agents.peek": "agents.list",
    "agents.detail": "agents",
}

LEADER = "<leader>"
_UNBIND = "none"

_ALIASES = {
    "esc": "escape",
    "return": "enter",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "page_up": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "page_down": "pagedown",
    "ctrl+enter": "ctrl+j",
    "bs": "backspace",
    " ": "space",
}
_MODIFIERS = ("ctrl", "alt", "shift", "meta")
_NAMED_KEYS = frozenset(
    {
        "enter",
        "tab",
        "escape",
        "backspace",
        "delete",
        "insert",
        "space",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
        "focus_in",
        "focus_out",
        # ← pressed on an empty composer (AGX-05 §5.2).
        "left_on_empty",
        *(f"f{index}" for index in range(1, 13)),
    }
)
_DISPLAY = {
    "enter": "Enter",
    "tab": "Tab",
    "escape": "Esc",
    "backspace": "Backspace",
    "delete": "Del",
    "insert": "Ins",
    "space": "Space",
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
    "home": "Home",
    "end": "End",
    "pageup": "PgUp",
    "pagedown": "PgDn",
    "left_on_empty": "← on empty",
}


class KeymapError(ValueError):
    """An invalid binding or a conflict between two actions."""


def normalize_key(key: str) -> str:
    """Return the canonical form of a binding (``"Ctrl+Y"`` → ``"ctrl+y"``).

    Single characters keep their case (``"y"`` and ``"Y"`` differ, and
    ``"shift+y"`` is ``"Y"``); modifier names are lower-case and ordered
    ``ctrl+alt+shift+meta``. Raises :class:`KeymapError` for anything that is
    not a key.
    """
    if not isinstance(key, str) or not key:
        raise KeymapError("a key binding must be a non-empty string")
    leader = key.startswith(LEADER)
    body = key[len(LEADER) :] if leader else key
    if not body:
        raise KeymapError(f"{key!r} names no key after {LEADER}")
    if len(body) == 1:
        name = "space" if body == " " else body
        return LEADER + name if leader else name
    body = _ALIASES.get(body.lower(), body)
    parts = [*body[:-2].split("+"), "+"] if body.endswith("++") else body.split("+")
    *modifiers, base = parts
    modifiers = [modifier.lower() for modifier in modifiers]
    if any(modifier not in _MODIFIERS for modifier in modifiers):
        raise KeymapError(f"{key!r} has an unknown modifier")
    if len(base) == 1:
        if modifiers == ["shift"] and base.isalpha():
            return (LEADER if leader else "") + base.upper()
        if modifiers and base.isalpha():
            if "shift" not in modifiers and base.isupper() and modifiers != ["ctrl"]:
                modifiers.append("shift")
            base = base.lower()
    else:
        base = _ALIASES.get(base.lower(), base.lower())
        if base not in _NAMED_KEYS:
            raise KeymapError(f"{key!r} is not a key")
    name = "+".join([*(m for m in _MODIFIERS if m in modifiers), base])
    return LEADER + name if leader else name


def display_key(key: str, *, leader: str | None = None) -> str:
    """Return a key the way hints and help show it (``"ctrl+g"`` → ``"Ctrl+G"``)."""
    if key.startswith(LEADER):
        prefix = display_key(leader) if leader else "Leader"
        return f"{prefix} {display_key(key[len(LEADER) :])}"
    parts = key.split("+") if len(key) > 1 else [key]
    base = parts[-1]
    label = _DISPLAY.get(base, base.upper() if len(base) == 1 and parts[:-1] else base)
    if len(base) > 1 and base.startswith("f") and base[1:].isdigit():
        label = base.upper()
    modifiers = [modifier.capitalize() for modifier in parts[:-1]]
    return "+".join([*modifiers, label])


@dataclass(frozen=True)
class Action:
    """One thing a key can do on one screen."""

    name: str
    context: str
    keys: tuple[str, ...]
    #: Short label for hints and help ("approve").
    label: str
    #: Higher shows first in footer hints; 0 keeps the action out of hints.
    hint_priority: int = 0
    #: One line for the help overlay; defaults to the label.
    description: str = ""
    #: Conditions that must all hold for the action to apply.
    when: frozenset[str] = frozenset()
    #: Conditions under which the action does not apply.
    unless: frozenset[str] = frozenset()
    #: A fixed key (a trigger character) that users cannot rebind.
    rebindable: bool = True
    #: Another action whose first key is shown before this one's in its hint
    #: ("↑↓ select").
    paired: str = ""

    def applies(self, conditions: frozenset[str]) -> bool:
        return self.when <= conditions and not (self.unless & conditions)

    def excludes(self, other: Action) -> bool:
        """Whether this action and *other* can never apply at the same time."""
        return bool(self.when & other.unless or other.when & self.unless)


@dataclass(frozen=True)
class Hint:
    keys: str
    label: str
    #: The action the hint runs (a click on a footer hint runs it, AGX-13 §5.4).
    action: str = ""


@dataclass(frozen=True)
class HelpEntry:
    action: str
    keys: str
    label: str
    description: str
    conditions: str
    source: str


@dataclass(frozen=True)
class Keymap:
    """Actions with their effective keys, checked for conflicts."""

    actions: tuple[Action, ...]
    leader: str | None = None
    leader_timeout_s: float = 2.0
    #: Action name → where its keys come from ("default" or the bindings file).
    sources: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        seen: dict[str, Action] = {}
        for action in self.actions:
            if action.context not in CONTEXTS:
                raise KeymapError(f"{action.name}: unknown context {action.context!r}")
            if action.name in seen:
                raise KeymapError(f"{action.name} is defined twice")
            seen[action.name] = action
            normalized = tuple(normalize_key(key) for key in action.keys)
            if normalized != action.keys:
                raise KeymapError(f"{action.name}: keys must be normalized ({action.keys!r})")
            if any(key.startswith(LEADER) for key in action.keys) and self.leader is None:
                raise KeymapError(f"{action.name}: {LEADER} binding without a leader key")
        by_key: dict[tuple[str, str], list[Action]] = {}
        for action in self.actions:
            for key in action.keys:
                by_key.setdefault((action.context, key), []).append(action)
        for (context, key), actions in by_key.items():
            for index, first in enumerate(actions):
                for second in actions[index + 1 :]:
                    if not first.excludes(second):
                        raise KeymapError(
                            f"{display_key(key)} is bound to both {first.name} and "
                            f"{second.name} in {context}"
                        )
        if self.leader is not None:
            for action in self.actions:
                if self.leader in action.keys:
                    raise KeymapError(
                        f"{action.name} uses the leader key {display_key(self.leader)}"
                    )

    @classmethod
    def build(
        cls,
        actions: Iterable[Action],
        *,
        overrides: Mapping[str, tuple[str, ...]] | None = None,
        leader: str | None = None,
        leader_timeout_s: float = 2.0,
        source: str = "default",
    ) -> Keymap:
        """Return a keymap with *overrides* (action name → keys) applied."""
        overrides = overrides or {}
        defined: dict[str, Action] = {}
        for action in actions:
            if action.name in defined:
                raise KeymapError(f"{action.name} is defined twice")
            defined[action.name] = action
        unknown = sorted(set(overrides) - set(defined))
        if unknown:
            raise KeymapError(f"unknown action {unknown[0]!r}")
        resolved: list[Action] = []
        sources: dict[str, str] = {}
        for name, action in defined.items():
            if name in overrides:
                if not action.rebindable:
                    raise KeymapError(f"{name} cannot be rebound")
                resolved.append(replace(action, keys=overrides[name]))
                sources[name] = source
            else:
                resolved.append(action)
                sources[name] = "default"
        return cls(
            tuple(resolved),
            leader=None if leader is None else normalize_key(leader),
            leader_timeout_s=leader_timeout_s,
            sources=sources,
        )

    def action(self, name: str) -> Action:
        for action in self.actions:
            if action.name == name:
                return action
        raise KeymapError(f"unknown action {name!r}")

    def _in(self, context: str) -> list[Action]:
        """The actions of *context*: its own, then inherited ones, then ``global``.

        An inherited action loses the keys a more specific context binds; one
        left without keys does not apply in *context* at all.
        """
        chain = list(_lineage(context))
        if context != "global":
            chain.append("global")
        actions: list[Action] = []
        claimed: set[str] = set()
        for index, name in enumerate(chain):
            own = [action for action in self.actions if action.context == name]
            for action in own:
                if index == 0:
                    actions.append(action)
                    continue
                keys = tuple(key for key in action.keys if key not in claimed)
                if keys:
                    actions.append(action if keys == action.keys else replace(action, keys=keys))
            claimed.update(key for action in own for key in action.keys)
        return actions

    def resolve(
        self, context: str, key: str, conditions: frozenset[str] = frozenset()
    ) -> Action | None:
        """Return the action *key* triggers in *context* under *conditions*."""
        if not key:
            return None
        for action in self._in(context):
            if key in action.keys and action.applies(conditions):
                return action
        return None

    def leads(self, context: str, conditions: frozenset[str] = frozenset()) -> bool:
        """Whether *context* has a two-step ``<leader>`` binding that applies."""
        return self.leader is not None and any(
            key.startswith(LEADER) and action.applies(conditions)
            for action in self._in(context)
            for key in action.keys
        )

    def hints(
        self, context: str, conditions: frozenset[str] = frozenset(), *, limit: int | None = None
    ) -> list[Hint]:
        """Return footer hints: applicable actions with keys, by priority."""
        ranked = sorted(
            (
                action
                for action in self._in(context)
                if action.hint_priority > 0 and action.keys and action.applies(conditions)
            ),
            key=lambda action: -action.hint_priority,
        )
        hints = [Hint(self._hint_keys(action), action.label, action.name) for action in ranked]
        return hints if limit is None else hints[:limit]

    def _hint_keys(self, action: Action) -> str:
        keys = display_key(action.keys[0], leader=self.leader)
        if action.paired:
            try:
                partner = self.action(action.paired)
            except KeymapError:
                return keys
            if partner.keys:
                return display_key(partner.keys[0], leader=self.leader) + keys
        return keys

    def hint(self, name: str) -> Hint | None:
        """The hint for one action regardless of priority (``? help``, ``q quit``)."""
        action = self.action(name)
        if not action.keys:
            return None
        return Hint(self._hint_keys(action), action.label, action.name)

    def help(self, context: str) -> list[HelpEntry]:
        """Return every action of *context* (and ``global``) for the help overlay."""
        entries: list[HelpEntry] = []
        for action in self._in(context):
            conditions = ", ".join(
                [*sorted(action.when), *(f"not {flag}" for flag in sorted(action.unless))]
            )
            entries.append(
                HelpEntry(
                    action=action.name,
                    keys=" / ".join(display_key(key, leader=self.leader) for key in action.keys)
                    or "unbound",
                    label=action.label,
                    description=action.description or action.label,
                    conditions=conditions,
                    source=self.sources.get(action.name, "default"),
                )
            )
        return entries


class Dispatcher:
    """Resolve keystrokes for one screen, following a leader key across two presses."""

    def __init__(self, keymap: Keymap, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.keymap = keymap
        self._now = clock
        self._leader_at: float | None = None

    @property
    def awaiting_leader(self) -> bool:
        return self._leader_at is not None

    def dispatch(
        self, context: str, key: str, conditions: frozenset[str] = frozenset()
    ) -> Action | None:
        """Return the action for *key*, or ``None`` (also while a leader waits)."""
        keymap = self.keymap
        if self._leader_at is not None:
            started, self._leader_at = self._leader_at, None
            if self._now() - started <= keymap.leader_timeout_s:
                return keymap.resolve(context, LEADER + key, conditions)
        if key and key == keymap.leader and keymap.leads(context, conditions):
            self._leader_at = self._now()
            return None
        return keymap.resolve(context, key, conditions)


# ── the bindings file ────────────────────────────────────────────────────────


def default_keybindings_path() -> Path:
    """``~/.agenthicc/keybindings.toml`` (``AGENTHICC_KEYBINDINGS`` overrides it)."""
    configured = os.environ.get("AGENTHICC_KEYBINDINGS")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".agenthicc" / "keybindings.toml"


@dataclass(frozen=True)
class KeymapLoad:
    """The effective keymap and what loading the bindings file reported."""

    keymap: Keymap
    path: Path
    errors: tuple[str, ...] = ()
    #: The file's (inode, size, mtime) when it was read; zeros when absent.
    fingerprint: tuple[int, int, int] = (0, 0, 0)


def _fingerprint(path: Path) -> tuple[int, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0, 0)
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _parse_keys(value: object, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return () if value.lower() == _UNBIND else (normalize_key(value),)
    if isinstance(value, list):
        keys: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise KeymapError(f"{where}: every key must be a string")
            if item.lower() != _UNBIND:
                keys.append(normalize_key(item))
        return tuple(dict.fromkeys(keys))
    raise KeymapError(f'{where}: expected a key, a list of keys or "none"')


def load_keybindings(actions: Iterable[Action], path: Path | None = None) -> KeymapLoad:
    """Build the keymap from the defaults and the user's bindings file.

    A missing file means defaults. A file that does not parse, names an
    unknown context or action, or produces a conflict is ignored as a whole:
    the defaults apply and the problems are reported in ``errors``.
    """
    defaults = tuple(actions)
    path = path or default_keybindings_path()
    fingerprint = _fingerprint(path)
    default_keymap = Keymap.build(defaults)
    if fingerprint == (0, 0, 0):
        return KeymapLoad(default_keymap, path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return KeymapLoad(default_keymap, path, (f"{path}: {exc}",), fingerprint)
    errors: list[str] = []
    overrides: dict[str, tuple[str, ...]] = {}
    leader: str | None = None
    timeout_s = 2.0
    contexts = {action.name: action.context for action in defaults}
    for name, value in data.items():
        if name == "leader":
            if isinstance(value, str) and value.lower() != _UNBIND:
                try:
                    leader = normalize_key(value)
                except KeymapError as exc:
                    errors.append(f"leader: {exc}")
            elif value is not None and not (isinstance(value, str) and value.lower() == _UNBIND):
                errors.append("leader: expected a key")
            continue
        if name == "leader_timeout_ms":
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 10_000:
                timeout_s = value / 1_000.0
            else:
                errors.append("leader_timeout_ms: expected 100–10000")
            continue
        if not isinstance(value, dict):
            errors.append(f"{name}: expected a [context] table of action = keys")
            continue
        for context, bindings in _tables(name, value):
            if context not in CONTEXTS:
                errors.append(f"[{context}]: unknown context")
                continue
            for action_name, keys in bindings.items():
                where = f"[{context}] {action_name}"
                # A section may also rebind an action its context inherits.
                if contexts.get(action_name) not in _lineage(context):
                    errors.append(f"{where}: no such action in this context")
                    continue
                try:
                    overrides[action_name] = _parse_keys(keys, where)
                except KeymapError as exc:
                    errors.append(str(exc))
    if not errors:
        try:
            keymap = Keymap.build(
                defaults,
                overrides=overrides,
                leader=leader,
                leader_timeout_s=timeout_s,
                source=str(path),
            )
        except KeymapError as exc:
            errors.append(str(exc))
        else:
            return KeymapLoad(keymap, path, (), fingerprint)
    return KeymapLoad(default_keymap, path, tuple(errors), fingerprint)


def _tables(name: str, value: Mapping[str, object]) -> list[tuple[str, dict[str, object]]]:
    """A section's bindings and those of its nested sections, by dotted name.

    TOML reads ``[agents.list]`` as a ``list`` table inside ``agents``; a
    quoted action name such as ``"agents.approve"`` stays a single key.
    """
    bindings = {key: item for key, item in value.items() if not isinstance(item, dict)}
    found = [(name, bindings)] if bindings or not value else []
    for key, item in value.items():
        if isinstance(item, dict):
            found.extend(_tables(f"{name}.{key}", item))
    return found


def _lineage(context: str) -> tuple[str, ...]:
    """*context* and the contexts it inherits from."""
    chain = [context]
    while chain[-1] in PARENTS:
        chain.append(PARENTS[chain[-1]])
    return tuple(chain)


def template(actions: Iterable[Action]) -> str:
    """Return a commented bindings file listing every action and its default keys."""
    lines = [
        "# agenthicc key bindings (AGX-13). Uncomment a line to change it; edits apply",
        '# immediately. A value is a key, a list of keys, or "none" to unbind.',
        '# Keys: "y", "Y", "ctrl+g", "alt+x", "shift+up", "enter", "escape", "space",',
        '# "pageup", "f5", and "<leader>a" for two-step bindings.',
        "",
        '# leader = "ctrl+t"',
        "# leader_timeout_ms = 2000",
    ]
    by_context: dict[str, list[Action]] = {}
    for action in actions:
        by_context.setdefault(action.context, []).append(action)
    for context in CONTEXTS:
        entries = [action for action in by_context.get(context, []) if action.rebindable]
        if not entries:
            continue
        lines += ["", f"[{context}]"]
        for action in entries:
            keys = ", ".join(f'"{key}"' for key in action.keys) or '"none"'
            lines.append(f'# "{action.name}" = [{keys}]  # {action.description or action.label}')
    return "\n".join(lines) + "\n"
