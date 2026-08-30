---
title: "PRD-180: Subagent Artifact Writing and Reliable Research Handoffs"
status: Proposed
version: 1.0.0
created: 2026-08-30
scope: "subagent tool capabilities, file-backed artefacts, research workflows, and generated workflow authoring"
related_prds:
  - PRD-68  # feature expectations and current subagent contracts
  - PRD-124 # concurrent typed subagents
  - PRD-125 # tool namespace and capability expansion
  - PRD-129 # conversation durability and retry resilience
  - PRD-161 # exploratory tool-call presentation
  - PRD-163 # cache-stable workflow prompts and generated workflows
  - PRD-169 # transaction-safe tool-call conversations
  - PRD-174 # tool-aware create_workflow authoring
  - PRD-175 # runtime AGENTS.md integration
tags:
  - subagents
  - write-file
  - artifacts
  - research
  - workflows
  - create-workflow
  - durability
  - capabilities
---

# PRD-180 — Subagent Artifact Writing and Reliable Research Handoffs

## 1. Executive summary

When a workflow asks several subagents to research independently, the workers
often collect more material than can safely fit in a final model response. The
workflow therefore tells them to write complete notes into a research
directory. In the reported run, four workers identified themselves as
`researcher`, produced useful findings, and then attempted to call
`write_file`. Every call failed with:

```text
lauren_ai.ToolExecutor: agent '_SubAgent' called unknown tool 'write_file'
```

The failure is a capability-contract mismatch, not a filesystem failure. The
built-in `researcher` role is deliberately read-only and its allow-list does
not contain `write_file`. A subagent receives the intersection of the tools
visible to the parent turn and the tools allowed by its role. Therefore a
parent that can write files does not automatically give a `researcher` worker
the ability to write files. The worker's model can still attempt the name if
the task or prompt asks for it, but lauren-ai's executor has no registered
callable to dispatch and returns an error. No note is written.

There is a second, related problem. The subagent result is currently a
response-oriented boundary: a worker is expected to put its work product in
its final response, and the parent receives an aggregate of those responses.
The TUI and kernel events intentionally show only bounded previews, while
the provider-facing aggregate and durable journal retain complete text. This
is safe for normal summaries but is a poor delivery mechanism for long
research notes or chapter-sized artefacts. A model may shorten the response,
the parent may have to carry a large aggregate through another context
window, or a user may mistake a 2,000-character presentation preview for the
stored result.

This PRD defines an explicit, least-privilege artifact-delivery contract:

1. Keep `researcher` read-only.
2. Add an explicitly named write-capable research role (or an equivalent
   per-task artifact capability) whose provider schema really contains
   `write_file` and whose worker prompt requires complete file delivery.
3. Validate the effective tool set before spawning a worker. A request that
   asks a read-only role to write must fail with an actionable role/capability
   diagnostic, before the provider can emit an unknown tool call.
4. Give artifact tasks an explicit output root and return a manifest containing
   paths, sizes, hashes, and status rather than copying complete notes through
   the parent response.
5. Preserve complete file bytes and durable manifests. Bounded TUI/event
   previews must be labelled as previews and must never be the source of
   truth.
6. Teach `make_book`, `create_workflow`, and generated custom workflows to
   use the artifact contract for large research output and to select a
   write-capable role deliberately.

The design does not grant every subagent filesystem writes, remove workspace
security, merge private worker conversations into the parent, or disable all
resource protection. It makes the desired write operation explicit and
verifiable.

## 2. Problem statement

### 2.1 The reported failure

The observed sequence is:

```text
parent workflow
  └─ spawn_subagents(tasks=[four {type: "researcher", ...}])
       ├─ researcher #1 investigates
       ├─ researcher #2 investigates
       ├─ researcher #3 investigates
       └─ researcher #4 investigates

researcher prompt: "write each research notes file ..."
worker model: emits write_file(path=..., content=...)
worker ToolExecutor: write_file is absent from its tool map
result: unknown tool 'write_file'
filesystem: no notes written
parent: receives prose/error instead of durable research files
```

The error message is emitted by lauren-ai's `ToolExecutor._dispatch()` when
the provider tool-call name is not in the executor's registered map. It does
not mean that `write_file` is broken globally. It means that this particular
`_SubAgent` instance did not receive that tool.

