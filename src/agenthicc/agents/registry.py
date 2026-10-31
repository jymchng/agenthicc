"""AgentsRegistry — discover, store, and instantiate named agent definitions (PRD-87, AGX-09).

Every agent is an :class:`~agenthicc.agents.definitions.AgentSpec`. Built-ins
ship as Markdown in ``agenthicc/agents/builtin/``; users add Markdown
definitions to ``~/.agenthicc/agents/`` and projects to ``.agenthicc/agents/``;
trusted Python ``AgentPlugin`` classes from the same directories are converted
to specs when they load. Precedence, lowest first: built-in, Python plugins
(user, then project), user Markdown, project Markdown. The registry holds the
winning spec per name; :attr:`AgentsRegistry.catalog` lists every candidate
with its state — active, shadowed, invalid or untrusted — for the agents
hub's Library and ``agenthicc agents definitions``.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agenthicc.agents.definitions import AgentSpec, DefinitionError, parse_definition
from agenthicc.agents.plugin import AgentPlugin
from agenthicc.tools.base import ToolLike

if TYPE_CHECKING:
    from agenthicc.tools.capabilities import ToolCapability

log = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_DIR",
    "BUILTIN_NAMES",
    "AgentsRegistry",
    "CatalogEntry",
    "build_agents_registry",
]

#: Where the built-in definitions are.
BUILTIN_DIR = Path(__file__).with_name("builtin")
#: The built-in agents, in listing order.
BUILTIN_NAMES = (
    "default",
    "planner",
    "executor",
    "explorer",
    "reviewer",
    "verifier",
    "human",
    "auto",
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One definition the registry considered, whether or not it is in use."""

    name: str
    #: ``builtin``, ``python-user``, ``python-project``, ``user`` or ``project``.
    source: str
    #: ``active``, ``shadowed``, ``invalid`` or ``untrusted``.
    state: str
    spec: AgentSpec | None = None
    path: Path | None = None
    #: The source whose definition of the same name is used instead.
    shadowed_by: str = ""
    error: DefinitionError | None = None
    #: An invalid file whose last valid version is still in use.
    kept_previous: bool = False


def _capability_set(spec: AgentSpec) -> frozenset[ToolCapability] | None:
    from agenthicc.tools.capabilities import ToolCapability

    if spec.capabilities is None:
        return None
    return frozenset(ToolCapability(name) for name in spec.capabilities)


class AgentsRegistry:
    """Maps agent names to their winning :class:`AgentSpec`.

    Later registrations shadow earlier ones (project > user > Python > built-in).
    """

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}
        self._lazy: dict[str, Callable[[], AgentSpec]] = {}
        #: Every definition considered, in discovery order (AGX-09).
        self.catalog: tuple[CatalogEntry, ...] = ()

    def register(self, spec: AgentSpec) -> None:
        existing = self._specs.get(spec.name)
        if existing is not None and existing.source != spec.source:
            log.debug(
                "Agent %r (%s) shadows the %s definition", spec.name, spec.source, existing.source
            )
        self._specs[spec.name] = spec
        self._lazy.pop(spec.name, None)

    def register_lazy(self, name: str, loader: Callable[[], AgentSpec]) -> None:
        """Register a definition that is read only when it is first needed."""
        if name and name not in self._specs:
            self._lazy[name] = loader

    def _materialize(self, name: str) -> AgentSpec | None:
        spec = self._specs.get(name)
        if spec is not None:
            return spec
        loader = self._lazy.get(name)
        if loader is None:
            return None
        spec = loader()
        self.register(spec)
        return spec

    def get(self, name: str) -> AgentSpec | None:
        return self._materialize(name)

    def all(self) -> list[AgentSpec]:
        return [spec for name in self.names() if (spec := self.get(name))]

    def names(self) -> list[str]:
        """Agent names, without reading lazily registered definitions."""
        return [*self._specs.keys(), *[name for name in self._lazy if name not in self._specs]]

    def primary(self) -> list[AgentSpec]:
        """Agents the session can talk to (``primary`` or ``all``, not hidden)."""
        return [spec for spec in self.all() if spec.primary and not spec.hidden]

    def subagents(self, *, include_hidden: bool = False) -> list[AgentSpec]:
        """Agents ``@agent:NAME`` can invoke; hidden ones only by exact name."""
        return [
            spec for spec in self.all() if spec.subagent and (include_hidden or not spec.hidden)
        ]

    def replace_with(self, other: AgentsRegistry) -> None:
        """Replace entries in place while preserving session-owned identity.

        The interactive session gives its workflow configuration a reference
        to one registry object. Deferred discovery and hot reload therefore
        swap the contents rather than replacing that object, just like the
        workflow registry does.
        """
        if other is self:
            return
        self._specs = dict(other._specs)
        self._lazy = dict(other._lazy)
        self.catalog = other.catalog

    def allowed_capabilities(self, agent_type: str) -> frozenset[ToolCapability] | None:
        """The capability classes *agent_type* may use (``None``: what the mode allows)."""
        spec = self.get(agent_type)
        return _capability_set(spec) if spec is not None else None

    def get_role_system_prompt(self, agent_type: str) -> str:
        """The system prompt of *agent_type* (``auto``'s for an unknown type)."""
        spec = self.get(agent_type) or self.get("auto")
        return spec.prompt if spec is not None else ""

    def make_instance(
        self,
        agent_type: str,
        filtered_tools: list[ToolLike],
        model_id: str,
        base_system_prompt: str = "",
    ) -> tuple[object, object]:
        """Create a per-turn (agent_class, instance) for AgentRunnerBase.run_stream().

        The base operating contract comes first, then the agent's prompt.
        A fresh class is created per turn so no shared class is ever mutated.
        """
        from lauren_ai._agents import agent as agent_decorator, use_tools
        from agenthicc.agents.plugin import BASE_SYSTEM_PROMPT

        role_prompt = self.get_role_system_prompt(agent_type)
        effective_base = base_system_prompt or BASE_SYSTEM_PROMPT
        system = f"{effective_base}\n\n{role_prompt}" if role_prompt else effective_base

        @agent_decorator(model=model_id, system=system)
        @use_tools(*filtered_tools)
        class _TurnAgent: ...  # type: ignore[type-var]  # lauren-ai dynamic class

        return _TurnAgent, _TurnAgent()


