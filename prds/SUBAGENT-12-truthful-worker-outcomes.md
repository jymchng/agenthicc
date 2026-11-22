---
title: "SUBAGENT-12: Subagents behave like the main agent, and report truthfully"
status: Implemented (2026-11-22)
version: 0.2.0
date: 2026-11-22
scope: "what a working subagent is given (every tool the parent exposes and the session turn limit, as the main agent has them) and what it may report as success: a per-type completion-evidence contract, a hard failure when the turn or output limit ended the run, failure with the real cause when every attempted change was denied or blocked, and no fabricated success text for an empty answer"
area: A-10
related_prds:
  - SUBAGENT-01
  - SUBAGENT-05
  - SUBAGENT-10
  - SUBAGENT-13
  - SUBAGENT-14
  - PRD-124
  - PRD-214
tags:
  - subagents
  - correctness
  - mutation-evidence
  - turn-limits
  - approvals
---

# SUBAGENT-12: Subagents behave like the main agent, and report truthfully

## Implementation status (2026-11-22)

Implemented as specified; every §7 criterion is a test in
`tests/unit/test_subagent_outcomes.py`. Differences from the first draft, taken
on the requester's direction that *subagents must behave like the main agent*:
the evidence-nudge turn was removed (§5.2) and the working roles inherit every
parent tool and the session turn limit instead of a corrected list. Policy
refusals are observed at the gate itself (`FailClosedGate.refuse` →
`refusal_observer`), because lauren-ai does not run `after_tool_call` for a
call a `before_tool_call` hook aborted.

## 1. Executive summary