### 2.2 Why the model attempted a tool that was unavailable

The current worker pipeline has several independent sources of instructions:

1. The parent task says that notes must be written to a directory.
2. A workflow prompt may repeat that instruction and mention the literal
   `write_file` name.
3. The role prompt for the built-in `researcher` says it is read-only.
4. The provider receives the worker's effective tool schema, which excludes
   `write_file` for `researcher`.

The task-specific instruction is concrete and often comes after the generic
role prompt. A model can follow the concrete instruction despite the role
allow-list. Tool schemas are constraints, not a guarantee that a model will
never emit an invalid name. The runtime must handle this mismatch explicitly;
it must not rely on a model silently inferring that a requested operation is
forbidden.

### 2.3 Why the notes appear truncated

There are three different size boundaries and they must not be conflated:

| Boundary | Current behaviour | Consequence |
|---|---|---|
| Worker private memory | Fresh `ShortTermMemory(max_tokens=8_000)` | Long investigation history can be compacted or trimmed before the final answer |
| Parent/provider result | Complete worker text is aggregated when it is returned | Large notes consume the parent's context and can be shortened by a model or provider context limit |
| UI/kernel presentation | Worker completion text is previewed at 2,000 characters | The screen can show an apparently truncated result even when the aggregate is complete |

The current pool intentionally does not truncate the provider-facing aggregate
or journal record. That is a useful invariant, but it does not solve delivery
of a large artifact when the worker's final response is the only value channel.
The correct fix is not to make every display field unbounded. The correct fix
is to write the complete artifact through the filesystem tool and return a
small, verifiable manifest.

### 2.4 Existing capability contract

The current built-in role allow-lists are intentionally differentiated:

| Role | Relevant current policy |
|---|---|
| `researcher` | Read/search-oriented; no `write_file` |
| `explorer` | Read-only; no writes |
| `planner` | Read-only; no writes |
| `implementer` | Includes `write_file`, `patch_file`, and `append_file` |
| `executor` | Includes implementer tools plus command tools |
| `tester` | Includes test and selected write tools |
| `documenter` | Includes documentation-oriented file writes |
| `reviewer` / `verifier` | No general file writes |

The role list is intersected with the parent's already filtered tools. A role
allow-list is therefore a ceiling, not a grant that bypasses the parent mode,
workspace, or capability policy. PRD-180 must retain this rule.

## 3. Goals

### 3.1 Product goals

- Make long-lived subagent research output reliably available as files.
- Make it impossible for a workflow prompt to accidentally request an
  unavailable tool without receiving a useful diagnostic.
- Make the distinction between response delivery and artifact delivery
  explicit in the provider schema, worker prompt, aggregate, TUI, and journal.
- Preserve complete notes exactly as written, including Unicode, code,
  equations, citations, and line breaks.
- Allow `make_book` and generated workflows to fan out research and hand off
  durable files to later phases without copying the complete notes into every
  parent LLM turn.
- Keep resume and retry deterministic: a completed artifact should be reused
  or verified rather than silently regenerated with a different result.

### 3.2 Engineering goals

- Keep the capability decision at the existing subagent/pool boundary.
- Use the existing `Tool`, `ToolResultEnvelope`, workspace policy, journal,
  and conversation-store contracts.
- Make the provider-visible tool schema and lauren-ai dispatch map derive from
  the same effective tool list.
- Add structured diagnostics and provenance without logging file contents,
  API keys, or other secrets.
- Keep old response-oriented `spawn_subagents` calls compatible.

## 4. Non-goals

PRD-180 does not:

- make the built-in `researcher` role generally mutating;
- grant write access to `explorer`, `planner`, `reviewer`, or `verifier`;
- allow a child to widen the parent's workspace, mode, or tool set;
- make subagents share the parent's provider message history;
- remove the existing bounded UI/event preview policy;
- copy every worker message into the parent conversation;
- silently truncate an artifact to fit an LLM response;
- create a second filesystem, memory, or workflow engine;
- make the parent automatically trust a path merely because a model returned
  it;
- guarantee that an external provider can produce arbitrarily large output in
  one response;
- make browser, network, shell, or git capabilities implicit in a research
  writer role.

## 5. Evidence-backed architecture and ownership

The implementation must remain within the current ownership boundaries:

| Concern | Owner | PRD-180 responsibility |
|---|---|---|
| Task input and provider schema | `src/agenthicc/subagents/tool.py` | Add delivery/artifact fields and validate role/tool compatibility |
| Built-in role policy | `src/agenthicc/subagents/types.py` | Add an explicit write-capable research role; keep `researcher` read-only |
| Worker filtering and execution | `src/agenthicc/subagents/pool.py` | Install effective tools, write manifest evidence, and deliver bounded summaries |
| File writes and path policy | `src/agenthicc/tools/fs/agent_tools.py`, `workspace_access.py` | Enforce the existing workspace boundary and exact writes |
| Provider tool dispatch | lauren-ai `ToolExecutor` and agent metadata | Ensure schema, metadata, and dispatch map cannot drift silently |
| Parent turn injection | `src/agenthicc/runners/agent_turn.py` | Pass the effective visible tools and session services unchanged |
| Workflow research handoff | `src/agenthicc/workflows/make_book/runner.py` | Spawn/use writer roles and consume file manifests |
| Generated workflow authoring | `src/agenthicc/workflows/create_workflow/` | Teach and validate file-backed artifact delivery |
| Durable result records | `src/agenthicc/memory/journal.py` | Persist complete manifest/provenance before completion projection |
| TUI display | `ConversationStore` and workspace renderers | Label previews and expose artifact paths/status |

No component may create a parallel global tool registry or bypass the existing
`ToolCapabilityGate` and workspace access policy.

## 6. Proposed design

### 6.1 Explicit delivery modes

Extend the task request with an optional delivery contract. Existing callers
that omit it retain response delivery:

```json
{
  "type": "researcher_writer",
  "task": "Research chapter 2 and persist the complete evidence notes.",
  "context": "Write only under the supplied research directory.",
  "delivery": "files",
  "artifact_dir": "Book/research",
  "artifact_paths": [
    "Book/research/ch02-notes.md",
    "Book/research/sources.md"
  ]
}
```

The exact field names may be refined during implementation, but the following
semantics are mandatory:

| Field | Required semantics |
|---|---|
| `delivery` | Enum with at least `response` and `files`; default is `response` for backwards compatibility |
| `artifact_dir` | A workspace-relative or policy-approved root under which the worker may write |
| `artifact_paths` | Optional expected paths used for validation and manifest completeness; paths must be relative to `artifact_dir` or otherwise canonicalised safely |
| task/context | Must describe the content and acceptance conditions, not rely on a hidden parent memory |

Validation must reject `delivery="files"` without an artifact root. It must
also reject an artifact path that escapes the workspace or artifact root,
including `..` traversal, an absolute path outside the approved scope, and a
symlinked destination that resolves outside the approved scope.

### 6.2 Explicit write-capable research role

Add a role with a name that makes its authority obvious. The recommended
canonical name is `researcher_writer`; an implementation may call it
`artifact_researcher` if that name is used consistently everywhere.

Its allow-list should be the smallest useful set:

- read/search tools required to investigate;
- `file_exists` and/or `get_file_info` for verification;
- `write_file` for complete atomic note creation;
- `append_file` only if a justified streaming/chunking workflow requires it;
- no shell, git-write, browser, network, or recursive subagent spawning by
  default.

`researcher` remains read-only. A task that asks `researcher` to write must
not be silently upgraded. The parent receives a validation error such as:

```text
researcher is read-only and cannot satisfy delivery=files; use
researcher_writer or documenter, and provide artifact_dir
```

The role prompt for `researcher_writer` must state:

1. investigate and verify evidence;
2. write the complete notes with `write_file` under the supplied artifact
   root;
3. create every required parent directory through the normal tool contract;
4. reread or stat each successful file where practical;
5. return a concise summary plus an exact artifact manifest;
6. never replace a required file with a prose claim that it was written;
7. never write outside the approved root.

### 6.3 Preflight capability validation

Before constructing or scheduling a worker, compute:

```text
parent_visible_tools
  ∩ role_allowed_tools
  ∩ delivery_requirements
  ∩ capability_policy
  ∩ workspace_policy
```

For `delivery="files"`, preflight must prove that `write_file` is present
and that the worker has an approved artifact root. If not, return a structured
failure without making a provider request. The diagnostic must identify:

- task index and role;
- requested delivery mode;
- missing capability/tool;
- whether the parent mode or role allow-list removed it; and
- the valid alternatives.

Preflight must not add `write_file` to a read-only role. It must select an
explicit writer role or require the parent to change the task.

### 6.4 One effective tool list, three consumers

The same effective tool list must drive all three surfaces:

```text
effective_tools
   ├─ provider function schema (`@use_tools` / agent metadata)
   ├─ runtime `ToolExecutor` dispatch map
   └─ human-readable worker capability instructions
```

The worker construction path must not register a tool in only one of these
surfaces. After `@use_tools(*filtered)` and tool population, tests must assert
that the provider schema names and executor dispatch names agree.

When a provider nevertheless emits an unknown name, lauren-ai must return a
structured `unknown_tool` result containing the attempted name and a safe,
bounded list of available names. It must not expose prompts, secrets, or
unbounded internal metadata. The worker should stop or perform one bounded
correction turn according to the existing retry contract; it must not invent a
tool or loop indefinitely.

### 6.5 File-backed artifact result

A successful file-delivery worker returns a structured result equivalent to:

```json
{
  "ok": true,
  "delivery": "files",
  "summary": "Collected and verified evidence for chapter 2.",
  "artifacts": [
    {
      "path": "Book/research/ch02-notes.md",
      "kind": "research_notes",
      "bytes": 18432,
      "sha256": "...",
      "verified": true
    },
    {
      "path": "Book/research/sources.md",
      "kind": "sources",
      "bytes": 4210,
      "sha256": "...",
      "verified": true
    }
  ],
  "missing_artifacts": [],
  "error": ""
}
```

The manifest is metadata, not a second copy of the note. The full bytes remain
in the file. The worker's final prose and parent aggregate should contain the
summary and manifest, not a full chapter-sized duplicate, unless the task
explicitly requests response delivery as well.

The manifest must be produced from successful tool evidence and filesystem
verification, not solely from paths claimed in model prose. A path whose
write failed or whose hash/stat cannot be verified must not be marked
`verified=true`.

### 6.6 Exact-write and no-silent-truncation rules

- `write_file` content must be written exactly as supplied, subject only to
  the existing encoding and workspace contracts.
- Artifact delivery must never truncate content to fit the parent response.
- If an explicit resource quota is introduced, exceeding it must return a
  structured failure before claiming completion; it must not write a partial
  success and silently shorten the file.
- TUI/kernel previews may remain bounded, but must carry a `preview=true`
  marker or equivalent label and expose the artifact path/manifest.
- Journal records must retain the complete manifest and status. They need not
  duplicate complete file bytes because the files are the artifact source of
  truth, but resume must verify existence and hash before reuse.

### 6.7 Artifact-aware aggregation and resume

The aggregate sent to the parent should be compact and actionable:

```text
researcher_writer #2: completed
summary: ...
artifacts:
  - Book/research/ch02-notes.md (18,432 bytes, sha256 ...)
  - Book/research/sources.md (4,210 bytes, sha256 ...)
```

The complete worker result and pool manifest are persisted to the session
journal before completion events are emitted. The pool fingerprint must
include the delivery mode, artifact root, expected paths, role, task, and
context so a response task cannot incorrectly reuse a file-delivery result or
reuse files from a different directory.

On resume:

1. load the durable artifact manifest;
2. validate that each path is still in the approved workspace/root;
3. verify existence, size, and hash;
4. reuse verified artifacts without spawning duplicate workers; and
5. rerun only missing or changed tasks, with an explicit status explaining
   why reuse was not possible.

A stale or missing file must never be reported as a successful cached result.

### 6.8 `make_book` research handoff

The `make_book` research phase currently asks for complete files under
`<output_dir>/research` while its final handoff also accepts a potentially
large `notes` list. The implementation must reconcile those two paths:

- research fan-out uses `researcher_writer` (or an explicitly equivalent
  writer role) with `delivery="files"`;
- each worker receives a deterministic chapter note path and the shared
  research root, subject to workspace policy;
- the parent receives summaries and manifests, not all note bodies;
- `submit_research` accepts file-backed notes as the authoritative handoff;
- it validates that required research files exist under the research root and
  records manifest/provenance data;
- any legacy inline `notes` input remains supported for small response-mode
  callers, but large file-backed runs do not need to duplicate note bodies;
