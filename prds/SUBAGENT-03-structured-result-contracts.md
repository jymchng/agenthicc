---
title: "SUBAGENT-03: Structured result contracts and schema-validated findings"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "the optional per-type contract a worker's result value must satisfy, how that value is delivered and validated, and what failure means when it does not conform"
area: A-3
related_prds:
  - PRD-124
  - PRD-214
  - PRD-180
  - AGX-09
tags:
  - subagents
  - result-contract
  - schema-validation
  - handoff
---

# SUBAGENT-03: Structured result contracts and schema-validated findings

## 1. Executive summary

A worker's result is whatever prose it ended with — and if it ended with
nothing, the runtime substitutes an inventory of the tools it called and marks
that success. There is no output contract to violate, so nothing can fail for
the right reason.

This PRD adds an **optional per-type value contract**. Where a type declares
one, the worker must deliver its value through a submission tool whose input
schema *is* that contract, the runtime validates the submitted value, and a pure
renderer turns it into the text the parent reads. The model never fills a free-
text schema; it fills tool arguments, which is the one schema-shaped channel
models already satisfy. Types that declare nothing behave exactly as today.

It does not create a second deliverable mechanism. PRD-214 already owns *where*
a result goes — inline response or file-backed Markdown — and *when a task
counts as complete*; this PRD owns only *what a conforming value is*. §9 states
the boundary so that exactly one validator and exactly one `deliverable` field
exist.

## 2. Problem statement

All claims verified at commit `ffbfa0da681fa76a52a8e49e5f9b5776cbb8f780`
(2026-10-29); line-level evidence is re-quoted in §3.

**Text is the only channel.** `SubagentResult` carries `text: str`, commented
"AgentResponse.content or a tool-evidence fallback (plain text)"
(`src/agenthicc/subagents/pool.py:317-329`, the field at `:325`).
`AggregatedResult.text` is the "labelled concatenation delivered to parent as
tool result" (`:353-360`), and the aggregator API returns `str`
(`src/agenthicc/subagents/types.py:51`). The parent receives that string as the
tool result verbatim (`src/agenthicc/subagents/tool.py:370`,
`"results": result.text`).

**The result format is an f-string, not a contract.** `_aggregate` builds
`=== {label} ({status}) ===` sections, a `✓ 1.2s` or `✗ <error>` status, an
optional `[failed: …]` prefix, blank-line joins, and a
`=== {type} (custom aggregator) ===` variant for custom aggregators
(`src/agenthicc/subagents/pool.py:1588-1628`). It is a presentation
convention embedded in a function: unversioned, undocumented, and unusable by
anything that wants to reason about one worker's finding rather than read a
digest.

**Success can mean "it called some tools".** When the final completion is
empty, the worker substitutes `_tool_call_summary(tool_calls, changed_paths)`
(`src/agenthicc/subagents/pool.py:722-723`, defined at `:418`), and if that is
non-empty the worker returns `ok=True` (`:736-746`). A worker that inspected
files and then said nothing is reported to the parent as succeeded, with an
inventory of tool names as its "result". The pool then counts it in
`succeeded` and emits a completion event.

**One contract already exists, hand-coded.** `implementer` results are rejected
when no successful mutating tool call occurred, with a specific error
(`src/agenthicc/subagents/pool.py:706-719`). That is exactly the mechanism this
PRD generalises — a per-type rule about what a conforming result is — but it is
a branch on a hard-coded name, invisible in the type record, and unavailable to
any other type.

**Nothing validates anything.** The only JSON in the pool is a *dumping* used
to hash message contents (`src/agenthicc/subagents/pool.py:1023`); the only
`json.loads` calls in the package read the transcript format back in
(`src/agenthicc/subagents/transcripts.py:306`, `:417`). Neither looks at a
worker's result. `SubagentTask` has four fields — `task_id`, `agent_type`,
`task_description`, `context` (`:307-315`) — with no output contract among them.
The research report records the same absence for the type record: no
`output_schema` among its fields (`prds/SUBAGENT-research-harness-comparison.md:109`).

