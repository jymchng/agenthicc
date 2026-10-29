---
title: "SUBAGENT-02: Per-type model, reasoning-effort and context routing"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "which model, reasoning effort and context budget a worker of a given type runs with, and how those values take precedence over the inherited execution policy"
area: A-2
related_prds:
  - PRD-197
  - PRD-233
  - PRD-124
  - PRD-157
  - AGX-09
tags:
  - subagents
  - model-routing
  - context-budget
  - cost
---

# SUBAGENT-02: Per-type model, reasoning-effort and context routing

## 1. Executive summary

Every worker runs on the parent's model, at the parent's reasoning effort, in a
fresh memory capped at a constant 8,000 tokens. The parent turn has none of
those limitations: it resolves a context window *per model*, computes a usable
input budget from it, and installs a pre-send guard. This PRD gives a subagent
type three optional settings — `model`, `effort`, `context` — defines the
single precedence chain for each, and makes the worker memory derive from the
model's real window instead of a magic number.

Inheritance stays the default: a type that sets nothing behaves exactly as it
does today. The PRD also repairs the attribution that per-type models make
necessary: the usage record currently labels every worker with the parent's
model and `provider="unknown"`, which is harmless only while the first claim in
the previous sentence is true.

## 2. Problem statement

All claims verified at commit
`4994e2a2cc1a97576e1260def27b4b01711ec987` (2026-10-29).

**The parent's model is the only model input a worker can have.** The turn
boundary passes its own model into the spawn tool
(`src/agenthicc/runners/agent_turn.py:1575`, `parent_model=self._model_id`), and
the pool decorates every worker with it
(`src/agenthicc/subagents/pool.py:849`,
`@agent_decorator(model=self._parent_model, system=system)`). There is no other
assignment to a worker's model anywhere: `parent_model` reaches the pool through
exactly three call sites (`tool.py:284`, `pool.py:1244`, `pool.py:1665`), all of
which forward the same value.

**The type record has nowhere to put a model.** `SubagentTypeSpec` is still
five fields (`src/agenthicc/subagents/types.py:87-91`), and the inherited
policy snapshot has thirteen (`src/agenthicc/subagents/policy.py:34-45`) —
`mode_name` through `policy_revision`, none of them a model, an effort, or a
context size.

**The parent has a per-model context budget and a pre-send guard; the worker has
neither.** The parent resolves `_window = _context_window_for(self._model_id)`
(`src/agenthicc/runners/agent_turn.py:2212`), derives a usable budget of
`window − max_output − max(4_000, window // 25)` (`:2213`, or
`effective_usable_budget()` at `:2208`), and installs a hard guard with
`context_window=_window` (`:2380`) or `max_input_tokens=_window_tokens`
(`:2568`). The worker gets `ShortTermMemory(max_tokens=8_000)`
(`src/agenthicc/subagents/pool.py:958`, `:960`) and no guard at all:
`grep -rn context_window src/agenthicc/subagents/` returns nothing. The 8,000 is
also a *reduction* against the library default — `ShortTermMemory.__init__` is
`(self, max_tokens: int = 40000)` — so a worker is given a fifth of what the
same model handles for the parent, on every model.

**Reasoning effort cannot reach a worker even when the user sets it.** The
parent builds a four-key option dict at the turn boundary
(`src/agenthicc/runners/agent_turn.py:1586-1591`: `temperature`, `top_p`,
`max_completion_tokens`, `request_options`), forwards it to the pool
(`pool.py:1676`), and the pool filters it to those same four keys
(`src/agenthicc/subagents/pool.py:966-969`). The parent's own path applies
provider options by dataclass-field detection (`agent_turn.py:2367-2374`), which
is how a `provider.reasoning_effort` — the exact example the shipped config
template advertises (`src/agenthicc/config_template.py:125`,
`provider = { reasoning_effort = "none" }`) — takes effect for the foreground
session. The worker path hard-codes the four keys, so the same setting is
silently dropped for subagents.

