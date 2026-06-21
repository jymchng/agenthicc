"""AgentsRegistry — discover, store, and instantiate named agent definitions (PRD-87)."""
from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path

from agenthicc.agents.plugin import AgentDefinition, AgentPlugin

log = logging.getLogger(__name__)


class AgentsRegistry:
    """Maps agent type names to AgentDefinition instances.

    Later registrations shadow earlier ones (project > user > builtin).
    """

    def __init__(self) -> None:
        self._defs: dict[str, AgentDefinition] = {}

    def register(self, defn: AgentDefinition) -> None:
        existing = self._defs.get(defn.name)
        if existing is not None and existing.source != defn.source:
            log.debug(
                "Agent %r (%s) shadows existing %s definition",
                defn.name, defn.source, existing.source,
            )
        self._defs[defn.name] = defn

    def get(self, name: str) -> AgentDefinition | None:
        return self._defs.get(name)

    def all(self) -> list[AgentDefinition]:
        return list(self._defs.values())

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
        filtered_tools: list,
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
        from agenthicc.agents.plugin import BASE_SYSTEM_PROMPT                         # noqa: PLC0415

        defn = self.get(agent_type) or self.get("auto")
        if defn is None:
            from agenthicc.agents.builtin import AutoAgent  # noqa: PLC0415
            defn = AgentDefinition(name="auto", agent_class=AutoAgent)

        base_meta     = getattr(defn.agent_class, AGENT_META, None)
        role_prompt   = getattr(base_meta, "system", "") or ""
        effective_base = base_system_prompt or BASE_SYSTEM_PROMPT

        # Combine: base contract first, then role-specific instructions.
        if role_prompt:
            system = f"{effective_base}\n\n{role_prompt}"
        else:
            system = effective_base

        @agent_decorator(model=model_id, system=system)
        @use_tools(*filtered_tools)
        class _TurnAgent: ...

        return _TurnAgent, _TurnAgent()


def build_agents_registry(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> AgentsRegistry:
    """Build the agents registry: builtin → user-global → project-local."""
    from agenthicc.agents.builtin import BUILTIN_AGENT_DEFINITIONS  # noqa: PLC0415

    if project_dir is None:
        project_dir = Path(".agenthicc")
    if user_dir is None:
        user_dir = Path.home() / ".agenthicc"

    registry = AgentsRegistry()

    # 1. Builtins
    for defn in BUILTIN_AGENT_DEFINITIONS:
        registry.register(defn)

    # 2. User-global
    _scan_agents_dir(user_dir / "agents", "user", registry)

    # 3. Project-local
    _scan_agents_dir(project_dir / "agents", "project", registry)

    return registry


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
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    # Check for explicit AGENTS list
    agents_list = getattr(module, "AGENTS", None)
    if agents_list:
        for cls in agents_list:
            if isinstance(cls, type) and issubclass(cls, AgentPlugin) and cls.name:
                _register_plugin_class(cls, source, registry, str(path))
        return

    # Fall back to scanning for AgentPlugin subclasses
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if (
            obj is not AgentPlugin
            and issubclass(obj, AgentPlugin)
            and getattr(obj, "name", "")
        ):
            _register_plugin_class(obj, source, registry, str(path))


def _register_plugin_class(
    cls: type,
    source: str,
    registry: AgentsRegistry,
    path: str,
) -> None:
    replaces = getattr(cls, "replaces", None)
    name     = replaces or cls.name

    defn = AgentDefinition(
        name=name,
        agent_class=cls,
        allowed_capabilities=getattr(cls, "allowed_capabilities", None),
        source=source,
    )
    registry.register(defn)
    log.debug("Registered agent %r from %s (source=%s)", name, path, source)