- later chapter phases are instructed to read the persisted research files;
- a missing/unverified file keeps the research phase incomplete or causes an
  actionable rejection, rather than allowing an apparently complete phase.

The phase must not ask a read-only `researcher` to execute `write_file`.

### 6.9 `create_workflow` and generated workflows

The `create_workflow` authoring workflow must teach the artifact contract to
downstream workflow authors. Its tool catalog and prompts must show:

- the distinction between read-only research and write-capable research;
- how to choose a role based on required capabilities;
- how to define an artifact root and deterministic expected paths;
- how to return a manifest instead of embedding large artifacts in a phase
  transition payload;
- how to make later phases read those files;
- how to include artifact verification in phase completion criteria;
- how checkpoints persist paths/hashes and revalidate them on resume;
- how retries avoid duplicate writes through idempotent paths and manifest
  checks;
- how the generated runner preserves the cache contract, conversation
  continuity, workspace policy, and boundary checkpoint contract from
  PRD-163, PRD-169, PRD-174, PRD-175, and PRD-179.

Static authoring validation must reject a generated workflow that:

- selects a read-only role while requiring file delivery;
- names `write_file` in a prompt but does not expose it through the phase's
  effective tool contract;
- transitions with an inline unbounded artifact payload instead of a path/
  manifest for a large result;
- claims artifact completion without a verification path;
- persists live tool objects or private worker memory in its checkpoint.

The authoring workflow should generate a small file-backed handoff example so
agents can copy a known-good pattern rather than infer one from prose.

## 7. End-to-end data flow

### 7.1 Current failing flow

```text
user/workflow prompt
        │
        ▼
parent AgentTurnRunner._build_agent()
  visible_tools = mode/capability/workspace-filtered tools
  inject spawn_subagents(all_tools=visible_tools)
        │
        ▼
parent calls spawn_subagents({type: researcher, task: "write notes"})
        │
        ▼
SubagentPool creates SubagentWorker
  filtered = visible_tools ∩ researcher.allowed_tools
  write_file is absent
        │
        ▼
@use_tools(*filtered) + populate_agent_tools()
  worker provider schema and executor map omit write_file
        │
        ▼
model emits write_file anyway
        │
        ▼
lauren-ai ToolExecutor._dispatch()
  no callable named write_file
  returns unknown-tool error
        │
        ▼
no file; worker may return prose or failure
parent aggregates response; UI may show only a bounded preview
```

### 7.2 Correct file-delivery flow

```text
workflow chooses researcher_writer + delivery=files
        │
        ▼
spawn_subagents validates:
  role is writer-capable
  parent exposes write_file
  artifact_dir and expected paths are policy-approved
        │
        ▼
SubagentWorker computes one effective tool list
  researcher/search tools + file_exists + write_file
        │
        ├─ same list → provider schema
        ├─ same list → ToolExecutor dispatch map
        └─ same list → worker capability instructions
        │
        ▼
worker investigates and calls write_file(path, complete_content)
        │
        ▼
workspace guard authorizes path
filesystem writes exact bytes atomically
        │
        ▼
worker verifies file, size, and sha256
        │
        ▼
worker result = summary + artifact manifest
        │
        ├─ journal persists complete worker/pool manifest
        ├─ TUI/kernel receive bounded preview + paths/status
        └─ parent receives compact manifest, not full note bodies
        │
        ▼
make_book submit_research / next phase reads files by manifest path
        │
        ▼
checkpoint stores manifest and fingerprint
        │
        ▼
resume verifies hashes and reuses valid files
```

### 7.3 Data ownership table

| Data | Worker private memory | Parent provider memory | TUI/kernel event | Journal/checkpoint | Filesystem |
|---|---:|---:|---:|---:|---:|
| Full research note bytes | Temporary source | No, by default | No | No duplicate required | **Authoritative** |
| Summary | Yes | Yes | Bounded | Yes | Optional |
| Artifact path/size/hash | Yes | Yes | Yes | **Yes** | Derived |
| Tool-call error | Yes | Yes if failed | Bounded | Yes | No |
| Secrets/content previews | Never beyond existing tool policy | Never in diagnostics | Redacted/bounded | Redacted | Existing file policy |

## 8. Functional requirements

### FR-1 — Explicit capability profiles