**Why it is like this: the PRD-124 amendment, which was right about its own
problem.** "Typed result schemas removed. LLM output cannot reliably follow a
Pydantic schema. All subagents return plain text. Aggregation concatenates
labelled text blocks. No `result_type`, no `TypedResult`, no output parser."
(`prds/prd-124-concurrent-typed-subagents.md:6-9`). That is correct about
*parsing prose*, and this PRD does not parse prose. See §4 goal 2.

**Consequence for users.** A parent cannot ask "which findings are high
severity?" — it can only re-read the digest and hope the worker formatted things
consistently. A pool cannot fail a worker for returning a summary instead of
findings, because there is nothing to compare against. And the one role that
does have a contract has it because someone hand-wrote a branch for it.

## 3. Current-source investigation

Run on 2026-10-29 against `ffbfa0da681fa76a52a8e49e5f9b5776cbb8f780`.

```console
$ git rev-parse HEAD
ffbfa0da681fa76a52a8e49e5f9b5776cbb8f780

$ sed -n '317,329p' src/agenthicc/subagents/pool.py
@dataclass
class SubagentResult:
    """Outcome of one subagent worker execution."""

    task_id: str
    agent_type: str
    label: str  # "explorer #1", "tester #2", …
    ok: bool
    text: str  # AgentResponse.content or a tool-evidence fallback (plain text)
    error: str = ""
    duration_ms: float = 0.0
    tool_calls: tuple[str, ...] = ()  # Provider tools actually dispatched
    changed_paths: tuple[str, ...] = ()  # Paths supplied to mutation tools

$ sed -n '722,723p' src/agenthicc/subagents/pool.py
            if not text.strip():
                text = _tool_call_summary(tool_calls, changed_paths)

$ grep -n "json.dumps" src/agenthicc/subagents/pool.py
1023:                        + json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)

$ grep -rn "json.loads" src/agenthicc/subagents/
src/agenthicc/subagents/transcripts.py:306:        value = json.loads(line)
src/agenthicc/subagents/transcripts.py:417:            manifest = json.loads((directory / "pool.json").read_text(encoding="utf-8"))

$ grep -n "def aggregate" src/agenthicc/subagents/types.py
51:    def aggregate(self, results: list[SubagentResult]) -> str:

$ sed -n '307,315p' src/agenthicc/subagents/pool.py
@dataclass
class SubagentTask:
    """One unit of work assigned to a subagent worker."""

    task_id: str
    agent_type: str
    task_description: str
    context: str = ""
```

Absence claims, with the pattern and the scope that establish them:

- **No output-contract field anywhere in the subagent package.** Patterns
  `output_schema|result_type|deliverable|TypedResult`, scope
  `src/agenthicc/subagents/`: no match. The two record types that could hold one
  are enumerated above and in `types.py:60-91`.
- **No parser and no validator of worker results.** Pattern `json.loads`, scope
  `src/agenthicc/subagents/`: two matches, both in `transcripts.py` (`:306`,
  `:417`), which read back the pool's own JSONL and `pool.json` persistence
  format. No code path parses a `SubagentResult.text` or checks it against
  anything; the pool's only JSON *write* is a message hash (`pool.py:1023`).
- **No submission tool.** Pattern `submit_`, scope
  `src/agenthicc/subagents/`: no match. A worker's only outbound channel is its
  final completion text.

The commit above is the one HEAD pointed at while these checks ran; this
document is committed immediately afterwards, and `git log -1 --name-only` on
that commit lists this file and nothing else, so every claim holds at both.

Diff against the research report's `f843040` snapshot: **nothing has moved** for
G-3. `pool.py:325`, `pool.py:360`, `types.py:51` and the PRD-124 amendment are
still the accurate citations.

Two surprises, both of which changed the design.

