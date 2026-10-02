---
title: "PRD-214: Reliable Subagent Findings and File-Backed Handoff"
status: Proposed
version: 1.0.0
date: 2026-10-01
repository: jymchng/agenthicc
related_prds:
  - PRD-124  # Concurrent typed subagents
  - PRD-161  # Exploratory tool-call presentation
  - PRD-169  # Transaction-safe tool-call conversations
  - PRD-180  # Subagent artifact writing and research handoffs
  - PRD-197  # Subagent policy and communication
tags:
  - subagents
  - findings
  - reports
  - artifacts
  - handoff
---

# PRD-214 — Reliable Subagent Findings and File-Backed Handoff

## 1. Executive summary

When a coordinating agent asks several explorers to inspect a repository, some
workers can finish with output such as:

```text
Executed tool call(s): file_exists, list_directory, read_file, grep_file.
```

The calls show that the worker was active, but they do not communicate what it
learned. The pool currently treats this text as a successful result, so the
coordinator receives a green completion marker and an inventory of tool names
instead of the requested facts.

This PRD defines a verifiable findings handoff. A worker assigned to produce
findings must submit a non-empty Markdown report through a worker-scoped report
tool. Agenthicc stores it at a deterministic, session-scoped `.md` path and
returns a concise summary plus a verified artifact reference to the parent.
The worker's private tool history remains private; tool-call inventories are
metadata, never substitutes for findings. A worker that performed tools but
did not submit its required deliverable is `incomplete`, not successful.

The report submission tool is narrowly scoped: it can write only that worker's
assigned report, not arbitrary project files. This lets `explorer` remain
read-only with respect to the user's code. Larger/general artifacts continue
to use the explicit file-delivery capability designed by PRD-180.

## 2. Current-source investigation

### 2.1 Dataflow and confirmed failure path

The parent does not receive a worker's private messages or raw tool results.
`SubagentWorker._execute()` captures the provider run's final
`response.content`; `SubagentPool._aggregate()` then passes worker result text
to the parent as the `spawn_subagents` tool result. Tool calls are separately
tracked for progress, auditing, and changed-path evidence.

When a provider returns no final prose after tool use, `_execute()` already
makes one bounded finalisation turn. If the final content is still empty,
`_tool_call_summary()` builds a string from the worker's tool-call names. The
worker's `run()` method sees that non-empty fallback string and returns
`SubagentResult(ok=True, text=...)`. The pool consequently sets the worker
state to `done`, emits a successful completion event, and counts it as
`succeeded`. `_aggregate()` forwards the inventory to the parent as if it
were the worker's result.

The observed output matches this fallback format. Source and tests confirm the
path:

- `src/agenthicc/subagents/pool.py` states that the final response is the
  parent/worker result boundary and that empty final completions can fall back
  to a tool-call summary.
- `_execute()` derives result text from `response.content`; when absent, it
  may substitute `_tool_call_summary(tool_calls, changed_paths)`.
- `SubagentWorker.run()` treats any remaining non-empty text as success except
  for the special mutation-verification rule for `implementer`.
- `SubagentPool.run()` maps `result.ok` to the worker's `done`/`failed` state
  and persists the tool inventory in the result record.
- `tests/integration/test_subagent_tool_execution.py` currently asserts that
  an empty final response after a tool call produces a successful result whose
  text is `Executed tool call(s): ...`. This test codifies the defect.

### 2.2 What is not causing this example

The TUI limits some worker-completion previews to 2,000 characters, but the
example's tool inventory is short and is the actual aggregate text returned
to the parent. Increasing the preview size would still show only tool names.
The fix must address the worker-to-parent result contract, not merely the
presentation limit.

The system prompt includes a final-response instruction, and the runtime
already tries one bounded finalisation turn. These are useful guidance and
recovery mechanisms, but neither proves that a report was submitted. The
current positive test demonstrates that an empty result can still be marked
successful after the recovery turn.

### 2.3 Relationship to existing PRDs

PRD-180 defines general file-backed artifact delivery, including explicit
writer capabilities, workspace roots, manifests, and research workflow
handoffs. PRD-214 adds the missing completion invariant for findings: the
runtime must distinguish a submitted report from a final-response fallback or
tool log, and report-oriented tasks must have a stable Markdown handoff the
parent can read. Implementation should reuse PRD-180's path, policy, journal,
and artifact-manifest contracts where applicable rather than creating a
second general-purpose file system or widening explorer permissions.

## 3. Problem statement

The current contract conflates **execution evidence** with **task output**:

