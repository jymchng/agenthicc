# PRD-122 — Lauren-AI Integration Gaps

Reference document. Each entry is a candidate implementation PRD.
Study conducted against lauren-ai `src/lauren_ai/` and agenthicc
`src/agenthicc/` — citations are file:line.

---

## Gap 1 — Prompt caching (Anthropic)

**Lauren-ai:** `LLMConfig.cache_system_prompt` and `cache_tools` (`_config.py:88-89`).
When `True`, the Anthropic transport attaches `cache_control` headers to the
system prompt and last tool definition, cutting input-token cost by ~90% on
repeated turns.

**Agenthicc today:** Neither field is set when constructing `LLMConfig` in
`tui_session.py`. Every code-plan turn pays full input-token cost.

**Impact:** Code-plan phases have a large, stable system prompt and a large,
fixed tool list — exactly the case prompt caching was designed for.

---

## Gap 2 — Extended thinking / reasoning effort

**Lauren-ai:** `AgentConfig.thinking`, `thinking_budget_tokens`,
`reasoning_effort`, `include_reasoning_in_response` (`_config.py:305-312`).
The runner passes these to the transport on every call.

**Agenthicc today:** `agent_turn.py:437-439` builds `AgentConfig(max_turns=...,
parallel_tool_calls=True)` — all other fields default. Thinking is always off.

**Impact:** Cannot use Claude extended thinking or OpenAI o3 reasoning effort
for complex planning phases.

---

## Gap 3 — Per-agent USD budget cap

**Lauren-ai:** `AgentConfig.max_cost_usd` (`_config.py:301`). Runner raises
`AgentBudgetExceededError` when the cumulative cost of a run exceeds this value.

**Agenthicc today:** No cost cap ever set on `AgentConfig`. `ExecutionSettings`
has no `max_cost_usd` field. Runaway agents have no spend limit.

**Impact:** A stuck tool loop can generate unbounded charges with no automatic
stop.

---

## Gap 4 — Built-in context-window summarisation

**Lauren-ai:** `AgentConfig.summarize_at` + `summary_model` (`_config.py:
310-312`). When token usage hits the fraction threshold the runner summarises
old turns with a cheap model and prepends the summary to the system prompt.

**Agenthicc today:** Maintains its own `compact_memory` / `should_compact`
in `memory/compactor.py` (PRD-119) — a separate reimplementation.

**Impact:** Duplicate code that could be deleted. Set
`summarize_at=0.8, summary_model="claude-haiku-4-5"` in `AgentConfig` instead.

---

## Gap 5 — Guardrails (input/output filters)

**Lauren-ai:** `@use_guardrails(TopicFilter, PIIRedactor,
PromptInjectionFilter, LengthFilter, LLMGuardrail)` on any `@agent` class.
Runner executes them before and after every LLM call (`_runner.py:477-499`,
`609-635`).

**Agenthicc today:** No guardrail decorators on any agent class constructed in
`agent_turn.py:374-376`. No PII scrubbing, no prompt-injection detection.

**Impact:** An autonomous coding agent is exposed to prompt injection via
malicious file content it reads during execution.

---

## Gap 6 — Agent teams (`@team` + `TeamRunner`)

**Lauren-ai:** `@team(workers=[WorkerA, WorkerB])` + `TeamRunner.run(task)`
orchestrates a coordinator model that routes tasks to specialist agents with
shared `TeamMemory` (`_teams/_runner.py`).

**Agenthicc today:** Multi-agent coordination built by hand in
`code_plan/runner.py` via sequential phase loops.

**Impact:** `TeamRunner` would let agenthicc define coordinator → planner →
coder → reviewer pipelines declaratively.

---

## Gap 7 — Subagent tool + `SubagentPool`

**Lauren-ai:** `SubagentTool(subagent_cls=..., return_type=MyModel)` produces
a `@tool()` that spawns a child agent with isolated memory and Pydantic-typed
output. `SubagentPool` runs N tasks concurrently (`_subagent/__init__.py:
145-240`).

**Agenthicc today:** Phases run sequentially via `_run_agent_turn`. No typed
subagent output, no concurrent subagent pool.

**Impact:** Parallel specialist subagents (lint-fixer, test-writer, doc-writer)
would speed up complex plans significantly.

---

## Gap 8 — `AgentMessageBus` for inter-agent messaging

**Lauren-ai:** Full pub/sub bus with point-to-point, topic fan-out,
request/response with retry, and dead-letter queue (`_messaging/__init__.py`).

**Agenthicc today:** DI slot exists in `comm_tools.py:86` but every call site
passes `None`. The slot is wired but entirely unused.

**Impact:** Structured inter-phase result passing currently done by hand via
`CodePlanContext` fields. A message bus would decouple phases cleanly.

---

## Gap 9 — Semantic router (`SemanticRouter`)

**Lauren-ai:** `SemanticRouter.compile()` builds embedding centroids for named
`Route` objects; `router.route(query)` returns the best match via cosine
similarity (`_routing/_router.py`).

**Agenthicc today:** Skill/workflow selection uses keyword regex in
`skills/runner.py`. No embedding-based routing.

**Impact:** Embedding-based routing would dispatch user intents to the right
workflow more robustly than keyword matching.

---

## Gap 10 — Structured output + `RetryOutputParser`

**Lauren-ai:** `llm.with_structured_output(MyModel)` forces JSON-schema-valid
Pydantic output via tool-use. `RetryOutputParser` wraps any parser with N
retries on `OutputParserError` (`_transport/_structured.py`, `_output_parsers/`).

**Agenthicc today:** Plan parsing uses raw `json.loads` + ad-hoc validation.
No retry wrapper.