**PRD-214 already proposes a "typed output contract", per task.** Its FR-1
requires `spawn_subagents` to accept `deliverable: response | findings_report`,
and its goal 1 is "Ensure a worker is counted successful only when its task's
declared output contract is satisfied"
(`prds/prd-214-reliable-subagent-findings-handoff.md:183-217`, `:150-158`). A
PRD that introduced its own per-task contract field would be the second profile
system the taxonomy exists to prevent. This PRD therefore **does not** add a
`deliverable` field, and §9 defers that whole axis to PRD-214. What PRD-214 does
not define is the *payload* — it validates that a report was submitted, not that
the submitted value has a shape — which is the gap this PRD fills.

**The enforcement point already exists, hard-coded.** The `implementer` rule at
`pool.py:706-719` proves two things: the runtime can decide "this result does
not conform" and fail the worker, and the only thing missing is a way to declare
the rule instead of writing a branch for each type.

## 4. Goals

1. A type definition may declare an `output:` contract — a JSON-schema-shaped
   description of the value its workers return. Absent means today's behaviour:
   plain text, no validation, no parser. (Gap G-3)
2. Where a contract is declared, the worker receives a submission tool whose
   input schema *is* that contract, and its submitted arguments are the value.
   The model fills tool arguments, not free text; the runtime validates the
   value. No prose is ever parsed for structure.
3. A result for a contract-declaring type is a failure — not a success with
   text — when no valid submission arrived, when the submission fails
   validation, or when it is empty. The error names the offending field.
4. The validated value is carried on `SubagentResult.value` (a mapping) and
   survives aggregation unchanged, so a parent can reason about findings instead
   of re-reading a digest. `text` remains the rendered prose, produced by a pure
   renderer over the value.
5. The effective contract participates in the pool and task fingerprint, so a
   cached result produced under a different contract cannot be reused. PRD-214
   FR-1 requires the same for its deliverable kind; both join one fingerprint.