def _load_builtin(name: str) -> AgentSpec:
    path = BUILTIN_DIR / f"{name}.md"
    return parse_definition(path.read_text(encoding="utf-8"), path=path, source="builtin")


def _builtin_loader(name: str) -> Callable[[], AgentSpec]:
    return lambda: _load_builtin(name)


def build_agents_registry(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    *,
    load_external: bool = True,
    previous: AgentsRegistry | None = None,
) -> AgentsRegistry:
    """Build the registry: built-in, Python plugins, user Markdown, project Markdown.

    With ``load_external=False`` only the built-ins are registered (read when
    first needed); this is the TUI's first-frame form, and the extension
    readiness phase loads the rest. *previous* is the registry being
    replaced: a definition file that no longer parses keeps its last valid
    version from it (hot reload, AGX-09 §5.4).
    """
    if project_dir is None:
        project_dir = Path(".agenthicc")
    if user_dir is None:
        user_dir = Path.home() / ".agenthicc"

    registry = AgentsRegistry()
    entries: list[CatalogEntry] = []
    for name in BUILTIN_NAMES:
        registry.register_lazy(name, _builtin_loader(name))
    if not load_external:
        return registry

    for name in BUILTIN_NAMES:
        entries.append(
            CatalogEntry(
                name, "builtin", "active", spec=registry.get(name), path=BUILTIN_DIR / f"{name}.md"
            )
        )
    kept = {
        spec.path: spec
        for spec in (previous.all() if previous is not None else ())
        if spec.path is not None
    }
    for directory, source in (
        (user_dir / "agents", "python-user"),
        (project_dir / "agents", "python-project"),
    ):
        entries.extend(_scan_python(directory, source, registry))
    for directory, source in ((user_dir / "agents", "user"), (project_dir / "agents", "project")):
        entries.extend(_scan_markdown(directory, source, registry, kept))
    registry.catalog = _mark_shadowed(entries, registry)
    return registry


def _mark_shadowed(
    entries: Iterable[CatalogEntry], registry: AgentsRegistry
) -> tuple[CatalogEntry, ...]:
    """Mark each active entry that is not the registered winner as shadowed."""
    result: list[CatalogEntry] = []
    reported: set[tuple[str, str, str]] = set()
    for entry in entries:
        winner = registry.get(entry.name)
        if entry.state == "active" and winner is not None and entry.spec is not winner:
            entry = replace(entry, state="shadowed", shadowed_by=winner.source)
            report = (entry.name, entry.source, winner.source)
            if report not in reported:
                log.warning(
                    "Agent definition %r from %s is shadowed by %s",
                    entry.name,
                    entry.source,
                    winner.source,
                )
                reported.add(report)
        result.append(entry)
    return tuple(result)


def _trusted(path: Path) -> bool:
    from agenthicc.plugins.trust import current_extension_trust

    return current_extension_trust().permits(path, kind="agent")