```text
worker executed tools
        ↓
provider supplied no useful final answer
        ↓
runtime substitutes tool names as result text
        ↓
non-empty text is interpreted as success
        ↓
parent receives no findings but sees a completed worker
```

This loses the information the coordinator delegated the task to obtain.
It also prevents reliable recovery: the pool journal faithfully persists the
tool inventory, and a complete-pool cache may reuse it, but neither contains
the missing conclusions or evidence. The coordinator must repeat the research
itself or respawn workers without knowing which tasks actually produced usable
results.

The contract needs to say what each worker owes its parent, verify that the
deliverable crossed the boundary, and persist a durable reference to detailed
findings without copying arbitrarily large reports into every parent turn.

## 4. Solution options and recommendation

| Option | Assessment |
|---|---|
| Strengthen only the system prompt | Insufficient. The code already has a final-response contract and a bounded finalisation turn, but completion still succeeds with tool-call inventory text. Prompt compliance is not a durable result invariant. |
| Copy all child tool output into the parent transcript | Rejected. It duplicates private execution history, expands parent context, and still does not distinguish evidence from conclusions. |
| Grant general `write_file` to `explorer` | Rejected. It broadens a read-only role and permits arbitrary project changes when the requirement is only to return a report. |
| Use a task-scoped report submission capability and artifact | Recommended. It makes delivery explicit, constrains the only write to one generated Markdown report, and gives the parent a compact summary plus a durable reference. |

The scoped report capability is intentionally distinct from PRD-180's
write-capable role for arbitrary research artifacts. It is the least-privilege
way for a read-only explorer to leave a report for its coordinator.

## 5. Goals

1. Ensure a worker is counted successful only when its task's declared output
   contract is satisfied.
2. Deliver substantive findings to the parent, not merely tool-call history.
3. Persist research/exploration findings as Markdown artifacts under a
   deterministic session-, pool-, worker-, and task-scoped location.
4. Give the parent a compact, actionable summary and a supported way to read
   the complete report.
5. Preserve least privilege: an explorer may submit its report but cannot use
   that capability to write arbitrary project files.
6. Make incomplete reports visible, retryable, journaled, and ineligible for
   successful-result caching.
7. Preserve response-only delivery for tasks that explicitly require a short
   answer, and preserve large general file artifacts through PRD-180.

## 6. Non-goals

PRD-214 does not:

- expose a worker's private chain-of-thought or full conversation memory;
- copy every child message or tool result into the parent transcript;
- claim that a structurally valid report is necessarily factually correct;
- grant explorer/researcher roles general `write_file`, shell, Git, or network
  access;
- replace PRD-180's general artifact writer and manifest contract;
- require every implementation, test, or reviewer task to produce a separate
  research report when its declared output is a different artifact;
- retry a worker indefinitely until the model produces acceptable prose; or
- treat a bounded UI preview as the report's source of truth.

## 7. Product requirements

### FR-1 — Declare the deliverable contract per task

The `spawn_subagents` task input MUST support a typed output contract with at
least:

```text
response        A concise result returned inline to the parent.
findings_report A Markdown report persisted as a session artifact.
```

Example model-facing request:

```json
{
  "type": "explorer",
  "task": "Inspect the resume path and report its invariants and evidence.",
  "context": "Cite source paths and line ranges.",
  "deliverable": "findings_report"
}
```

The runtime assigns the task and artifact IDs; the model cannot choose another
worker's IDs or report path.

For `explorer` and `researcher`, the default MUST be `findings_report`, since
their normal purpose is to gather information for the parent. A caller may
explicitly request `response` for a narrow question whose complete answer is
short. Other roles retain their appropriate existing outputs unless the task
explicitly requests a findings report.

The effective contract and its version MUST be included in the task and pool
fingerprints. A cached response-only result cannot satisfy a report task, and
a cached report for a different destination or task cannot be reused.

### FR-2 — Submit a findings report through a scoped tool

In `findings_report` mode, the worker MUST receive a dedicated
`submit_subagent_report` tool. Its input MUST include a concise answer/summary
and the Markdown report body. The tool MUST:

- validate that the call belongs to the currently running pool, worker, and
  task;
- write only the report path allocated to that task by the runtime;
- write atomically and return a durable artifact identifier, canonical path,
  byte length, and content digest;
- reject empty/whitespace-only reports and malformed or oversized metadata;
- never accept an arbitrary destination path from the model; and
- not grant access to `write_file` or any other filesystem path.

The report tool is an output capability, not a project-mutation capability.
The existing mode, workspace, secret-redaction, and journal policies remain
authoritative. For general files beyond a findings report, use PRD-180's
explicit file-delivery contract.