**Impact:** Malformed plan JSON propagates as untyped exceptions. A
`RetryOutputParser(PydanticOutputParser(PlanSchema), max_retries=2)` would
self-heal.

---

## Gap 11 — Tracing (`@traced` + `FileTraceExporter`)

**Lauren-ai:** `@traced(name="...", kind=SpanKind.AGENT)` records `Span`
objects with start/end timestamps into a `TraceStore`; `FileTraceExporter`
writes JSONL traces to disk (`_tracing/`).

**Agenthicc today:** No `@traced` decoration on any runner or workflow method.
The kernel event log provides partial observability but no span-level timing.

**Impact:** Cannot diagnose latency hot-spots across a multi-phase run without
manual log parsing.

---

## Gap 12 — `CostTracker` / `CostReport`

**Lauren-ai:** `CostTracker` accumulates `CostSession` objects; `CostReport.
summary()` gives per-model and aggregate USD cost (`_cost/_tracker.py`).

**Agenthicc today:** `conv_store.add_tokens()` accumulates per-turn totals.
No per-model breakdown, no `CostSession` concept, no aggregate report.

**Impact:** Users cannot see a session cost summary broken down by model or
workflow phase.

---

## Gap 13 — Client-side `RateLimiter`

**Lauren-ai:** `RateLimiter(requests_per_minute=60, tokens_per_minute=100_000)`
raises `RateLimitExhaustedError` before hitting the API (`_cost/_rate.py`).

**Agenthicc today:** No client-side rate limiter. Relies entirely on
provider-returned 429s. `parallel_tool_calls=True` is set in `agent_turn.py:
438`, making burst requests possible.

**Impact:** Parallel tool calls can burst-fire requests simultaneously; no
pre-emptive throttle.

---

## Gap 14 — `SQLiteConversationStore` + `SQLiteUserMemoryStore`

**Lauren-ai:** `SQLiteConversationStore` persists full `ShortTermMemory.
snapshot()` per `conversation_id`. `SQLiteUserMemoryStore` persists
`MemoryFact` objects with confidence scoring and topic tagging
(`_memory/_sqlite.py`).

**Agenthicc today:** Maintains its own bespoke SQLite conversation store.
`UserMemoryStore` / `MemoryFact` entirely unused.

**Impact:** Cross-session user preferences (coding style, preferred patterns)
are never persisted with confidence/decay.

---

## Gap 15 — Multimodal content (`ImageContent`, `AudioContent`, `DocumentContent`)

**Lauren-ai:** `ImageContent(url=..., detail="high")`, `AudioContent`,
`DocumentContent` integrate into `Message` content parts (`_transport/
_multimodal.py`).

**Agenthicc today:** All messages are text-only. No image or document
attachment possible in the TUI input bar.

**Impact:** Users cannot paste screenshots or attach PDFs as coding context,
even though Claude supports it natively.

---

## Gap 16 — `@use_knowledge_sources` / `KnowledgeBase` RAG

**Lauren-ai:** `@use_knowledge_sources(kb)` attaches a `KnowledgeBase` as a
retrieval tool. `KnowledgeBase.load(TextLoader(...))` chunks and embeds
documents; `kb.as_tool()` exposes hybrid keyword + semantic search
(`_knowledge/__init__.py`).

**Agenthicc today:** `SemanticIndex` in `memory/vector.py` indexes turn text
only. No document loader, no chunker, no `@use_knowledge_sources` integration.

**Impact:** Cannot attach project docs or OpenAPI specs as a searchable
knowledge base for the coding agent.

---

## Gap 17 — `FewShotPromptTemplate` / `PromptTemplate`

**Lauren-ai:** `FewShotPromptTemplate(examples=[...], input_variables=[...])`
generates few-shot prompts with typed variable substitution. `PromptTemplate`
and `ChatPromptTemplate` for simpler cases (`_prompts/`).

**Agenthicc today:** System prompts built by bare string concatenation in
`agent_turn.py:363-373`. No template abstraction, no typed variable
substitution.

**Impact:** As the number of optional prompt sections grows (skills, registry,
memory), unstructured concatenation becomes fragile and hard to test.

---

## Gap 18 — `Chain` / `Runnable` pipeline composition

**Lauren-ai:** `Chain([prompt | llm | parser])` composes `Runnable` objects
into a pipeline with `@chain` decorator shorthand (`_chains/`).

**Agenthicc today:** Multi-step prompting is imperative async coroutine calls
inside phase runners. No composable pipeline abstraction.

**Impact:** Phase logic cannot be tested in isolation or reused across
different workflow types.

---

## Gap 19 — Eval framework (`TrajectoryEval`, `AccuracyEval`)

**Lauren-ai:** `AccuracyEval`, `TrajectoryEval`, `PerformanceEval` run batched
agent evaluation with pass-rate assertions; `EvalReport.assert_pass_rate(0.9)`
integrates with CI (`_eval/__init__.py`).

**Agenthicc today:** No eval harness. E2E tests assert infrastructure, not
agent output quality or tool-call ordering.

**Impact:** Tool-call ordering regressions go undetected between releases.

---

## Gap 20 — `tool_error_policy` configuration

**Lauren-ai:** `AgentConfig.tool_error_policy` accepts `"raise"`,
`"return_error"`, or `"skip"` (`_config.py:303`). Runner branches on this
in `_execute_single_tool` (`_runner.py:1569-1588`).

**Agenthicc today:** `AgentConfig` constructed with only `max_turns` and
`parallel_tool_calls` at `agent_turn.py:437-439`. Policy defaults to
`"return_error"` and is never surfaced in `ExecutionSettings` or TOML.

**Impact:** Cannot set `"raise"` in CI to fail fast or `"skip"` in batch runs
to continue past broken tools without modifying source code.