**Cost and usage are attributed to the wrong model the moment the first sentence
stops being true.** The pool opens a usage tracker with `provider="unknown"` and
`model=self._parent_model` (`src/agenthicc/subagents/pool.py:939-940`), and the
ledger's `_cost_for` refuses to invent a price, returning `UNAVAILABLE` unless
the caller supplies one (`src/agenthicc/runners/usage_ledger.py:615-623`). Today
those two facts cancel out. Introduce a per-type model and the record carries a
model that never ran the request, with no error to notice it.

**Consequence for users.** A pool cannot use a cheap model for triage and a
strong one for review; it cannot ask for more reasoning on the verifier than on
the documenter; and a worker doing real work on a 200k-token model is truncated
at 8,000 tokens with no warning, because the guard that would have warned lives
only on the parent's path.

## 3. Current-source investigation

Run on 2026-10-29 against `4994e2a2cc1a97576e1260def27b4b01711ec987`.

```console
$ git rev-parse HEAD
4994e2a2cc1a97576e1260def27b4b01711ec987

$ grep -rn "parent_model=" --include=*.py src
src/agenthicc/subagents/pool.py:1244:                parent_model=self._parent_model,
src/agenthicc/subagents/pool.py:1665:        parent_model=parent_model,
src/agenthicc/subagents/tool.py:284:            parent_model=parent_model,
src/agenthicc/runners/agent_turn.py:1575:                parent_model=self._model_id,

$ sed -n '849p;958,960p;966,969p' src/agenthicc/subagents/pool.py
        @agent_decorator(model=self._parent_model, system=system)
            memory = _ObservedMemory(max_tokens=8_000)
        else:
            memory = ShortTermMemory(max_tokens=8_000)
            "temperature": self._provider_options.get("temperature"),
            "top_p": self._provider_options.get("top_p"),
            "max_completion_tokens": self._provider_options.get("max_completion_tokens"),
            "request_options": self._provider_options.get("request_options"),

$ grep -rn "context_window" --include=*.py src/agenthicc/subagents/
(no output)

$ grep -n "reasoning_effort" --include=*.py -r src
src/agenthicc/config_template.py:125:    _comment(lines, 'provider = { reasoning_effort = "none" }')

$ python -c "import inspect; from lauren_ai import ShortTermMemory; \
print(inspect.signature(ShortTermMemory.__init__))"
(self, max_tokens: 'int' = 40000) -> 'None'
```

Absence claims, with the pattern and the scope that establish them:

- **No model, effort or context field on either record.** Patterns
  `^    model:|effort|context_window` over
  `src/agenthicc/subagents/types.py` and `src/agenthicc/subagents/policy.py`:
  no match. The five `SubagentTypeSpec` fields and the thirteen
  `SubagentExecutionPolicy` fields are enumerated at
  `types.py:87-91` and `policy.py:34-45` and contain none of the three.
- **No pre-send guard on the worker path.** Pattern `context_window`, scope
  `src/agenthicc/subagents/`: zero matches (quoted above). The guard exists only
  in `runners/agent_turn.py` (`:2380`, `:2568`).
- **No model catalog in `src`.** Pattern `model_catalog|ModelCatalog|list_models`,
  scope `src/`: zero matches. Nothing in agenthicc knows which models exist, so
  "a type names a model" is a string that can only be validated at the provider
  boundary — which is why §7 fails closed instead of claiming validation it
  cannot do.

The commit above is the one HEAD pointed at while these checks ran; this
document is committed immediately afterwards, and `git log -1 --name-only` on
that commit lists this file and nothing else, so every claim holds at both.

Diff against the research report's `f843040` snapshot: **nothing has moved** for
G-2. The report's `pool.py:849`, `:940`, `:958`, `:960` citations are still
exact at this commit.

One surprise, and it changes the rollout rather than the goals. PRD-197 — the
*shipped* policy-inheritance contract — does not merely omit per-type models; it
states model reuse as an intentional decision, with the rationale "Provider/model
identity is consistent" (`prds/prd-197-subagent-runtime-policy-inheritance.md:108`)
and a rule that a worker inherits "the resolved effective values" for
"Provider/model and safe request options" (`:512`). Its limitation table also
already records that only "a subset of execution configuration is inherited"
(`:109`), which is the provider-option allow-list in §2 restated by the
document that created it. So this PRD amends a shipped decision rather than
filling an oversight, and the only defensible default is the one that reproduces
it: inherit, unless a type says otherwise.