### FR-3 — Store reports durably at a deterministic path

The recommended workspace-local layout is:

```text
.agenthicc/subagent-reports/
  <session-id>/
    <pool-id>/
      <worker-id>/
        <task-id>/
          findings.md
```

This extends the user's proposed
`./.agenthicc/<session-id>/<subagent-id>/<task-id>/<name>.md` layout with a
pool segment so two concurrent pools in one session cannot collide. All path
segments MUST be runtime-generated or safely encoded identifiers; the model
may choose report contents but not path traversal, symlinks, or another
worker's destination. The implementation must use the session/workspace
artifact policy established by PRD-180 and keep these runtime artifacts out of
ordinary source-control changes by default.

If a workspace-local root is unavailable (for example, a headless session
without a project checkout), the implementation MUST resolve a documented
session artifact root and provide the parent the same safe read operation. A
report write must not silently fall back to an arbitrary current directory.

### FR-4 — Define minimum report content

The worker prompt and tool description MUST require a concise, task-focused
report that distinguishes conclusions from evidence. The standard template
must support:

```markdown
# <task title>

## Answer / outcome
<Direct answer to the delegated question.>

## Findings
<Numbered facts or conclusions, each with evidence references.>

## Evidence
<File path and line range, source URL, test/command and relevant result, etc.>

## Limitations and uncertainty
<Missing evidence, ambiguity, or a result that could not be verified.>
```

Role-specific adapters may add sections such as changed files and tests for an
implementer, but must not replace requested findings with a list of tools.
Reports must not assert facts unsupported by the recorded sources. The runtime
can validate required structure and evidence references; semantic truth
remains the coordinator's responsibility.

### FR-5 — Return a structured result to the parent

For a successfully submitted report, the worker result MUST expose separate
fields, conceptually:

```json
{
  "task_id": "task-2",
  "status": "completed",
  "summary": "The session loader invokes the resume handler before the UI starts.",
  "report": {
    "artifact_id": "opaque-id",
    "path": ".agenthicc/subagent-reports/.../findings.md",
    "bytes": 1842,
    "sha256": "...",
    "verified": true
  },
  "tool_calls": ["read_file", "grep_file"]
}
```

The parent-facing aggregate MUST include the task label, completion status,
the worker's concise summary, and the report reference. Tool names and changed
paths remain separate execution metadata. Tool inventory text MUST NOT be
inserted into the `summary` field.

The parent MUST be able to retrieve the complete report by a supported,
session-scoped read path. Prefer one canonical artifact-reader tool keyed by
`artifact_id` (or one explicitly documented extension of the existing result
collector); do not rely on arbitrary absolute paths or assume a hidden
artifact directory is readable through every workspace tool. Retrieval MUST
verify the report's recorded digest and return a clear stale/missing-artifact
diagnostic if it changed or disappeared.

### FR-6 — Do not count tool inventories as findings

`_tool_call_summary()` or equivalent telemetry MAY remain available for
debugging, but MUST NOT be used as successful task result text. In particular:

- an empty final response plus ordinary read/search tool calls is
  `incomplete` when the task requires a report or inline answer;
- a report task without a verified report artifact is `incomplete`, even if
  its final prose says that it completed;
- an explicit response task requires non-empty response content that is not
  merely the generated tool inventory. The runtime MUST retain whether result
  text came from provider final content, a submitted report, or an internal
  diagnostic; it must not infer a successful answer from its own fallback
  string;
- the existing bounded finalisation turn may still be used once, but its
  exhaustion produces an incomplete result, not false success; and
- the user-facing result MUST explain the missing deliverable and identify
  the affected task so the parent can retry or answer without repeating
  already-complete workers.

The legacy boolean `ok` may remain as a compatibility projection, but it MUST
be false for `incomplete`. The richer status set should distinguish
`completed`, `incomplete`, `failed`, `timed_out`, `cancelled`, and
`awaiting_clarification` where those states are already meaningful.

### FR-7 — Show execution completion separately from deliverable completion

The subagent TUI and scroll output MUST distinguish:

- execution finished with a verified deliverable;
- execution finished but the required report/answer is missing;
- execution failed or timed out; and
- the worker is awaiting clarification.

Only the first state receives a success marker. A tool-only worker may be
shown as execution-finished/incomplete, but not as `✓ done`. Bounded previews
must say they are previews and include the report reference when available.

### FR-8 — Persist and recover report metadata before completion publication

Before emitting the worker-completed event or returning the pool aggregate,
the journal MUST durably record the worker status, output contract, concise
summary, report identifier/path/size/digest, and separate tool-call metadata.
The full Markdown body remains in the report file and is not duplicated into
the provider conversation by default.