The system MUST retain a read-only `researcher` role and MUST provide a
documented, registered write-capable research role or an equally explicit
artifact capability. The default `researcher` provider schema MUST NOT contain
`write_file`.

### FR-2 — Artifact delivery schema

`spawn_subagents` MUST accept a backwards-compatible explicit file-delivery
contract containing delivery mode and an approved artifact root. Expected
paths, when supplied, MUST be validated and included in the fingerprint.

### FR-3 — Preflight rejection

The tool MUST reject an incompatible role/delivery combination before a model
request. The result MUST name the missing capability and a valid replacement.

### FR-4 — Schema/dispatch parity

The provider-visible schema, lauren-ai executor map, and worker capability
description MUST be generated from one effective tool list. A regression test
MUST fail if `write_file` appears in one surface but not another.

### FR-5 — Structured unknown-tool diagnostics

An invalid provider tool call MUST result in a structured, bounded diagnostic
with an error kind, attempted name, worker role, and safe available-tool list.
It MUST NOT execute an unregistered callable or loop without a bound.

### FR-6 — Exact artifact persistence

Successful artifact writes MUST preserve the complete content exactly. The
artifact path MUST be workspace-approved, and the resulting manifest MUST
record successful verification evidence.

### FR-7 — Manifest-based delivery

File-delivery workers MUST return summaries and manifests. Complete artifact
content MUST NOT be required in the parent response for a task marked
`delivery="files"`.

### FR-8 — Preview labelling

Bounded TUI/kernel text MUST be explicitly labelled as a preview. The UI MUST
show or make discoverable the artifact path and completion/error status.

### FR-9 — Durable resume

Complete worker and pool manifests MUST be journaled before completion
events. Resume MUST verify hashes before reusing artifacts and MUST rerun only
invalid/missing work.

### FR-10 — Idempotent retry

Retrying a file-delivery task with the same fingerprint and valid artifact
manifest MUST not duplicate provider work or corrupt the file. Partial failed
tasks MUST not be cached as complete.

### FR-11 — `make_book` integration

The `make_book` research handoff MUST support file-backed notes, use a
write-capable research role for file delivery, validate the research manifest,
and make persisted files available to chapter phases.

### FR-12 — `create_workflow` authoring guidance

`create_workflow` prompts, tool catalog, generated examples, and static
validation MUST teach and enforce the artifact-delivery contract. Generated
workflows MUST not ask a read-only researcher to write.

### FR-13 — Existing response mode compatibility

Existing tasks that omit delivery metadata MUST continue to return the current
response-oriented aggregate. Existing read-only roles MUST not gain writes as
an incidental compatibility change.

## 9. Non-functional requirements

### NFR-1 — Security

- Artifact writes MUST pass the existing capability and workspace gates.
- Paths MUST be canonicalised and checked against the approved root before
  and during the write.
- The worker MUST not widen the parent's capabilities, workspace, or network
  policy.
- Diagnostics, manifests, events, and logs MUST redact secrets and MUST NOT
  include full note bodies by default.
- Symlink, traversal, and race-sensitive path checks MUST fail closed.

### NFR-2 — Durability

Manifest records MUST be flushed through the existing journal contract before
the corresponding completion event or parent result is considered durable.
File writes MUST use the existing atomic/safe filesystem mechanisms where
available.

### NFR-3 — Determinism

Task fingerprints, expected paths, manifest ordering, and aggregate ordering
MUST be deterministic. Concurrent completion order MUST NOT change the
parent-visible task ordering.

### NFR-4 — Performance

The parent context SHOULD carry O(number of artifacts) metadata rather than
O(total artifact bytes). Hashing and stat operations SHOULD avoid reading a
file more than necessary, and large files MUST not be copied into UI events.

### NFR-5 — Operability

Failures MUST identify whether the cause was role policy, parent mode, path
policy, tool dispatch, filesystem I/O, provider output, or artifact
verification. Operators MUST be able to find the complete artifact from the
manifest without scraping a TUI preview.

### NFR-6 — Backwards compatibility

The existing task input and response result shape remain valid when the new
delivery fields are omitted. New roles and result metadata may be additive;
changing `researcher` from read-only to write-capable is explicitly not
allowed.

## 10. Testing strategy

### 10.1 Unit tests

Add deterministic tests for:

1. `researcher` excludes `write_file` and `researcher_writer` includes it.
2. A `delivery="files"` task without `artifact_dir` is rejected.
3. A read-only `researcher` with file delivery is rejected with an actionable
   message before a transport call.
4. Parent-visible absence of `write_file` is reported distinctly from a role
   allow-list absence.
5. Provider schema names and executor dispatch names are identical for every
   worker role.
6. An invalid tool name produces a bounded structured unknown-tool result.
7. Relative paths, nested paths, traversal, absolute paths, and symlinks are
   handled according to the workspace policy.
8. `write_file` preserves long Unicode, code, equation, and newline-rich
   content byte-for-byte.
9. A successful write produces the expected path, byte count, and SHA-256;
   failed writes cannot appear as verified artifacts.
10. The aggregate contains a manifest and summary but not the full note body
    for file delivery.
11. UI/event previews are bounded and labelled while the durable manifest is
    complete.
12. Delivery mode, artifact root, expected paths, role, task, and context
    change the task fingerprint.
13. A valid cached manifest is reused; a deleted, changed, or out-of-scope
    artifact is rejected and not treated as a cache hit.
14. Existing response-mode task inputs retain their current result shape.

### 10.2 Integration tests

Add isolated temporary-workspace tests covering:

1. A mocked provider worker with `researcher_writer` writes several chapter
   notes concurrently; all files exist and contain complete content.
2. The real subagent tool factory, pool, tool registration, and executor
   dispatch one `write_file` call end to end without an unknown-tool error.
3. A parent with a mode-filtered tool set cannot give a writer role a missing
   `write_file` capability; no provider request is made.
4. The worker journal is written before completion events and can reconstruct
   the complete manifest after reopening.
5. Retry after a transient provider failure does not duplicate or truncate a
   successful file.
6. Partial pool failure is not cached as a successful pool result.
7. `make_book` accepts file-backed research manifests and the chapter phase
   can read the resulting notes.
8. `create_workflow` generated source includes the artifact role, delivery
   fields, verification, cache contract, and checkpoint metadata.
9. Workspace scope and path policy reject writes outside the configured
   project/output directory even when the worker is otherwise write-capable.

### 10.3 End-to-end tests

Add provider-mocked E2E journeys for:

1. A `make_book` research phase spawning four writer workers, producing one
   complete note file per chapter plus sources/summary manifests, and handing
   them to chapter phases.
2. A long note substantially larger than the TUI preview limit. The TUI shows
   a labelled preview, while the file hash/size proves the complete content
   is persisted and the parent receives a usable path.
3. A user interrupts after workers finish but before the parent turn commits;
   resume discovers the journaled manifest and does not rerun completed work.
4. A user runs a generated custom workflow created by `create_workflow`; its
   research phase writes files, its next phase reads them, and its checkpoint
   resumes from the correct boundary.
5. A negative journey in which a read-only `researcher` is asked to write;
   the user receives a clear correction rather than repeated unknown-tool
   errors or a false completion.
6. A retry with one missing artifact reruns only that worker and preserves the
   other workers' verified files.

## 11. Rollout and migration

### Phase 1 — Contract and diagnostics

- Add delivery types, role metadata, preflight validation, schema/dispatch
  parity checks, and structured unknown-tool diagnostics.
- Keep all existing callers in response mode.
- Add telemetry counters for incompatible role requests and unknown-tool
  attempts, with no content logging.

### Phase 2 — Artifact result and durability

- Add manifest creation, filesystem verification, journal persistence, cache
  fingerprints, and resume validation.
- Add the focused unit and integration suites before enabling workflow use.

### Phase 3 — Workflow adoption

- Update `make_book` prompts/tools and its research handoff.
- Update `create_workflow` catalog, prompts, validation, examples, and
  generated source contract.
- Add the E2E journeys and test a generated workflow in a temporary project.

### Migration rules

- Existing `researcher` tasks remain valid for read-only research.
- Existing workflows that need file output must change their role/task to the
  explicit writer contract; the runtime must not infer a privilege upgrade.
- Existing inline notes remain supported for small response-mode workflows.
- Existing journal entries without artifact manifests are not treated as
  verified file-delivery cache entries; they may remain available as legacy
  response results.
- Regenerated custom workflows should adopt the new contract, while existing
  generated files continue to run under the runtime's current capability and
  checkpoint validation.

