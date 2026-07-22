"""SubagentTypeSpec and SubagentTypeRegistry — typed subagent catalogue (PRD-124)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.subagents.pool import SubagentResult

__all__ = [
    "SubagentTypeSpec",
    "SubagentAggregator",
    "SubagentTypeRegistry",
    "DEFAULT_REGISTRY",
]

# ── type specifications ───────────────────────────────────────────────────────


class SubagentAggregator:
    """Base class for custom result aggregators (PRD-124 Phase 5).

    Override ``agent_type`` and ``aggregate()`` to produce a custom
    plain-text digest for a specific subagent type.  Register via
    ``registry.register_aggregator(MyAggregator())``.

    The default aggregator (used when no custom one is registered)
    produces the labelled-concatenation format defined in ``_aggregate()``
    in ``pool.py``.
    """

    #: Type name this aggregator handles (matches ``SubagentTypeSpec.name``).
    agent_type: str = ""

    def aggregate(self, results: list[SubagentResult]) -> str:
        """Convert a list of ``SubagentResult`` objects into a plain-text digest.

        :param results: ``SubagentResult`` instances for this type only.
        :return: Plain-text string delivered to the parent agent.
        """
        raise NotImplementedError


@dataclass
class SubagentTypeSpec:
    """Complete specification for one subagent type.

    Attributes
    ----------
    name:
        Unique type identifier used in ``spawn_subagents(tasks=[{"type": name, ...}])``.
    allowed_tools:
        Tool function ``__name__`` values this type may call.  The pool filters
        ``AGENT_TOOLS`` down to this set before building the subagent.
    max_turns:
        Maximum LLM sub-turns (tool-call → response cycles) per worker.
    system_prompt:
        Full system prompt injected as the ``@agent(system=...)`` argument.
        Must be a self-contained instruction — the task description is appended
        as the first user message, not here.
    max_turn_time_s:
        Wall-clock timeout per worker execution.  Default: 120 seconds.
    """

    name: str
    allowed_tools: frozenset[str]
    max_turns: int
    system_prompt: str
    max_turn_time_s: float = 120.0


# ── system prompts ────────────────────────────────────────────────────────────

_EXPLORER_PROMPT = (
    "You are a read-only codebase explorer. "
    "Your job is to investigate the repository and gather facts. "
    "You may read files, list directories, search, grep, and inspect git history. "
    "You must NOT write, edit, or delete any files. "
    "When you have gathered enough information, write a clear Markdown summary of your findings "
    "with specific file paths and line numbers where relevant. "
    "End your response with a concise one-paragraph synthesis."
)

_PLANNER_PROMPT = (
    "You are an implementation planner. "
    "Your job is to read the codebase and produce a clear, numbered implementation plan "
    "for the task you are given. "
    "You may read files and search the codebase. You must NOT modify any files. "
    "End your response with a numbered list of concrete implementation steps, "
    "each scoped to a specific file or function."
)

_IMPLEMENTER_PROMPT = (
    "You are a focused implementer. "
    "Your job is to carry out a specific, scoped code change. "
    "Read the relevant files, make the change, and verify it compiles or evaluates correctly. "
    "Stay strictly within the files mentioned in your task — do not refactor unrelated code. "
    "End your response with a brief summary of exactly what you changed and why."
)

_TESTER_PROMPT = (
    "You are a test engineer. "
    "Your job is to write or run tests for the component described in your task. "
    "Write clear, well-named tests, run them, and report the outcome. "
    "End your response with a summary of tests written, passed, and failed, "
    "and any failures with their error messages."
)

_REVIEWER_PROMPT = (
    "You are a code reviewer. "
    "Your job is to review the code or change described in your task for correctness, "
    "clarity, security issues, and potential bugs. "
    "Read the relevant files. Do not modify anything. "
    "End your response with a clear verdict — APPROVED or NEEDS CHANGES — "
    "followed by a bulleted list of issues found (or 'No issues found')."
)

_DOCUMENTER_PROMPT = (
    "You are a documentation writer. "
    "Your job is to write or update documentation for the module, function, or API "
    "described in your task. "
    "Read the source code to understand what to document, then write accurate docs. "
    "End your response with a brief summary of what you documented and which files changed."
)

_VERIFIER_PROMPT = (
    "You are an adversarial verifier. "
    "Your job is to check whether a specific requirement, invariant, or assertion holds "
    "in the codebase. Actively look for counter-evidence. "
    "Run tests if relevant. Read files and grep for patterns. "
    "End your response with a clear verdict — VERIFIED or NOT VERIFIED — "
    "followed by the evidence you found for and against."
)

_RESEARCHER_PROMPT = (
    "You are a technical researcher. "
    "Your job is to find answers to specific technical questions. "
    "Search local files and documentation first. "
    "End your response with a direct answer to the question and any relevant sources."
)


# ── default type definitions ──────────────────────────────────────────────────

_EXPLORER_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "search_files",
        "grep_files",
        "grep_file",
        "git_log",
        "git_show",
        "git_blame",
        "git_grep",
        "file_exists",
        "get_file_info",
        "read_lines",
    }
)

_PLANNER_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "search_files",
        "grep_files",
        "grep_file",
        "read_lines",
        "file_exists",
    }
)

_IMPLEMENTER_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "patch_file",
        "append_file",
        "list_directory",
        "search_files",
        "grep_files",
        "grep_file",
        "run_python_expr",
        "read_lines",
        "file_exists",
        "get_file_info",
    }
)

_TESTER_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "patch_file",
        "run_tests",
        "run_bash",
        "run_python_expr",
        "list_directory",
        "search_files",
        "grep_files",
        "read_lines",
    }
)

_REVIEWER_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "search_files",
        "grep_files",
        "grep_file",
        "git_diff",
        "git_log",
        "run_python_expr",
        "read_lines",
        "file_exists",
    }
)

_DOCUMENTER_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "patch_file",
        "search_files",
        "list_directory",
        "read_lines",
    }
)

_VERIFIER_TOOLS = frozenset(
    {
        "read_file",
        "search_files",
        "grep_files",
        "grep_file",
        "run_tests",
        "run_python_expr",
        "git_diff",
        "git_log",
        "read_lines",
        "file_exists",
    }
)

_RESEARCHER_TOOLS = frozenset(
    {
        "read_file",
        "read_lines",
        "search_files",
        "grep_files",
        "list_directory",
    }
)

_BUILTIN_SPECS: list[SubagentTypeSpec] = [
    SubagentTypeSpec(
        name="explorer",
        allowed_tools=_EXPLORER_TOOLS,
        max_turns=15,
        system_prompt=_EXPLORER_PROMPT,
    ),
    SubagentTypeSpec(
        name="planner",
        allowed_tools=_PLANNER_TOOLS,
        max_turns=10,
        system_prompt=_PLANNER_PROMPT,
    ),
    SubagentTypeSpec(
        name="implementer",
        allowed_tools=_IMPLEMENTER_TOOLS,
        max_turns=20,
        system_prompt=_IMPLEMENTER_PROMPT,
        max_turn_time_s=180.0,
    ),
    SubagentTypeSpec(
        name="tester",
        allowed_tools=_TESTER_TOOLS,
        max_turns=20,
        system_prompt=_TESTER_PROMPT,
        max_turn_time_s=180.0,
    ),
    SubagentTypeSpec(
        name="reviewer",
        allowed_tools=_REVIEWER_TOOLS,
        max_turns=10,
        system_prompt=_REVIEWER_PROMPT,
    ),
    SubagentTypeSpec(
        name="documenter",
        allowed_tools=_DOCUMENTER_TOOLS,
        max_turns=15,
        system_prompt=_DOCUMENTER_PROMPT,
        max_turn_time_s=150.0,
    ),
    SubagentTypeSpec(
        name="verifier",
        allowed_tools=_VERIFIER_TOOLS,
        max_turns=12,
        system_prompt=_VERIFIER_PROMPT,
        max_turn_time_s=150.0,
    ),
    SubagentTypeSpec(
        name="researcher",
        allowed_tools=_RESEARCHER_TOOLS,
        max_turns=8,
        system_prompt=_RESEARCHER_PROMPT,
    ),
]


# ── registry ──────────────────────────────────────────────────────────────────


class SubagentTypeRegistry:
    """Maps type name → SubagentTypeSpec and optionally → SubagentAggregator.

    Plugin authors register custom types and aggregators via:

    .. code-block:: python

        # In .agenthicc/tools/my_plugin.py
        from agenthicc.subagents import SubagentTypeSpec, SubagentAggregator, DEFAULT_REGISTRY

        DEFAULT_REGISTRY.register(SubagentTypeSpec(
            name="security_reviewer",
            allowed_tools=frozenset({"read_file", "grep_files"}),
            max_turns=8,
            system_prompt="You are a security-focused reviewer...",
        ))

        class SecurityAggregator(SubagentAggregator):
            agent_type = "security_reviewer"
            def aggregate(self, results):
                verdicts = ["APPROVED" if "APPROVED" in r.text else "NEEDS CHANGES"
                            for r in results if r.ok]
                return "\\n".join(verdicts)

        DEFAULT_REGISTRY.register_aggregator(SecurityAggregator())
    """

    def __init__(self) -> None:
        self._types: dict[str, SubagentTypeSpec] = {}
        self._aggregators: dict[str, SubagentAggregator] = {}

    def register(self, spec: SubagentTypeSpec) -> None:
        """Register or replace a subagent type."""
        self._types[spec.name] = spec

    def get(self, name: str) -> SubagentTypeSpec | None:
        """Return the spec for *name*, or ``None`` if not registered."""
        return self._types.get(name)

    def register_aggregator(self, aggregator: SubagentAggregator) -> None:
        """Register a custom aggregator for a subagent type.

        Only one aggregator per type is kept; calling this again replaces it.
        """
        self._aggregators[aggregator.agent_type] = aggregator

    def get_aggregator(self, name: str) -> SubagentAggregator | None:
        """Return the custom aggregator for *name*, or ``None`` for default."""
        return self._aggregators.get(name)

    def names(self) -> list[str]:
        """Return all registered type names."""
        return list(self._types.keys())

    def __contains__(self, name: object) -> bool:
        return name in self._types


def _build_default_registry() -> SubagentTypeRegistry:
    reg = SubagentTypeRegistry()
    for spec in _BUILTIN_SPECS:
        reg.register(spec)
    return reg


#: Process-level default registry pre-loaded with all 8 built-in types.
DEFAULT_REGISTRY: SubagentTypeRegistry = _build_default_registry()