On session resume, a report may be reused only if the canonical path, artifact
identity, size, and digest verify. If the file is missing or changed, the
worker result is incomplete/stale and must not be presented as verified.
Writes must be idempotent for the same pool/task attempt and must not overwrite
another pool's artifact.

### FR-9 — Cache only output-contract-complete results

Complete-pool cache entries MUST be written only when every required worker
deliverable is complete and verified. The fingerprint MUST include role,
task/context, output contract version, report path/root policy, and expected
report identity. A worker that has tool calls but no report cannot make a pool
cacheable. Partial pools MUST identify which task IDs need rerun so successful
worker reports need not be discarded or regenerated.

### FR-10 — Guide parent and downstream workflow agents

The `spawn_subagents` tool documentation/system guidance MUST tell the parent
to:

- request `findings_report` for exploration/research intended to inform later
  reasoning or workflow phases;
- inspect the summary and status, not infer results from tool-call telemetry;
- read reports whose full evidence is needed before drawing conclusions; and
- retry only incomplete tasks, preserving successful artifacts.

`create_workflow` authoring tools/prompts and research-heavy built-in workflows
MUST show a complete example of this contract. Generated workflow phase
instructions must request the findings and evidence in the report, and phase
transition tools must not receive or validate large report bodies; they can
carry a short summary and the stable artifact reference. Apply PRD-180's
separate file-delivery contract when a downstream phase needs arbitrary large
files rather than a bounded findings report.

## 8. End-to-end data flow

```text
parent plans explorer/researcher tasks
        │ output contract = findings_report
        ▼
spawn_subagents validates request and allocates pool/worker/task IDs
        │
        ├── runtime-derived path under .agenthicc/subagent-reports/...
        └── worker receives read tools + scoped submit_subagent_report
                    │
                    ▼
         worker investigates; tool results remain private
                    │
                    ▼
         submit_subagent_report(summary, markdown)
                    │
                    ├── validate worker/task identity and report structure
                    ├── atomic write, digest, journal manifest
                    └── return accepted artifact reference
                    │
                    ▼
         SubagentResult(status=completed, summary, report_ref)
                    │
          ┌─────────┴────────────┐
          │                      │
  verified report          no report submitted
  ✓ complete               ⚠ incomplete, not cached
          │                      │
          ▼                      ▼
 parent gets summary +     parent gets task ID + actionable
 report reference          missing-deliverable diagnostic
          │
          ▼
 parent reads full report only when needed and synthesizes answer
```

## 9. Acceptance criteria

### AC-1 — Reproduce and correct the tool-log-only result

Use a deterministic mock provider that executes several read tools, returns
empty final content, and does not submit a report. The result MUST be
`incomplete`/`ok=false`; the parent-facing summary MUST NOT be
`Executed tool call(s): ...`; the call inventory remains available only as
separate diagnostic metadata. The TUI must not render a success marker.

### AC-2 — Report mode carries useful findings and a file reference

A mock explorer submits a Markdown report. Verify the file is written at the
runtime-assigned session/pool/worker/task path, the digest and size match the
bytes on disk, the parent aggregate contains the concise answer and reference,
and the parent read operation returns the complete report.

### AC-3 — Empty final prose does not erase a submitted report

If the worker successfully submits its report but the provider's final prose
is empty, the accepted report summary/manifest still yields a completed
result. A redundant finalization turn is not required solely to restate a
report that has already crossed the validated tool boundary.

### AC-4 — Read-only explorer remains unable to mutate the project

An explorer can submit its assigned report but cannot call general
`write_file`, write outside the report root, choose another worker's path, or
access tools excluded by parent mode/workspace policy. Attempted traversal or
symlink escape is denied and leaves no outside file.

### AC-5 — Missing and malformed reports fail closed

Empty body, absent required evidence section, foreign task identity, failed
write, digest mismatch, or missing file MUST not be reported as completed.
Diagnostics identify the task and the particular missing/invalid condition.

### AC-6 — Multiple concurrent workers do not overwrite reports

Two pools in one session and same-named tasks in different sessions receive
distinct canonical report paths. Repeated submission by the same worker is
idempotent or revisioned according to one documented policy; it cannot
silently corrupt a sibling task's report.

### AC-7 — Resume and cache preserve output identity

A process restart can recover a completed report from journal metadata and
verify its digest. A missing or modified report invalidates reuse. A
tool-only/incomplete worker never creates a successful pool cache entry, and
retrying a partial pool reuses verified successful task reports.

