---
title: "SUBAGENT-14: Parent-verifiable worker evidence in the spawn digest"
status: Implemented (2026-11-22)
version: 0.1.0
date: 2026-11-22
scope: "what the parent model and the kernel event log learn about each worker besides its prose: files changed, tools run, calls the runtime refused, and turns used"
area: A-10
related_prds:
  - SUBAGENT-09
  - SUBAGENT-12
  - SUBAGENT-13
  - PRD-180
  - PRD-214
tags:
  - subagents
  - observability
  - handoff
  - events
---

# SUBAGENT-14: Parent-verifiable worker evidence in the spawn digest

## Implementation status (2026-11-22)

Implemented as specified; criteria 1–7 are tests in
`tests/unit/test_subagent_evidence.py`. §5.1 (`changed_paths` records successes
only) and the `blocked_calls` / `turns` fields of §5.2 shipped with SUBAGENT-12,
whose evidence hook they come from; this change adds the digest line, the event
payload keys and the journal record fields. The formatter is
`agenthicc.subagents.pool.evidence_footer`. SUBAGENT-15 extends the line with
where an isolated worker's changes went.

## 1. Executive summary

The runtime already observes, per worker, which tools ran (`tool_calls`), which
paths mutating tools were given (`changed_paths`) and — after SUBAGENT-12 —
which calls it refused (`blocked_calls`). None of that reaches the parent model.
`spawn_subagents` returns one string, `results`, built from each worker's
**prose**, plus `values` for structured workers. The parent therefore cannot
check a claim like "I updated `src/x.py`" and must either trust it or redo the
work — the failure mode PRD-214 fixed for findings but not for changes.

This PRD adds a one-line **evidence footer** under each worker in the digest and
the same fields to the `worker_done` kernel event, so the parent, the TUI, the
journal and replay all carry the same observed facts.

## 2. Problem statement

Measured against the current code, a worker that wrote two files and one that
wrote none produce digests that differ only in their prose:

```
=== implementer #1 (✓ 0.1s) ===
Changed chapters/01.md.

=== implementer #2 (✓ 0.1s) ===
Changed chapters/02.md.
```

Whether `chapters/02.md` exists is not in the result. `SubagentResult` carries
`changed_paths` and `tool_calls`; `_aggregate` (`pool.py`) ignores both for
successful workers, and the `spawn_subagents` return dict has no per-worker
list. The recorded session of 2026-11-05 shows the effect: a `tester` result
whose text was the raw call markup, an `implementer` whose text was a progress
note, and a `verifier` whose text was a list of tool names — all rendered with
`✓`, and the parent's next message summarised them as done.

## 3. Current-source investigation

* `pool.py` `_aggregate` writes `=== {label} ({status}) ===` and `result.text`;
  `partial` adds a stop-reason line. Nothing else from `SubagentResult`.
* `SubagentResult.changed_paths` is populated from `ToolCallStarted` events for
  mutating tools, i.e. the **requested** paths, including calls that were later
  refused. It is therefore not proof of a change, and must be reported next to
  the refusals, or be restricted to successful calls (§5.1).
* `_emit_worker_done` (`pool.py`) sends `ok`, `text`, `error`, `tool_calls`,
  `changed_paths`, `stop_reason`; it does not carry refusals or turns.
* Custom aggregators (`SubagentAggregator`) replace the section body entirely.

## 4. Goals

* **G1.** The parent can see, without trusting prose, what a worker changed,
  ran, and was refused.
* **G2.** `changed_paths` means changes that **succeeded**.
* **G3.** The footer is bounded and deterministic so it does not destabilise the
  resume fingerprint or inflate the parent's context.
* **G4.** The same data is in the `worker_done` event.

## 5. Design

### 5.1 `changed_paths` records successes only

Today the path hints are taken at `ToolCallStarted`. They move to the existing
after-call hook, keyed to the call id, and are recorded only when the call
returned a non-`ok: false` result. A refused or failed write contributes to
`blocked_calls` (SUBAGENT-12 §5.4), never to `changed_paths`.

### 5.2 `SubagentResult` additions

```python
blocked_calls: tuple[str, ...] = ()   # "write_file: approval_denied"  (SUBAGENT-12)
turns: int = 0                        # provider turns used, across nudge/finalisation runs
```

### 5.3 The footer

For each worker rendered by the default aggregator, after the body, one line
containing only the parts that apply, in this order:

```
[evidence: changed chapters/01.md, chapters/02.md (+1 more) · ran run_tests ×2, grep_files ×5 · refused write_file: approval_denied · 7 turns]
```

* at most 5 paths, then `(+N more)`; at most 6 distinct tools by call count,
  ties by first use; at most 3 refusals; each path/tool/refusal truncated to
  120 characters;
* omitted entirely for a `not_started` worker and for a worker that ran no
  tools and used ≤ 1 turn;
* never contains worker prose, so redaction (`_redact_result_value`) has
  nothing new to cover; paths pass through the same `WorkspaceView`-relative
  rendering the panel uses.

Types with a registered `SubagentAggregator` keep full control of their text;
the footer is not added to a custom section. This is documented as a plugin
contract.

### 5.4 Event payload

`worker_done` gains `blocked_calls` (list of strings) and `turns` (int).
`subagent_worker_done` replays through `kernel/reducer.py` unchanged because the
reducer stores the payload mapping; the schema note in `subagents/events.py`
and `docs/reference/` event tables are updated. Old logs without the keys decode
with `[]` / `0` through `.get`, so no migration exists or is needed.

## 6. Non-goals

* No structured per-worker list in the `spawn_subagents` return dict (the digest
  string is what the parent model reads; `values` stays for contracts).
* No diff content, file hashes or command output in the footer.
* No change to the panel; `changed_paths` there simply becomes more accurate.

## 7. Acceptance criteria

1. A worker whose `write_file` succeeded shows `changed <path>` in the digest;
   one whose `write_file` was `approval_denied` shows
   `refused write_file: approval_denied` and **no** `changed` part.
2. A worker with no tool calls and one turn has no footer.
3. More than 5 changed paths render exactly 5 plus `(+N more)`.
4. The footer text is identical across two runs of the same scripted pool
   (determinism), so the resume fingerprint is unaffected.
5. A custom aggregator's section has no footer.
6. `subagent_worker_done` payloads include `blocked_calls` and `turns`, and a
   log written before this change still replays.
7. `SubagentResult.changed_paths` never contains a path from a call whose result
   had `ok: false`.

## 8. Verification

`tests/unit/test_subagent_evidence.py` for the formatter and bounds;
integration tests extend `tests/integration/test_subagent_tool_execution.py`
for the approval-denied and success cases; the replay test loads a fixture event
without the new keys. Existing tests that assert the exact digest text are
updated to include the footer, not loosened to substring checks that would hide
a regression.

## 9. Related PRDs

* SUBAGENT-09 defines the transcript/event contract this extends.
* SUBAGENT-12 supplies `blocked_calls` and the stop reasons shown in the line.
* PRD-214 made findings reliable; this does the same for changes.
