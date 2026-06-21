"""AgentPlugin — base types for agent definitions (PRD-87).

AgentDefinition wraps a @agent(...)-decorated class with registry metadata.
AgentPlugin is the ABC for user/project agent plugins discovered from
~/.agenthicc/agents/ and .agenthicc/agents/.

Capability convenience sets replace the old tool_access strings:
    READ_CAPS   — read, git_read, search
    WRITE_CAPS  — write, git_write, execute, network
"""
from __future__ import annotations

from dataclasses import dataclass

from agenthicc.tools.capabilities import ToolCapability

# ── Base system prompt ────────────────────────────────────────────────────────
# Injected before every agent's role-specific system prompt by
# AgentsRegistry.make_instance().  Ensures all agents share the same
# foundational operating contract regardless of role.
# Can be overridden per-project via cfg.execution.base_system_prompt.

BASE_SYSTEM_PROMPT = (
    "You are a capable AI assistant working inside the current project directory. "
    "You have access to filesystem, shell, and git tools. "
    "Use them directly to complete tasks — explore the codebase with tools to "
    "discover what you need. "
    "Never ask the user for information you can obtain with a tool. "
    "Give concise responses. Show command output when relevant. "
    "Never invent file contents — always read them first."
)


# ── Capability shorthands ──────────────────────────────────────────────────────

READ_CAPS: frozenset[ToolCapability] = frozenset({
    ToolCapability.READ,
    ToolCapability.GIT_READ,
    ToolCapability.SEARCH,
})

WRITE_CAPS: frozenset[ToolCapability] = frozenset({
    ToolCapability.WRITE,
    ToolCapability.GIT_WRITE,
    ToolCapability.EXECUTE,
    ToolCapability.NETWORK,
})

# ── Role → default allowed capabilities ──────────────────────────────────────
# Used when PhaseSpec.allowed_capabilities is None.
# None means "all capabilities the session mode permits" (mode ceiling applies).

ROLE_DEFAULT_ALLOWED: dict[str, frozenset[ToolCapability] | None] = {
    "planner":  READ_CAPS,
    "executor": None,         # full access within mode ceiling
    "reviewer": READ_CAPS,
    "explorer": READ_CAPS,
    "verifier": READ_CAPS,
    "human":    frozenset(),  # no tools — waits for user input
    "custom":   None,
    "auto":     None,
}


# ── AgentDefinition ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentDefinition:
    """Registry entry: maps a name to a @agent(...)-decorated class."""
    name:                 str
    agent_class:          type                           # @agent(model=None,system=…) class
    allowed_capabilities: frozenset[ToolCapability] | None = None
    source:               str                            = "builtin"


# ── AgentPlugin ───────────────────────────────────────────────────────────────

class AgentPlugin:
    """ABC for user/project agent plugins.

    Subclasses must:
      1. Set `name` to the registry key.
      2. Be decorated with @agent(system="…") from lauren_ai._agents.
      3. Optionally set `allowed_capabilities` and `replaces`.

    Example::

        from lauren_ai._agents import agent
        from agenthicc.agents.plugin import AgentPlugin, READ_CAPS

        @agent(system="You are a domain-specific planning agent…")
        class MyPlannerAgent(AgentPlugin):
            name                 = "my_planner"
            allowed_capabilities = READ_CAPS
            replaces             = "planner"   # shadow the builtin planner

    The file must export AGENTS = [MyPlannerAgent] or the class will be
    discovered by name scanning.
    """
    name:                 str                            = ""
    allowed_capabilities: frozenset[ToolCapability] | None = None
    replaces:             str | None                     = None
    source:               str                            = "user"