6. Failure is typed and contained: a validation error, a renderer error and a
   projector error each produce a failed `SubagentResult` with a distinct error
   code, and never propagate as an exception out of the pool. (Mechanism
   borrowed from DeepSeek's canonical tool-output contract, M-4.)
7. The boundary with PRD-214 is stated once and implemented once: this PRD owns
   *what a conforming value is*; PRD-214 owns *where the value is delivered* and
   *when a task counts as complete*. One validator, one `deliverable` field.
8. Every default reproduces today's behaviour exactly.

## 5. Non-goals

The A-3 boundaries from the research report §5.3, restated as commitments:

- **Partial output when a worker is stopped or fails.** What is salvageable when
  there is no complete result is SUBAGENT-08 (A-8). This is the boundary most
  likely to be violated, because "partial results" sounds like one topic. It is
  not: this PRD defines the shape of a *complete* result; A-8 defines the
  lifecycle of an *incomplete* one.
- **Deliverable kind, inline-versus-file, and the completion invariant for
  report tasks.** PRD-214 owns those (`deliverable: response |
  findings_report`, the `submit_subagent_report` tool, the Markdown artifact,
  caching and journaling). This PRD neither restates nor replaces them.
- **General file artifacts and writer capabilities.** PRD-180 owns those.
- **Parsing free text into structure.** PRD-124's amendment stands: no
  `result_type`, no `TypedResult`, no output parser. This PRD's value arrives as
  tool arguments, which is a different channel from parsing a completion.
- **An output contract for every tool.** The DeepSeek mechanism is borrowed as
  precedent for subagent results only (research report §5.4 M-4); making every
  agenthicc tool declare a canonical output is a different product decision and
  is not proposed here.
- **Per-call schema selection.** The contract is declared by the type. A caller
  selects a deliverable (PRD-214), not a schema.

## 6. Acceptance criteria

Each criterion names the check that decides it.

1. A type with no `output:` contract behaves exactly as today, including the
   tool-call-summary fallback marking a silent worker successful: existing
   `pytest tests/unit/test_subagent_pool.py -q` and
   `pytest tests/unit/test_subagent_pool_runtime_coverage.py -q` pass unedited.
2. A type declaring `output:` causes the worker's tool set to include a
   submission tool whose input schema is the declared schema, and omits it for
   types that declare none: new cases in `tests/unit/test_subagent_pool.py`.
3. A valid submission populates `SubagentResult.value` and the rendered `text`
   contains the rendered value: `tests/unit/test_subagent_pool.py`.
4. A submission that violates the schema yields `ok=False` with an error naming
   the field, and the pool counts it as failed rather than succeeded:
   `tests/unit/test_subagent_pool.py`.
5. A worker for a contract-declaring type that finishes without submitting
   yields `ok=False` — the same shape as the existing `implementer` rule at
   `pool.py:706-719`: `tests/unit/test_subagent_pool.py`.
6. A renderer that raises produces a failed result with a distinct error code
   and does not raise out of `SubagentPool.run()`:
   `tests/unit/test_subagent_pool.py`.
7. An invalid `output:` schema is refused at definition-load time with the file
   path and the offending key, and the type is not registered:
   `tests/unit/test_subagent_plugin.py`.
8. Two tasks whose types declare different contracts do not share a cached
   result, and the fingerprint changes when the schema changes:
   `tests/unit/test_subagent_pool.py`.
9. `pytest tests/integration/test_subagent_tool_execution.py -q` and
   `pytest tests/integration/test_subagent_executor_integration.py -q` pass
   unchanged, proving the spawn boundary and the parent-facing result shape did
   not shift for types without a contract.

## 7. Rollout, migration and security

**Defaults.** No type declares `output:`, so nothing changes on day one:
`SubagentResult.text` is still the parent's result, the aggregation format is
untouched, and the tool-call-summary fallback still applies. A type opts in by
declaring a contract; a project opts in by defining such a type.

**Migration.** Three ordered steps, each independently shippable:

1. Add `value: Mapping[str, object] | None` to `SubagentResult` with a default,
   and carry it through `_aggregate` without rendering into the digest yet. No
   behaviour change.
2. Add the declaration, the submission tool and validation for types that
   declare a contract.
3. Add the renderer and include rendered values in the digest for contract
   types — this is the only step that changes what the parent reads.

**Definition-format coordination.** AGX-09 §5.1 fixes the frontmatter key set
and makes unknown keys a validation error by PRD-250's inert-field rule
(`prds/agx-09-agent-library-and-switching.md:92`). `output:` is not in that key
list and must be added there in the same change, exactly as SUBAGENT-02 §7
requires for `effort:` and `context:`.

**Security, and where it fails closed.**

- *Schema is data from a possibly untrusted checkout → validate the schema
  itself.* Bounded depth and size, no remote `$ref`, no executable keywords. A
  definition whose schema fails that check is refused at load with the file path
  and the key (§6 criterion 7), not accepted and enforced later.
- *Fail closed on non-conformance.* A worker of a contract type that does not
  submit, or submits an invalid value, is a failed worker. There is no fallback
  to the tool-call summary for those types — that fallback is precisely the
  false success this PRD exists to remove, and silently restoring it for
  contract types would make the contract advisory.
- *Fail open nowhere, contain everything.* Renderer and projector errors
  degrade the *presentation* of an already-validated value, so they are
  contained and reported (§4 goal 6) rather than raised: a pool must not abort
  because a formatter mishandled a value.
- *No secrets in values.* A declared schema describes findings, not credentials;
  the existing redaction rules for transcripts and tool payloads apply to
  submitted values unchanged, and a schema that names a secret-shaped field is
  refused at load.
- *Bounds.* Submitted values are size-capped before they reach the parent's
  context; the existing display truncation
  (`pool.py:1536`, `text[:_MAX_DISPLAY_RESULT_CHARS]`, cap defined at `:92`) applies to the rendered
  form, and the value itself is capped at the submission boundary.

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_plugin.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/integration/test_subagent_tool_execution.py -q
$ pytest tests/integration/test_subagent_executor_integration.py -q
```

New cases belong in `tests/unit/test_subagent_pool.py` (worker behaviour, the
submission tool, validation and failure) and
`tests/unit/test_subagent_plugin.py` (definition-time schema rejection), because
those files already own worker construction and definition validation.

## 9. Related and superseded PRDs

- **PRD-124 — supersedes the "no output contract" clause, and only that.** The
  amendment at `prds/prd-124-concurrent-typed-subagents.md:6-9` removed typed
  result schemas because "LLM output cannot reliably follow a Pydantic schema",
  and ended with "no `result_type`, no `TypedResult`, no output parser". Its
  diagnosis stands and is respected: this PRD never parses a completion. What it
  replaces is the absolute form of the conclusion — a type may now declare a
  value contract, delivered through tool arguments and validated by the runtime,
  which is the one channel where "the model fills a schema" is not the failure
  mode PRD-124 observed. If a future reader finds themselves writing a parser
  for worker prose, this PRD has been misread.
- **PRD-214 — defers to, and narrows against, on one axis.** PRD-214 owns the
  per-task deliverable kind (`response` | `findings_report`), the
  `submit_subagent_report` tool, the Markdown artifact, the success invariant
  for report tasks, retryability and caching
  (`prds/prd-214-reliable-subagent-findings-handoff.md:183-217`). This PRD adds
  the payload contract PRD-214's invariant is checked against, and adds no
  `deliverable` field of its own. Whichever ships second adapts: if PRD-214 lands
  first, its report submission validates the body against this PRD's schema
  rather than inventing a second check; if this PRD lands first, PRD-214's
  `findings_report` deliverable is the transport for a value that satisfies a
  declared contract. Two validators would be a defect, not a feature.
- **PRD-180 — unrelated.** PRD-180 owns file-backed artifact delivery, writer
  capabilities, manifests and research handoffs. That is a transport and
  least-privilege axis; this PRD does not define file delivery, and PRD-214 is
  the document that reconciles the two (its §2.3).
- **AGX-09 — extends.** AGX-09 §5.1 owns the definition format. This PRD adds an
  `output:` key to it and defines what a subagent does with it; the Library tab,
  editing and hot reload stay AGX-09's.

## A. Competitor evidence

| Harness | Evidence | Depth | What it does | Take or reject |
|---|---|---|---|---|
| DeepSeek Harness | `.agents/notes/archived/architecture/2026-07-20-canonical-tool-output-contract.md` @ `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` | code + design notes | Every tool declares `output: { schema, render(args, value), presentationMeta? }`; the registry validates the returned value against `output.schema`, deep-freezes it, then invokes a *pure renderer*; schema, JSON, renderer and projector failures are contained as `ToolOutputError` results | Take all of it: this is the mechanism that makes a contract possible without parsing prose, and the containment rule in §4 goal 6 |
| opencode | `packages/core/src/tool/plugin/subagent.ts:30-54` @ `726cf096396d5014c3b1845e231cc01ee0421ee` | code | The subagent tool returns a typed envelope `{ sessionID, status: "completed" \| "running", output }` rather than a bare string | Take the envelope idea: a result carries its status and payload as separate fields, not concatenated text |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | A subagent returns a final summary to the parent; on a limit or server error it returns its partial output with a note that it was cut, and a background subagent's last output is reported | Take the summary/partial distinction; the partial half is A-8's, not this PRD's |
| Grok CLI | `src/utils/settings.ts:109-112` @ `fb97af83f06dca873281d60168430f06c8de6324` | code | A subagent record is `{name, model, instruction}` and its result is prose; there is no output contract anywhere in the type | Reject: this is the state agenthicc is in, recorded as the contrast case rather than as a model |
| Qoder CLI | `https://docs.qoder.com/cli/subagent` fetched 2026-10-29 | docs-only | Subagents return prose to the caller; the documented surface describes `model`, `effort` and `maxTurns`, not result shape | Reject for value shape; note that this is docs-only evidence, so absence here is weak evidence (§2.1's caveat) |