## 12. Failure handling and observability

Every file-delivery task has a finite, typed outcome:

| Failure kind | Example | Required behaviour |
|---|---|---|
| `invalid_delivery_request` | missing `artifact_dir` | reject before provider call |
| `capability_unavailable` | parent Plan mode removed `write_file` | explain role/mode and valid alternative |
| `unknown_tool` | provider emitted unregistered `write_file` | bounded structured error; no execution |
| `workspace_denied` | path escapes project root | fail closed; no file outside scope |
| `write_failed` | permissions/disk/encoding failure | mark artifact unverified; preserve error |
| `verification_failed` | hash/stat mismatch | do not report completion or cache |
| `provider_failed` | timeout/transport error | use existing bounded retry contract |
| `partial_pool` | one of four workers failed | return partial status; never cache complete |

TUI output should be equivalent to:

```text
✓ [2/4] researcher_writer #2  32.4s  completed
  └ artifacts: Book/research/ch02-notes.md (18 KB), sources.md (4 KB)
  └ summary preview: ...
```

The `summary preview` is explicitly not the artifact. The path and manifest
are the user-facing recovery route. Kernel and journal payloads must use
bounded previews and redacted metadata while preserving full manifests.

## 13. Acceptance criteria

The PRD is complete only when all of the following are true:

1. A built-in `researcher` worker cannot execute `write_file`, and the denial
   is intentional, tested, and documented.
2. A dedicated file-capable research worker can execute `write_file` when the
   parent-visible tool set and workspace policy allow it.
3. A researcher task that requests file delivery with a read-only role fails
   during preflight with a corrective diagnostic; it does not reach an
   unknown-tool provider call.
4. The provider schema and runtime executor agree on the effective tool set.
5. Four concurrent writer workers can create four complete research files in
   a temporary output directory.
6. A long note is not shortened in the file, even when the TUI preview is
   bounded.
7. The parent receives a compact path/hash/size manifest and can direct the
   next phase to read the file without receiving the full note in its prompt.
8. Artifact paths cannot escape the workspace or declared output root.
9. A failed or incomplete write cannot be marked verified or cached as a
   successful worker/pool result.
10. Worker and pool manifests survive journal reopen and are available after
    session resume.
11. Resume reuses valid artifacts and reruns only missing or changed ones.
12. Retry does not duplicate successful writes or produce a partial file
    falsely reported as complete.
13. `make_book` uses the file-backed research handoff and later phases can
    consume the persisted files.
14. `create_workflow` instructs downstream authors to select a write-capable
    role, define artifact roots, verify manifests, and avoid large inline
    payloads.
15. A generated workflow containing a read-only writer mismatch is rejected
    by static validation or reports a preflight error before model execution.
16. Existing response-mode `spawn_subagents` calls and read-only workflows
    remain backwards compatible.
17. Unit, integration, and E2E coverage described in this PRD passes with
    deterministic temporary workspaces and mocked providers.
18. No log, event, manifest, prompt, or error path leaks secrets or complete
    sensitive file contents by default.

## 14. Definition of done

- Source, tests, and documentation agree on the role/capability contract.
- `researcher` remains read-only; file writing is explicit and inspectable.
- `write_file` is available to a writer worker only when the effective parent
  and workspace policies permit it.
- Complete artifacts are persisted exactly once or reused by verified
  manifest, and large content is not transported unnecessarily through LLM
  context.
- `make_book` and `create_workflow` use the new contract in prompts, tools,
  validation, checkpoints, and resume paths.
- Unit, integration, and E2E tests pass.
- Relevant guides, storage documentation, public PRD index, and implementation
  evidence are updated.
- Any existing static-analysis or Nox blockers are reported rather than
  hidden.

## 15. Verification commands

The implementation should be verified with at least:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

Focused additions should also be runnable independently, for example:

```bash
uv run pytest tests/unit/test_subagent_artifact_delivery.py -q
uv run pytest tests/integration/test_subagent_artifact_delivery.py -q
uv run pytest tests/e2e/test_subagent_artifact_delivery_e2e.py -q
```

If lauren-ai's executor or tool-schema implementation changes, run its
focused unit/integration suite and the corresponding agenthicc E2E suite
together. The two repositories form one provider-tool contract.
