"""Intent layer: Parse -> Validate -> Plan (PRD-02).

* :class:`IntentParser` — regex/heuristic goal extraction from raw text.
* :class:`IntentValidator` — capacity check against
  ``settings.max_concurrent_intents``.
* :class:`IntentPlanner` protocol with two implementations:
  :class:`StaticPlanner` (parses JSON task arrays) and :class:`LlmPlanner`
  (delegates to an injected lauren-ai ``AgentRunnerBase``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from agenthicc.kernel import AppState, IntentStatus

__all__ = [
    "IntentParser",
    "IntentPlanner",
    "IntentValidator",
    "LlmPlanner",
    "NodeSpec",
    "ParsedIntent",
    "StaticPlanner",
    "ValidationResult",
]


# ── Parse ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedIntent:
    """Structured representation of a user goal after heuristic parsing."""

    goal: str
    raw_text: str
    entities: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0


_PREFIX_RE = re.compile(
    r"^(?:please|kindly|hey|hi|could you|can you|would you|"
    r"i want to|i need to|i'd like to|i would like to)[,\s]+",
    re.IGNORECASE,
)
_ACTION_VERBS = (
    "add", "audit", "build", "create", "delete", "deploy", "document", "fix",
    "implement", "migrate", "optimize", "patch", "refactor", "remove",
    "rename", "test", "update", "upgrade", "write",
)
_FILE_RE = re.compile(
    r"\b[\w\-./]+\.(?:py|md|rst|txt|json|toml|cfg|ini|yaml|yml|js|ts|sh|sql|html|css)\b"
)
_QUOTED_RE = re.compile(r"[\"'`]([^\"'`]+)[\"'`]")
_DEADLINE_RE = re.compile(r"\bwithin\s+(\d+)\s*(seconds?|minutes?|hours?|days?)\b", re.IGNORECASE)


class IntentParser:
    """Heuristic (regex-based) extraction of a goal from raw request text."""

    def parse(self, raw_text: str) -> ParsedIntent:
        text = raw_text.strip()
        if not text:
            return ParsedIntent(goal="", raw_text=raw_text, confidence=0.0)

        goal = text
        while True:
            stripped = _PREFIX_RE.sub("", goal).strip()
            if stripped == goal:
                break
            goal = stripped
        goal = goal.rstrip(".!?").strip()

        entities: dict[str, Any] = {}
        files = _FILE_RE.findall(goal)
        if files:
            entities["files"] = files
        quoted = _QUOTED_RE.findall(goal)
        if quoted:
            entities["quoted"] = quoted

        constraints: dict[str, Any] = {}
        deadline = _DEADLINE_RE.search(goal)
        if deadline:
            constraints["deadline"] = f"{deadline.group(1)} {deadline.group(2)}"
        if re.search(r"\b(urgent|asap|immediately)\b", goal, re.IGNORECASE):
            constraints["priority"] = "high"

        first_word = goal.split()[0].lower() if goal.split() else ""
        confidence = 0.9 if first_word in _ACTION_VERBS else 0.5

        return ParsedIntent(
            goal=goal,
            raw_text=raw_text,
            entities=entities,
            constraints=constraints,
            confidence=confidence,
        )


# ── Validate ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None


_ACTIVE_STATUSES = frozenset(
    {
        IntentStatus.pending,
        IntentStatus.validating,
        IntentStatus.planning,
        IntentStatus.running,
    }
)


class IntentValidator:
    """Validates a parsed intent against current system state."""

    def validate(self, parsed: ParsedIntent, state: AppState) -> ValidationResult:
        if not parsed.goal.strip():
            return ValidationResult(ok=False, reason="empty goal")

        limit = state.settings.max_concurrent_intents
        active = sum(
            1 for intent in state.intents.values() if intent.status in _ACTIVE_STATUSES
        )
        if active >= limit:
            return ValidationResult(
                ok=False,
                reason=(
                    f"capacity exceeded: {active} active intents >= "
                    f"max_concurrent_intents={limit}"
                ),
            )
        return ValidationResult(ok=True)


# ── Plan ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NodeSpec:
    """Planner output: the specification for one workflow node."""

    node_id: str
    label: str
    dependencies: tuple[str, ...] = ()


def _coerce_spec(item: Any, index: int) -> NodeSpec | None:
    """Map one JSON task entry to a NodeSpec; None if uninterpretable."""
    if isinstance(item, str):
        return NodeSpec(node_id=f"node-{index}", label=item)
    if not isinstance(item, dict):
        return None
    node_id = str(item.get("id") or item.get("node_id") or f"node-{index}")
    label = str(item.get("label") or item.get("name") or item.get("task") or node_id)
    deps_raw = item.get("dependencies") or item.get("deps") or item.get("depends_on") or []
    if not isinstance(deps_raw, list):
        deps_raw = [deps_raw]
    return NodeSpec(node_id=node_id, label=label, dependencies=tuple(str(d) for d in deps_raw))


def parse_task_json(text: str) -> list[NodeSpec]:
    """Extract a JSON task array from ``text`` and convert it to NodeSpecs.

    Returns ``[]`` when no parseable JSON array of tasks is found.
    """
    candidates: list[Any] = []
    try:
        candidates.append(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\[.*\]", text or "", re.DOTALL)
        if match:
            try:
                candidates.append(json.loads(match.group(0)))
            except json.JSONDecodeError:
                pass

    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("tasks") or candidate.get("nodes")
        if not isinstance(candidate, list):
            continue
        specs = [
            spec
            for i, item in enumerate(candidate)
            if (spec := _coerce_spec(item, i)) is not None
        ]
        if specs:
            return specs
    return []


def _fallback_plan(parsed: ParsedIntent) -> list[NodeSpec]:
    """Single-node plan used when structured decomposition is unavailable."""
    return [
        NodeSpec(
            node_id=f"node-{uuid4().hex[:8]}",
            label=parsed.goal or parsed.raw_text or "task",
        )
    ]


@runtime_checkable
class IntentPlanner(Protocol):
    """Anything that can turn a ParsedIntent into a list of node specs."""

    async def plan(self, parsed: ParsedIntent) -> list[NodeSpec]: ...


class StaticPlanner:
    """Plans by parsing a JSON task array embedded in the request text.

    Falls back to a single-node plan when no task array is found.
    """

    async def plan(self, parsed: ParsedIntent) -> list[NodeSpec]:
        specs = parse_task_json(parsed.raw_text)
        return specs or _fallback_plan(parsed)


_DEFAULT_PLAN_PROMPT = (
    "Decompose the following goal into a JSON array of tasks. Respond with "
    'only the JSON array, e.g. [{{"id": "a", "label": "...", "dependencies": []}}].\n'
    "Goal: {goal}"
)


class LlmPlanner:
    """Plans by asking a lauren-ai agent (via ``AgentRunnerBase``) to decompose
    the goal into a JSON task array.

    The runner and the ``@agent()``-decorated instance are injected through
    the constructor; this class never builds transports or agents itself.
    Falls back to a single-node plan when the response is unparseable or the
    run fails.
    """

    def __init__(
        self,
        runner: Any,
        agent: Any,
        *,
        prompt_template: str = _DEFAULT_PLAN_PROMPT,
    ) -> None:
        self._runner = runner
        self._agent = agent
        self._prompt_template = prompt_template

    async def plan(self, parsed: ParsedIntent) -> list[NodeSpec]:
        prompt = self._prompt_template.format(goal=parsed.goal or parsed.raw_text)
        try:
            response = await self._runner.run(self._agent, prompt)
        except Exception:
            return _fallback_plan(parsed)
        content = getattr(response, "content", "") or ""
        specs = parse_task_json(content)
        return specs or _fallback_plan(parsed)