The same table explains a non-goal. PRD-197 rejects model control arriving as an
"arbitrary model-supplied `spawn_subagents` argument"
(`prds/prd-197-subagent-runtime-policy-inheritance.md:185`). Any per-call model
override in this PRD is therefore operator-gated, not model-chosen — see §4
goal 4 and §6 criterion 6.

## 4. Goals

1. A type definition may set `model:`, and that model is the one the worker
   actually runs on. Absent, or `model: inherit`, means the parent's model.
2. A type definition may set `effort:`, and that value reaches the worker's
   provider options instead of being filtered out. Absent means inherit.
3. A type definition may set `context:`, which sizes worker memory and installs
   the parent's pre-send guard; absent means today's 8,000-token behaviour,
   unchanged. A declared budget larger than the model's resolved window is
   clamped, not honoured. (Gap G-2)
4. One precedence chain per knob, written once and implemented once:
   definition value > operator-set session default > parent value. A per-call
   `model`/`effort` argument on `spawn_subagents` is **not** part of the chain
   unless the operator enables it explicitly, because PRD-197 forbids
   model-supplied policy arguments.
5. Provider options stay intersected with the inherited safe set. A type may
   narrow `temperature`/`top_p`/`max_completion_tokens`/`request_options` and may
   add an effort value; it may not widen or replace the session's resolved
   request options.
6. The usage record for a worker carries the model and provider that actually
   served it, so PRD-157's totals stay correct per model once goal 1 is true.
7. If worker model names become visible to the model — in the tool description
   or the enum, as SUBAGENT-01 makes types visible — that list is context and
   must be regenerated from the same source and invalidated on change, rather
   than typed into a docstring. (Research report §5.5 R-6.)
8. Every default reproduces today's behaviour exactly: no definition, no
   change.

## 5. Non-goals

The A-2 boundaries from the research report §5.3, restated as commitments:

- **Discovering which models exist.** The model catalog, its staleness notices
  as a *feature*, and provider capability metadata are out of scope; agenthicc
  has no such registry (§3). This PRD only defines what a type may say and when
  a value is refused.
- **Spend limits and the response to a breach.** Aggregate cost and per-pool
  budgets are SUBAGENT-06 (A-6); the abort-versus-drain response to a crossed
  limit is SUBAGENT-08 (A-8), per research report §5.4 M-1 — A-6 decides *when a
  budget is exceeded*, A-8 decides *what happens to the workers*. Context
  *sizing* is here; aggregate *cost* is there. All three read the same ledger and
  none owns another's policy.
- **Turn and tool-call budgets.** Already implemented as `max_turns`
  (`src/agenthicc/subagents/types.py:89`) and `max_turn_time_s` (`:91`); M-3 and
  R-8 in the research report reject them as an area, and this PRD does not
  redefine them.
- **Per-type permission narrowing.** What a type may *do* is SUBAGENT-05 (A-5).
- **Depth-tiered model escalation.** A worker's model may depend on its type
  here; making it depend on nesting depth is SUBAGENT-04 (A-4).
- **Provider profiles and endpoints.** `model:` may name a PRD-162 provider
  profile (AGX-09 §5.1, `prds/agx-09-agent-library-and-switching.md:80`); PRD-162
  owns what a profile is and how it connects.

## 6. Acceptance criteria

Each criterion names the check that decides it.

1. With no per-type settings, a worker is decorated with the parent model and
   built with `ShortTermMemory(max_tokens=8_000)`: a new case captures the model
   handed to `agent_decorator` and the memory budget. The existing cases in
   `tests/unit/test_subagent_pool_runtime_coverage.py` — which construct pools
   with `parent_model="model"` (`:39`, `:88`) but assert only usage
   normalisation — must stay green unedited, proving no behavioural drift.
2. A definition declaring `model: <id>` produces workers decorated with that id
   and usage records naming it: new cases in
   `tests/unit/test_subagent_pool.py`.
3. `model: inherit` and an omitted `model:` both resolve to the parent model:
   `tests/unit/test_subagent_pool.py`.