def _scan_markdown(
    directory: Path,
    source: str,
    registry: AgentsRegistry,
    kept: dict[Path, AgentSpec],
) -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    builtin_subagent_names: frozenset[str] = frozenset()
    if source == "project":
        from agenthicc.subagents.types import _BUILTIN_REGISTRY

        builtin_subagent_names = frozenset(_BUILTIN_REGISTRY.names())
    try:
        paths = sorted(directory.glob("*.md"))
    except OSError:
        return entries

    parsed: dict[Path, AgentSpec] = {}
    for path in paths:
        if path.name.startswith(("_", ".")):
            continue
        name = path.stem
        if not _trusted(path):
            entries.append(CatalogEntry(name, source, "untrusted", path=path))
            continue
        try:
            parsed[path] = parse_definition(
                path.read_text(encoding="utf-8"), path=path, source=source
            )
        except (DefinitionError, OSError, UnicodeDecodeError) as exc:
            error = (
                exc
                if isinstance(exc, DefinitionError)
                else DefinitionError(path, 1, f"cannot read the file: {exc}")
            )
            previous = kept.get(path)
            if previous is not None:
                registry.register(previous)
            entries.append(
                CatalogEntry(
                    previous.name if previous is not None else name,
                    source,
                    "invalid",
                    spec=previous,
                    path=path,
                    error=error,
                    kept_previous=previous is not None,
                )
            )

    paths_by_name: dict[str, list[Path]] = {}
    for path, spec in parsed.items():
        paths_by_name.setdefault(spec.name, []).append(path)

    for path, spec in parsed.items():
        matching_paths = paths_by_name[spec.name]
        if len(matching_paths) > 1:
            other_paths = [other for other in matching_paths if other != path]
            other_names = ", ".join(str(other) for other in other_paths)
            error = DefinitionError(
                path,
                1,
                f"duplicate agent name {spec.name!r} in {source} definitions; also declared by {other_names}",
            )
            entries.append(CatalogEntry(spec.name, source, "invalid", path=path, error=error))
            continue
        if (
            source == "project"
            and spec.subagent
            and spec.name in builtin_subagent_names
            and not spec.override
        ):
            error = DefinitionError(
                path,
                1,
                f"project definition for built-in subagent type {spec.name!r} "
                "requires override: true",
            )
            entries.append(
                CatalogEntry(spec.name, source, "invalid", spec=spec, path=path, error=error)
            )
            continue
        registry.register(spec)
        entries.append(CatalogEntry(spec.name, source, "active", spec=spec, path=path))
    return entries


def _scan_python(directory: Path, source: str, registry: AgentsRegistry) -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    try:
        paths = sorted(directory.glob("*.py"))
    except OSError:
        return entries
    for path in paths:
        if path.name.startswith("_"):
            continue
        if not _trusted(path):
            entries.append(CatalogEntry(path.stem, source, "untrusted", path=path))
            continue
        try:
            specs = _load_agent_file(path, source)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not hide the others
            log.warning("Failed to load agents from %s: %s", path, exc)
            entries.append(
                CatalogEntry(
                    path.stem,
                    source,
                    "invalid",
                    path=path,
                    error=DefinitionError(path, 1, f"{type(exc).__name__}: {exc}"),
                )
            )
            continue
        for spec in specs:
            registry.register(spec)
            entries.append(CatalogEntry(spec.name, source, "active", spec=spec, path=path))
    return entries


def _load_agent_file(path: Path, source: str) -> list[AgentSpec]:
    """The ``AgentPlugin`` classes of a trusted Python file, as specs."""
    module_name = f"_agenthicc_agent_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agents_list = getattr(module, "AGENTS", None)
    if agents_list:
        classes = [
            candidate
            for candidate in cast(list[object], agents_list)
            if isinstance(candidate, type) and issubclass(candidate, AgentPlugin) and candidate.name
        ]
    else:
        classes = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if obj is not AgentPlugin and issubclass(obj, AgentPlugin) and obj.name
        ]
    return [_plugin_spec(cls, source, path) for cls in classes]


def _plugin_spec(cls: type[AgentPlugin], source: str, path: Path) -> AgentSpec:
    """An ``AgentPlugin`` class as a spec (its ``@agent(system=…)`` is the prompt)."""
    from lauren_ai._agents import AGENT_META

    meta = vars(cls).get(AGENT_META)
    prompt = str(vars(meta).get("system", "") or "") if meta is not None else ""
    doc = inspect.getdoc(cls) or ""
    description = next((line.strip() for line in doc.splitlines() if line.strip()), "")
    capabilities = cls.allowed_capabilities
    return AgentSpec(
        name=cls.replaces or cls.name,
        description=description or f"A Python agent plugin from {path.name}",
        prompt=prompt,
        capabilities=(
            None
            if capabilities is None
            else tuple(sorted(str(item.value) for item in capabilities))
        ),
        source=source,
        path=path,
    )