A subagent worker reports success (`ok=True`, a green `✓` in the digest) whenever
lauren-ai's `runner.run()` returns without raising. That is the same root cause
the August 2026 fix recorded for the implementer ("subagents were marked
successful when runner.run() returned without raising"), but that fix was applied
to **one type, by name** (`self._spec.name == "implementer"`). Every other path
to a false success is still open, and each was reproduced against the current
code (§3):

| # | Observed on current `main` | Parent is told |
|---|---|---|
| R1 | `tester`, `executor` and `documenter` answer in prose and call no tool | `✓` and the model's claim of work |
| R2 | A write is **denied** (`approval_denied`) or **blocked** (`blocked_by_mode`); the model then writes "I wrote out.txt" | `✓` for `documenter`/`tester`/`executor`; for `implementer`, a failure that does not say why |
| R3 | The worker uses all `max_turns` while still calling tools | `✓ Executed tool call(s): …` — and a second, fresh turn budget is spent on the "finalisation" turn |
| R4 | A reply is cut off by the output-token limit (lauren-ai maps it to `stop_reason="max_turns"`) | `✓` with truncated text |
| R5 | A `verifier` ends with no verdict | `✓ Executed tool call(s): read_file.` — no `VERIFIED`/`NOT VERIFIED` |
| R6 | A mutating role narrates ("Now verifying the report…") instead of calling its write tool | implementer: failure; others: `✓` |
| R7 | The working roles are handed a hand-picked tool list (the implementer had no `run_tests`, `apply_diff` or `move_file`; the executor and tester no `apply_diff`) and 15–24 turns, where the main agent has every tool and 200 turns | the worker improvises through `run_python_expr` or runs out of turns |

The fix is one principle, not six patches: **a working subagent behaves like the
main agent**. It is given what the main agent has — every tool the parent
session exposes and the session's own turn limit — and, because it is trusted
with the same tools, its success is decided from evidence the runtime observed
by a per-type rule the type declares. When that evidence is missing the parent
receives the *reason* the runtime saw.

## 2. Problem statement

The requester's report was that subagents "don't use mutating tools". Two
different things produce that symptom and the code treats them identically:

1. **The model never called a mutating tool.** Nothing makes it try again.
2. **The model called one and the runtime refused it.** The model saw
   `{"ok": false, "error_code": "approval_denied"}` or `"blocked_by_mode"`, but
   the worker discards that code. The parent sees either a bare success or the
   generic "completed without a successful mutating tool call".

Neither is recoverable by the parent, because the parent cannot tell them apart
from "the worker did the work".

## 3. Current-source investigation (measured, 2026-11-22)

All results below come from driving the real `spawn_subagents` tool with the
built-in types, the real 56-tool catalog and a scripted `MockTransport`.

### 3.1 Only the implementer has an evidence rule

`pool.py` `SubagentWorker._run` fails a worker only when
`self._spec.name == "implementer" and not successful_tool_calls`.
`_successful_tool_calls` is appended to by `_MutationEvidenceHook` only for
tools in `mutating_tools`. Result for a prose-only completion (no tool call at
all):

```
implementer  ok=False  "implementer completed without a successful mutating tool call"
tester       ok=True   ✓ "Done. I tester handled it and everything looks good."
executor     ok=True   ✓ "Done. I executor handled it and everything looks good."
documenter   ok=True   ✓ "Done. I documenter handled it and everything looks good."
```

A name check is also not a contract: a Markdown definition that overrides
`implementer` (`override: true`) is built in `agent_turn.py`
`_build_definition_subagent_registry` as a plain `SubagentTypeSpec`, and keeps
the check only because it reuses the name.

### 3.2 Denied and blocked writes are reported as work

Safe-mode parent with a broker that denies, model calls `write_file` then
answers "I wrote out.txt with hello.":

```
documenter   ok=True  ✓ "I wrote out.txt with hello."   (out.txt does not exist)
implementer  ok=False "…completed without a successful mutating tool call" + the false claim as partial text
```

Plan-mode parent, `tester` calls `write_file`: the tool result the model sees is
`blocked_by_mode … Switch to Yolo mode or annotate it as read-only.`; the worker
returns `✓` with the false claim. `_MutationEvidenceHook.after_tool_call` already
sees these results (it filters on `result.get("ok") is False`) and throws the
`error_code` away.

### 3.3 The turn limit is invisible

`lauren_ai` returns `AgentResponse(content, turns, stop_reason)`; `stop_reason`
is `"max_turns"` when the loop is exhausted **and also when a completion stops
for `max_tokens`** (`_runner.py`: `if completion.stop_reason == "max_tokens":
stop_reason = "max_turns"`). `SubagentWorker._run_once` stores only
`response.content`. With `max_turns=3` and a model that keeps calling tools, the
mock transport served **6** provider calls: the run exhausted its 3 turns, the
text was empty, and the "finalisation" recovery in `_execute` called
`runner.run()` again with a fresh 3-turn budget. The worker then returned
`✓ Executed tool call(s): write_file, list_directory ×5`. The recorded session
`c14af6ba…` (2026-11-05) shows the same signature in production: an implementer
that wrote one file and finished with `ok: true, partial: false` and the text
"Now verifying the written report parses and re-checking the headline numbers
against it." — a progress note, not a result.

### 3.4 An empty answer becomes a fabricated one

`_run` replaces empty text with `_tool_call_summary(...)`
("Executed tool call(s): …"). For a worker whose product is *text* (explorer,
reviewer, verifier, researcher) that is not a result. The recorded session shows
`verifier #1 (✓ 169.7s)` whose entire answer was a list of 43 tool names, and
the existing test `test_empty_final_response_gets_a_tool_execution_summary`
asserts `ok is True` for it.

### 3.5 What already works and is out of scope

* Leaked tool-call markup (DSML etc.) is detected and fails after one correction
  (`textual_tool_calls.py`, dd3b3c6). Unchanged.
* Broker-less pools and the main turn both leave `approval_required`
  capabilities ungated when no `ApprovalBroker` is supplied; this is documented
  (`docs/guides/subagents.md`, "the absence of an approval service does not
  create a new approval UI") and consistent between the two paths. Not changed.
* Budget `drain` lets a running worker finish. Not changed.

## 4. Goals

* **G1.** Success is decided by a per-type rule declared on the type, not by the
  type's name.
* **G2.** A working subagent has what the main agent has: every tool the
  parent exposes, and the session's agent turn limit. Nothing a hand-written
  list forgot can stop it from doing its job.
* **G3.** A run that ended on the turn limit or the output-token limit is a
  failure with a named `stop_reason`, never a success, and triggers no further
  provider turn.
* **G4.** When a worker attempted changes and none succeeded, the failure names
  the tool and the runtime's error code.
* **G5.** A worker with no final answer fails unless its type's product is the
  work itself and the evidence rule is satisfied.
* **G6.** The same truth reaches the TUI, kernel events and the parent digest
  (carried by SUBAGENT-14).

## 5. Design

### 5.1 `SubagentTypeSpec.completion_evidence`

```python
CompletionEvidence = Literal["none", "mutation", "action"]

@dataclass
class SubagentTypeSpec:
    ...
    completion_evidence: CompletionEvidence = "none"
```

| Value | A worker succeeds only if it made… |
|---|---|
| `none` | no requirement (text is the product) |
| `mutation` | ≥ 1 successful call to a mutating tool |
| `action` | ≥ 1 successful call to a mutating **or** execute-capability tool |

Built-ins: `implementer` → `mutation`; `executor`, `tester` → `action`; all
others `none` (a `documenter` may be asked to *return* rewritten content, which
its prompt allows, so it is governed by §5.4 instead). The hard-coded
`self._spec.name == "implementer"` branch and its
"no write-capable tools" `RuntimeError` are replaced by this field (one path).
A Markdown definition that overrides a built-in name inherits that built-in's
`completion_evidence`; all other definitions default to `none`. A "success" is a
call whose result is not a mapping with `ok is False`, as today.

### 5.2 Working roles inherit what the main agent has

`SubagentTypeSpec.allowed_tools` becomes `frozenset[str] | None` and
`max_turns` becomes `int | None`; `None` means *inherit*:

* `allowed_tools=None` — every tool the parent session exposes. The parent's
  mode, phase, capability policy and ancestor scopes remain the ceiling
  (`AgentCapabilityScope(allowed_tools=None)` is already "unrestricted within
  the inherited scope"), so inheriting cannot widen what the session allows.
  Nested `spawn_subagents` follows `subagents.max_depth`, not a per-type list.
* `max_turns=None` — `ExecutionSettings.max_agent_turns`, the limit the main
  agent runs under (200 by default), so a delegated implementation is not cut
  off sooner than the same work would be in the main turn.

"Every tool the parent exposes" excludes the parent's **session control
plane**: tools with the `control` capability ("advances an internal
workflow/session state machine" — phase transitions, `spawn_worker_agents`,
`ask_user`) and the parent's own `spawn_subagents`. A worker that could call
`narration_ready` would close the parent's phase; one that could call
`ask_user` would question the human past the parent. The worker reaches the
parent through its own `ask_parent` / `send_parent_message` tools, and gets a
fresh nested `spawn_subagents` only when `subagents.max_depth` allows one.

Built-ins: `implementer`, `executor`, `tester` and `documenter` inherit both.
The investigative roles (`explorer`, `planner`, `reviewer`, `verifier`,
`researcher`) keep explicit read-only lists, because "this role cannot write"
is a guarantee that must not rest on a prompt. The per-role tool constants for
the four working roles are deleted, not left as dead code.

**Rejected: a corrective "nudge" turn** for a worker that only narrates. It
existed to paper over a restricted tool list; with the full tool set the
worker has no excuse, a hidden extra provider call is a cost the parent does not
see, and a nudge that named tools had to choose which to name (an alphabetical
slice dropped `write_file`). A worker that only narrates now fails at once with
`missing_mutation` or `missing_action`.

### 5.3 Turn and output limits

`_run_once` records `AgentResponse.stop_reason` and `turns`.

* `stop_reason == "max_turns"` and `turns >= spec.max_turns` → worker fails,
  `stop_reason="max_turns"`, `partial=True` when any text or tool call exists,
  error `"reached its {n}-turn limit before finishing"`.
* `stop_reason == "max_turns"` and `turns < spec.max_turns` → the reply hit the
  output-token limit → `stop_reason="output_truncated"`.

Neither case runs the finalisation turn (a fresh budget would double the
configured limit).

### 5.4 Attempted-and-refused changes

`_MutationEvidenceHook` records, for each mutating or execute-capability call
whose result is `{"ok": false, "error_code": C}` with `C` a **policy refusal** —
`approval_denied`, `blocked_by_mode`, `outside_workspace` or
`permission_denied` (`tools/errors.py` `ToolErrorCode`) — the string
`"{tool}: {C}"` (first 8, de-duplicated) in `SubagentResult.blocked_calls`.
Other failures (a stale patch, a failing test run) are ordinary tool results the
model reacts to; they are not refusals and are not recorded here.

If `blocked_calls` is non-empty and the worker has no *success evidence*
(a successful mutating call; for `action` types, a successful mutating or
execute call), the worker fails with `error_code="mutation_refused"` and the
error text `"{tool} was refused ({C}); no change was made"`, whatever the type's
`completion_evidence`. The model's prose is kept as partial text. A worker that
was refused and then succeeded by another permitted route is judged on its
evidence, not penalised for the earlier refusal; the refusal still appears in
the evidence footer (SUBAGENT-14).

### 5.5 No fabricated success text

If, after the existing single finalisation turn, the text is empty:

* the worker has success evidence (defined below) → keep the tool-call summary
  as the result (the work is the product);
* otherwise → `ok=False`, `stop_reason="empty_response"`, the tool-call
  inventory is kept as **partial** text so the parent can see what ran.

"Evidence is met" here means any successful mutating call (or, for `action`
types, mutating or execute call), whatever the type: a `documenter` that wrote
a file and then said nothing has produced its work product.

### 5.6 Documentation drift

`make_spawn_subagents_tool` docstrings say child workers "run with an isolated
Yolo policy". They inherit the parent's mode (`SubagentExecutionPolicy`
snapshot). The text is corrected.

## 6. Non-goals

* No change to approval semantics, workspace scope or budgets.
* No new frontmatter key for Markdown definitions (inheritance in §5.1 only).
* No retry of a refused call and no corrective turn for a worker that only narrates (§5.2).
* No change to structured-result (`output_schema`) workers, which already
  require a validated submission.

## 7. Acceptance criteria

1. A prose-only `implementer` fails with `stop_reason="missing_mutation"`, and a
   prose-only `tester` or `executor` with `missing_action`; each makes exactly
   one provider call.
2. `implementer`, `executor`, `tester` and `documenter` have
   `allowed_tools is None` and `max_turns is None`; the investigative roles
   have explicit lists.
3. An `implementer` worker's provider request lists every tool the parent
   exposed, including a project tool no built-in role names; an `explorer`'s
   request lists none of `write_file`, `patch_file`, `run_bash` or that tool.
4. A working role with `max_turns=None` stops at
   `ExecutionSettings.max_agent_turns` (5 in the test) and reports
   `"5-turn limit"`.
5. A `documenter`/`tester`/`executor`/`implementer` whose only mutating call is
   refused (`approval_denied`, `blocked_by_mode`) fails with
   `error_code="mutation_refused"` naming the tool and code, and the file does
   not exist.
6. A worker that exhausts its turn limit fails with `stop_reason="max_turns"`,
   `partial=True`, and the transport saw exactly that many calls (no
   finalisation turn).
7. A completion with provider `stop_reason="max_tokens"` fails with
   `stop_reason="output_truncated"`.
8. A `verifier` with an empty final answer after one finalisation turn is
   `ok=False`, `stop_reason="empty_response"`, partial text lists its tool calls.
9. An `implementer` that wrote a file and then returned empty text succeeds
   with the tool-call summary naming the changed path.
10. A Markdown definition overriding `implementer` keeps `mutation` evidence; a
    custom type defaults to `none`; a custom type named `implementer` is not
    special.
11. A type that needs a write tool fails fast, with no provider call, when the
    session policy blocks writes (Plan mode).
12. A working role's request lists none of the parent's `control`-capability
    tools (a phase tool, `ask_user`) nor the parent's `spawn_subagents`, and
    still lists its file and command tools.
13. `depth_tiers` validation accepts a tier on an inherit-all type and still
    rejects one that grants tools outside an explicit list.
14. No occurrence of `name == "implementer"` remains in `pool.py`; `tool.py` no
    longer claims workers run an "isolated Yolo policy".

## 8. Verification

Unit tests in `tests/unit/test_subagent_outcomes.py` drive `SubagentWorker`
through `spawn_subagents` with `MockTransport`; the integration tests that
assumed the old behaviour
(`test_empty_final_response_gets_a_tool_execution_summary`) are updated, not
shimmed. `uv run pytest tests/unit tests/integration -q -k subagent`,
`ruff`, `mypy`, and `scripts/type_audit.py` gate the change.

## 9. Related PRDs

* SUBAGENT-10 introduced the remediation pattern this extends.
* SUBAGENT-13 makes the tool surface match the evidence rules.
* SUBAGENT-14 puts the evidence in front of the parent.
