"""Builtin @agent(...)-decorated classes — canonical source of truth for system prompts.

model=None on all classes: the session model is injected via model_override
in AgentRunnerBase.run_stream() at instantiation time.

NOTE: no ``from __future__ import annotations`` — @agent() inspects class
attributes at decoration time.
"""

from lauren_ai._agents import agent

from agenthicc.agents.plugin import AgentDefinition, READ_CAPS


@agent(
    model=None,
    system=(
        "You are a careful planning agent. Produce a numbered step-by-step plan. "
        "Do NOT execute any tools that modify files or run commands. "
        "Wrap your final plan in <plan>...</plan> tags."
    ),
)
class PlannerAgent: ...


@agent(
    model=None,
    system=(
        "You are an execution agent. Follow the plan step by step. "
        "Use tools to implement each step. Report progress after each step."
    ),
)
class ExecutorAgent: ...


@agent(
    model=None,
    system=(
        "You are a code reviewer. Inspect the work done and identify issues. "
        "Be constructive. End your review with <review>approved</review> or "
        "<review>rejected: reason</review>."
    ),
)
class ReviewerAgent: ...


@agent(
    model=None,
    system=(
        "You are a research agent. Explore the codebase and environment to "
        "gather context. Do NOT make any changes. Report your findings clearly."
    ),
)
class ExplorerAgent: ...


@agent(
    model=None,
    system=(
        "You are a verification agent. Check that the implementation is correct "
        "and complete. End with <review>approved</review> or "
        "<review>rejected: reason</review>."
    ),
)
class VerifierAgent: ...


@agent(
    model=None,
    system="",  # HUMAN phase pauses for user input; no LLM invocation
)
class HumanAgent: ...


@agent(
    model=None,
    system=(
        "You are a capable AI assistant with access to filesystem, shell, "
        "and git tools. Use them directly to complete tasks. "
        "Give concise responses. Show command output when relevant. "
        "Never invent file contents — always read them first."
    ),
)
class AutoAgent: ...


# ── Registry entries ──────────────────────────────────────────────────────────

BUILTIN_AGENT_DEFINITIONS: list[AgentDefinition] = [
    AgentDefinition(
        name="planner",
        agent_class=PlannerAgent,
        allowed_capabilities=READ_CAPS,
    ),
    AgentDefinition(
        name="executor",
        agent_class=ExecutorAgent,
        allowed_capabilities=None,   # mode ceiling applies
    ),
    AgentDefinition(
        name="reviewer",
        agent_class=ReviewerAgent,
        allowed_capabilities=READ_CAPS,
    ),
    AgentDefinition(
        name="explorer",
        agent_class=ExplorerAgent,
        allowed_capabilities=READ_CAPS,
    ),
    AgentDefinition(
        name="verifier",
        agent_class=VerifierAgent,
        allowed_capabilities=READ_CAPS,
    ),
    AgentDefinition(
        name="human",
        agent_class=HumanAgent,
        allowed_capabilities=frozenset(),   # no tools
    ),
    AgentDefinition(
        name="auto",
        agent_class=AutoAgent,
        allowed_capabilities=None,
    ),
]