4. A definition declaring `effort:` yields that value in the worker's resolved
   provider options, and a definition declaring none yields the inherited
   value: `tests/unit/test_subagent_pool.py`.
5. A definition declaring `context: N` produces
   `ShortTermMemory(max_tokens=N)` when `N` fits the model's window, and the
   clamped window value when it does not: `tests/unit/test_subagent_pool.py`.
6. A per-call `model` argument on `spawn_subagents` is rejected unless the
   operator-enabled switch is on, and the rejection names PRD-197:
   `tests/unit/test_subagent_plugin.py`.
7. The usage record for a worker carries the effective model rather than
   `self._parent_model`: extended cases in
   `tests/integration/test_usage_ledger_integration.py`.
8. An unknown model or effort value is refused at definition-load time with the
   file path and the offending key, and the type is not registered:
   `tests/unit/test_subagent_plugin.py`.
9. `pytest tests/integration/test_subagent_tool_execution.py -q` and
   `pytest tests/integration/test_subagent_executor_integration.py -q` pass
   unchanged, proving the spawn boundary did not shift.

## 7. Rollout, migration and security

**Defaults.** Every new field defaults to the inherited value:

| Key | Absent means | Today's behaviour | Change for existing users |
|---|---|---|---|
| `model:` | parent model | parent model | none |
| `effort:` | parent's resolved provider options | filtered out (four keys only) | none — an unset effort still yields no effort key |
| `context:` | 8,000 tokens | 8,000 tokens | none |

