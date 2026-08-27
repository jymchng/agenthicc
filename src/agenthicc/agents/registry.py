"""AgentsRegistry — discover, store, and instantiate named agent definitions (PRD-87)."""

from __future__ import annotations

import importlib.util
import inspect
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

from agenthicc.agents.plugin import AgentDefinition, AgentPlugin
from agenthicc.tools.base import ToolLike

log = logging.getLogger(__name__)


class AgentsRegistry:
    """Maps agent type names to AgentDefinition instances.

    Later registrations shadow earlier ones (project > user > builtin).
    """

    def __init__(self) -> None:
        self._defs: dict[str, AgentDefinition] = {}
        self._lazy: dict[str, Callable[[], AgentDefinition]] = {}

    def register(self, defn: AgentDefinition) -> None:
        existing = self._defs.get(defn.name)
        if existing is not None and existing.source != defn.source:
            log.debug(
                "Agent %r (%s) shadows existing %s definition",
                defn.name,
                defn.source,
                existing.source,
            )
        self._defs[defn.name] = defn
        self._lazy.pop(defn.name, None)

    def register_lazy(self, name: str, loader: Callable[[], AgentDefinition]) -> None:
        """Register an agent descriptor without importing its implementation."""
        if name and name not in self._defs:
            self._lazy[name] = loader

    def _materialize(self, name: str) -> AgentDefinition | None:
        definition = self._defs.get(name)
        if definition is not None:
            return definition
        loader = self._lazy.get(name)
        if loader is None:
            return None
        definition = loader()
        self.register(definition)
        return definition

    def get(self, name: str) -> AgentDefinition | None:
        return self._materialize(name)

    def all(self) -> list[AgentDefinition]:
        return [definition for name in self.names() if (definition := self.get(name))]

    def names(self) -> list[str]:
        """Return agent names without materializing lazy definitions."""
        return [*self._defs.keys(), *[name for name in self._lazy if name not in self._defs]]

    def replace_with(self, other: "AgentsRegistry") -> None:
        """Replace entries in place while preserving session-owned identity.

        The interactive session gives its workflow configuration a reference
        to one registry object.  Deferred project-agent discovery therefore
        swaps the contents rather than replacing that object, just like the
        workflow registry does.
        """
        if other is self:
            return
        self._defs = dict(other._defs)
        self._lazy = dict(other._lazy)

    def get_role_system_prompt(self, agent_type: str) -> str:
        """Return the role-specific system prompt for *agent_type*.

        Reads from the @agent(system=...) metadata on the registered class.
        Returns an empty string for unknown types (base prompt still applies).
        """
        from lauren_ai._agents import AGENT_META  # noqa: PLC0415

        defn = self.get(agent_type) or self.get("auto")
        if defn is None:
            return ""
        base_meta = getattr(defn.agent_class, AGENT_META, None)
        return getattr(base_meta, "system", "") or ""

    def make_instance(
        self,
        agent_type: str,
        filtered_tools: list[ToolLike],
        model_id: str,
        base_system_prompt: str = "",
    ) -> tuple[object, object]:
        """Create a per-turn (agent_class, instance) for AgentRunnerBase.run_stream().

        Reads the role-specific system prompt from the registered @agent(...) class
        and prepends base_system_prompt (the universal operating contract) to it.
        Creates a fresh class per turn so the shared base class is never mutated.

        base_system_prompt defaults to BASE_SYSTEM_PROMPT; callers may pass a
        custom value from cfg.execution.base_system_prompt when set.
        """
        from lauren_ai._agents import agent as agent_decorator, use_tools, AGENT_META  # noqa: PLC0415
        from agenthicc.agents.plugin import BASE_SYSTEM_PROMPT  # noqa: PLC0415

        defn = self.get(agent_type) or self.get("auto")
        if defn is None:
            from agenthicc.agents.builtin import AutoAgent  # noqa: PLC0415

            defn = AgentDefinition(name="auto", agent_class=AutoAgent)

        base_meta = getattr(defn.agent_class, AGENT_META, None)
        role_prompt = getattr(base_meta, "system", "") or ""
        effective_base = base_system_prompt or BASE_SYSTEM_PROMPT

        # Combine: base contract first, then role-specific instructions.
        if role_prompt:
            system = f"{effective_base}\n\n{role_prompt}"
        else:
            system = effective_base

        @agent_decorator(model=model_id, system=system)
        @use_tools(*filtered_tools)
        class _TurnAgent: ...  # type: ignore[type-var]  # lauren-ai dynamic class

        return _TurnAgent, _TurnAgent()


def build_agents_registry(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    *,
    load_external: bool = True,
) -> AgentsRegistry:
    """Build the agents registry: builtin → user-global → project-local.

    With ``load_external=False`` only built-in descriptors are registered.
    This is the progressive TUI form; user/project Python modules are loaded
    by the extension readiness phase.  The default remains eager for callers
    that use this public helper as a complete registry builder.
    """
    if project_dir is None:
        project_dir = Path(".agenthicc")
    if user_dir is None:
        user_dir = Path.home() / ".agenthicc"

    registry = AgentsRegistry()

    # 1. Builtins. The decorated lauren-ai classes are imported only when a
    # role prompt or a per-turn instance requests one of these descriptors.
    for name in ("planner", "executor", "reviewer", "explorer", "verifier", "human", "auto"):
        registry.register_lazy(name, _make_builtin_loader(name))

    if load_external:
        # 2. User-global
        _scan_agents_dir(user_dir / "agents", "user", registry)

        # 3. Project-local
        _scan_agents_dir(project_dir / "agents", "project", registry)

    return registry


def _make_builtin_loader(name: str) -> Callable[[], AgentDefinition]:
    """Return a zero-argument loader for one built-in role."""

    def load() -> AgentDefinition:
        from agenthicc.agents.builtin import BUILTIN_AGENT_DEFINITIONS  # noqa: PLC0415

        for definition in BUILTIN_AGENT_DEFINITIONS:
            if definition.name == name:
                return definition
        raise LookupError(f"unknown built-in agent {name!r}")

    return load


def _scan_agents_dir(directory: Path, source: str, registry: AgentsRegistry) -> None:
    if not directory.exists():
        return
    for path in sorted(directory.iterdir()):
        if path.name.startswith("_") or path.suffix != ".py":
            continue
        try:
            _load_agent_file(path, source, registry)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load agents from %s: %s", path, exc)


def _load_agent_file(path: Path, source: str, registry: AgentsRegistry) -> None:
    module_name = f"_agenthicc_agent_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Check for explicit AGENTS list
    agents_list = getattr(module, "AGENTS", None)
    if agents_list:
        for candidate in cast(list[object], agents_list):
            if isinstance(candidate, type) and issubclass(candidate, AgentPlugin):
                cls = candidate
                if cls.name:
                    _register_plugin_class(cls, source, registry, str(path))
        return

    # Fall back to scanning for AgentPlugin subclasses
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj is not AgentPlugin and issubclass(obj, AgentPlugin) and obj.name:
            _register_plugin_class(obj, source, registry, str(path))


def _register_plugin_class(
    cls: type[AgentPlugin],
    source: str,
    registry: AgentsRegistry,
    path: str,
) -> None:
    replaces = getattr(cls, "replaces", None)
    name = replaces or cls.name

    defn = AgentDefinition(
        name=name,
        agent_class=cls,
        allowed_capabilities=getattr(cls, "allowed_capabilities", None),
        source=source,
    )
    registry.register(defn)
    log.debug("Registered agent %r from %s (source=%s)", name, path, source)