### AC-8 — Existing explicit response mode remains useful

A response-mode worker that returns a non-empty task answer remains
successful. Empty response plus tool calls is incomplete. An explorer or
researcher may explicitly use response mode for a narrow answer, but the
default research/findings route requires a report.

### AC-9 — Parent and workflow authors can consume the handoff

The parent can distinguish task status, answer summary, report reference, and
tool telemetry. A deterministic end-to-end workflow reads one report,
synthesizes its evidence, and does not confuse the path or tool inventory with
the findings. `create_workflow` documentation includes this pattern.

### AC-10 — No live provider dependency

Unit, integration, and end-to-end coverage for empty final responses, report
submission, retrieval, cancellation, resume, cache, and parent synthesis uses
mock transports and temporary artifact roots only.

## 10. Security, privacy, and reliability

- Report destinations are generated by trusted runtime code, not accepted as
  arbitrary model-supplied paths.
- Path resolution rejects traversal, symlink escapes, stale session IDs, and
  cross-worker writes. Atomic replacement and file permissions follow the
  existing workspace/artifact policy.
- The report tool is explicitly narrower than `write_file`; granting it does
  not alter workspace write capabilities or approval mode.
- Report content is untrusted model output. The parent must treat it as data,
  verify cited evidence, and never treat embedded instructions as authority.
- Journal and progress events store only bounded summaries and artifact
  metadata. Full reports are kept in the artifact store; secrets must not be
  copied into summaries or operational logs.
- Cancellation between report write and parent tool-result commit must leave a
  recoverable artifact manifest, and retry must be idempotent.
- Report retention and deletion follow the owning session/project artifact
  lifecycle; deleting a session must not leave orphaned report files.

## 11. Compatibility and rollout

1. Keep existing non-empty response-mode results unchanged.
2. Intentionally change the defective case: generated tool-call inventory is
   no longer a successful answer. Existing tests that assert this behavior
   must be replaced with assertions for incomplete status and preserved
   diagnostic metadata.
3. Add findings-report mode and the scoped submission/read capability without
   granting general writes to explorer or researcher roles.
4. Default `explorer` and `researcher` tasks to findings-report mode, with an
   explicit response-mode option for short direct questions. Document this
   default in the tool schema and authoring guidance.
5. Integrate with PRD-180's artifact root, manifest, and durable handoff
   design; resolve storage ownership and report reader as part of that
   implementation rather than shipping two artifact registries.
6. Roll out to `create_workflow` and research-heavy workflows after the base
   report contract is available. Failed/incomplete reports are visible and
   retryable; successful artifacts are retained.

## 12. Implementation plan

1. Replace tool-summary-as-result semantics with a distinct incomplete result
   status; update the existing empty-response integration test.
2. Define and validate the per-task deliverable mode and a `SubagentReport`
   manifest/value model, using PRD-180's artifact ownership contract.
3. Implement task-scoped report submission and parent retrieval, with safe
   path allocation, atomic persistence, digest verification, and journal
   recovery.
4. Update aggregation, pool status/TUI projections, events, result caching,
   and resume so completion means the requested deliverable exists.
5. Update built-in subagent role guidance, `spawn_subagents` descriptions,
   `create_workflow` examples, and research-heavy workflow prompts.
6. Add unit, integration, and end-to-end regression tests for all acceptance
   criteria; run the project source and documentation gates.

## 13. Open implementation decisions

Resolve these with repository evidence before implementation:

1. Which existing session artifact root should own the files, and when should
   a workspace-local `.agenthicc/subagent-reports/` path be selected versus a
   session-store path?
2. Should the report reader extend the existing `collect_subagent_results`
   contract or be one dedicated artifact-read tool? The implementation must
   choose one canonical retrieval API.
3. What Markdown structure is mandatory for all reports versus role-specific
   (explorer, researcher, reviewer, tester, implementer) fields?
4. Should resubmitting a report atomically replace the same task artifact or
   create a revisioned artifact with a stable latest pointer?
5. What report size/retention defaults fit existing checkpoint and session
   artifact policies without truncating successful reports?
6. How should `SubagentPoolState.done` count `incomplete` terminal workers
   while preserving compatibility for existing projections?

## 14. Definition of done

The coordinator cannot mistake tool execution for delivered findings. A
research/exploration task is successful only after the required Markdown
report is durably submitted and verifiable; the parent receives its substantive
summary and can retrieve the full report. Missing reports are explicit,
non-cacheable, and retryable. The explorer remains read-only outside this
single report-output capability, and all listed acceptance criteria pass
without live model calls.