**Migration of the shipped contract.** PRD-197's table is a record of behaviour,
so it is amended in the same change set: `:108` ("Parent model ID is reused")
gains the qualifier *unless the type declares `model:`*, and `:512` ("Inherit
the resolved effective values") becomes *inherit unless the type narrows*. The
rest of PRD-197 — mode, capability blocks, approvals, workspace scope, transcripts
— is untouched, and this PRD does not restate it.

**Definition-format coordination.** AGX-09 §5.1 fixes the frontmatter key set and
makes unknown keys a validation error by PRD-250's inert-field rule
(`prds/agx-09-agent-library-and-switching.md:92`). `model:` already exists there;
`effort:` and `context:` do not. They must be added to AGX-09's key list and its
schema in the same change, exactly as SUBAGENT-01 §9 requires for the format it
consumes — otherwise every definition using them fails validation as an unknown
key. The union of the keys this series adds, with the owning PRD and the default
for each, is recorded once in SUBAGENT-01 §7; this paragraph states what
SUBAGENT-02 adds and does not restate the other PRDs' keys.

**Security, and where it fails closed.**

- *Unknown model or effort → fail closed.* The definition is refused at load
  with the file path (§6 criterion 8). There is no model registry to validate
  against (§3), so the check happens where the value is first used, and a type
  that names a model the provider rejects spawns nothing rather than silently
  falling back to the parent's model — a silent fallback would let a definition
  claim `opus` and quietly run `haiku`, which is a correctness lie, not a
  degradation.
- *Unsupported effort for a known model → fail open, once.* Effort is a quality
  knob, not a permission, so an unsupported value warns with the type, the model
  and the value, then runs at the model's default. Refusing to spawn over a
  reasoning setting would be a worse failure than the setting itself.
- *Model allowlists are the operator's, not the definition's.* If the session
  restricts which models may be used, a per-type `model:` outside that set is
  refused with a visible reason. A definition is data that can arrive in a
  cloned repository; it may choose within the operator's set, never outside it.
- *No secrets move.* A model or profile name is an identifier, not a credential;
  nothing in this PRD adds a key, endpoint or token to a definition file or to a
  spawn result.

**Cost.** Per-worker model choice makes spend per type legible, which is the
point and also the risk: a type pointed at an expensive model multiplies. This
PRD stops at making the cost *attributable* (`§4` goal 6) and visible through the
existing ledger. Stopping a pool that has spent too much is SUBAGENT-08 (A-8),
acting on the limit SUBAGENT-06 (A-6) declares (research report §5.4 M-1).

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_plugin.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/integration/test_subagent_tool_execution.py -q
$ pytest tests/integration/test_subagent_executor_integration.py -q
$ pytest tests/integration/test_usage_ledger_integration.py -q
```

New cases belong in the first two files and in the ledger integration test,
because those already assert worker construction, definition validation and usage
records respectively.

## 9. Related and superseded PRDs

- **PRD-197 — supersedes for the model and provider-option inheritance rule
  only.** Its §5 rule "the parent model ID is reused"
  (`prds/prd-197-subagent-runtime-policy-inheritance.md:108`) and "inherit the
  resolved effective values" (`:512`) become conditional on the type not
  overriding. Everything else in PRD-197 — the policy snapshot, approvals,
  workspace scope, isolation, transcripts — is inherited unchanged and cited, not
  restated. It also constrains this PRD twice: its limitation table is the
  provider-option allow-list in §2, and its §:185 rejection of model-supplied
  policy arguments is why §4 goal 4 gates per-call overrides behind an operator
  switch.
- **PRD-233 — supersedes for the subagent type record, delegates the rest.**
  PRD-233 proposes profiles that "select a prompt, provider/model, tools, skill
  set, resource budgets"
  (`prds/prd-233-declarative-agent-profiles-and-policy.md:17`). For subagent
  types, SUBAGENT-01 and this PRD are the narrower statements; PRD-233 remains
  authoritative for primary-agent profiles, policy ceilings and approvals. This
  PRD does not create a second place to configure an agent's model.
- **PRD-124 — inherits.** The concurrent-pool architecture is unchanged; this
  PRD adds fields that flow through the existing `SubagentPool` construction path
  (`src/agenthicc/subagents/pool.py:1118-1130`), it does not restructure the
  pool.
- **PRD-157 — extends.** PRD-157 (Implemented) owns the usage ledger and the TUI
  token display. This PRD only fixes what a worker's record says about its model
  (§4 goal 6) so those totals are meaningful per type; it does not touch the
  ledger's schema, the `/usage` surface or the TUI.
- **AGX-09 — extends.** AGX-09 §5.1 owns the definition format and already
  provides a `model:` key
  (`prds/agx-09-agent-library-and-switching.md:80`). This PRD adds `effort:` and
  `context:` to that format and defines what all three do for a subagent; AGX-09
  keeps the editing UI, the Library tab and hot reload.

## A. Competitor evidence

| Harness | Evidence | Depth | What it does | Take or reject |
|---|---|---|---|---|
| opencode | `packages/schema/src/agent.ts:23-35` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | `model?: Model.Ref` on the same typed `Agent.Info` record that carries `description` and `mode` | Take: the model belongs on the type record, not in a side table |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Four-step precedence: per-invocation `model` argument → frontmatter `model` (`inherit` = parent) → `CLAUDE_CODE_SUBAGENT_MODEL` env var → main conversation model; extended thinking is inherited with no per-subagent setting | Take the chain and the explicit `inherit` value; reject the model-supplied per-invocation argument, which PRD-197 forbids — our gate is operator-side |
| Qoder CLI | `https://docs.qoder.com/cli/subagent` fetched 2026-10-29 | docs-only | `model` and `effort` are separate per-subagent frontmatter fields | Take: effort is not a variant of the model string, so it needs its own key |
| Codex CLI | `codex-rs/core/src/session/multi_agents.rs:61` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | `expose_model_overrides: config.expose_spawn_agent_model_overrides`, registered conditionally under V2; with `ModelCatalogInContext` a model catalog is injected into context and can be invalidated | Take the operator gate; take the staleness lesson as §4 goal 7 |
| Grok CLI | `src/utils/settings.ts:109-112` @ `fb97af83f06dca873281d60168430f06c8de6324` | code | A subagent record is `{name, model, instruction}` — model only, no effort, no context budget | Partially take: evidence that model is the minimum viable knob; reject the record as a whole, it has no description (SUBAGENT-01) and no budget |
| DeepSeek Harness | `.agents/notes/archived/architecture/2026-08-03-per-session-agent-presets.md` @ `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` | code + design notes | Model routing is deliberately kept out of seat composition: "`installAgentLlmTarget` is already the per-agent seam for provider, model, and reasoning effort" | Take the separation: model selection has one owner, and it is not the same object that composes tools and prompts |
