---
title: "PRD-175: Runtime AGENTS.md Integration"
status: Proposed
version: 0.1.0
created: 2026-08-26
scope: Load bounded project instructions into every eligible agent turn
related_prds:
  - PRD-92   # typed agent-turn context
  - PRD-95   # workflow configuration
  - PRD-100  # code_plan architecture
  - PRD-138  # repository improvement roadmap
  - PRD-147  # workflow-native extension authoring
  - PRD-154  # create_workflow architecture
  - PRD-156  # resumable workflow continuation
  - PRD-163  # cache-stable workflow prompts
  - PRD-168  # workspace-scoped access policy
  - PRD-169  # tool-call transaction integrity
  - PRD-170  # durable workflow recovery
  - PRD-171  # single live session owner
  - PRD-173  # recoverable workflow errors
  - PRD-174  # tool-aware create_workflow authoring
tags:
  - agents-md
  - project-instructions
  - prompts
  - prompt-cache
  - sessions
  - workflows
  - checkpoints
---

# PRD-175 — Runtime AGENTS.md Integration

## 1. Summary

agenthicc currently knows how to create, inspect, and update a project
AGENTS.md file through project_bootstrap.py, but the file is not part of the
runtime agent contract. A project can therefore contain carefully maintained
guidance that is visible to a human and to /init while being invisible to the
LLM.

This PRD integrates AGENTS.md into the existing session and agent-turn
boundaries. A session-owned instruction manager discovers applicable files,
reads them under strict size and path rules, creates a deterministic immutable
snapshot, and supplies that snapshot to direct turns, workflow phases, retries,
resumes, and inherited subagents. The rendered instruction block belongs to the
stable system-prompt region. It is never appended to the provider conversation
as a synthetic user or assistant message and never placed in the reactive TUI
ConversationStore.

The feature is enabled by default. Projects without non-empty applicable
instruction files retain their current effective prompt. Existing workflow
memory, the stable session conversation_id, journal positions, tool
transactions, and checkpoint context remain the sources of truth for
conversation and recovery. AGENTS.md adds policy guidance; it does not grant
capabilities or bypass mode, workspace, approval, browser, MCP, or checkpoint
enforcement.

## 2. Problem statement

The current bootstrap lifecycle is:

    agenthicc init or /init
      -> inspect project manifests
      -> create or propose AGENTS.md
      -> optionally atomically write the file
      -> no runtime consumer

The intended lifecycle is:

    session construction
      -> establish canonical WorkspaceScope and session conversation
      -> discover applicable AGENTS.md files
      -> read and validate bounded files
      -> create immutable InstructionSnapshot
      -> inject snapshot into every AgentTurnContext
      -> render it in the stable prompt/cache prefix
      -> retain only snapshot metadata in workflow checkpoints
      -> refresh at the next logical boundary when source files change

Without this integration:

- project build, testing, style, architecture, and safety guidance is not
  available to the agent that is expected to follow it;
- a direct user turn, a code_plan phase, and a create_workflow phase can make
  different decisions about the same repository;
- generated workflows may read or encode project instructions themselves,
  creating duplicate prompt logic, unstable cache prefixes, and unsafe path
  handling;
- a resumed workflow cannot explain which instruction snapshot was active when
  its checkpoint was created; and
- operators cannot distinguish “no AGENTS.md exists” from “AGENTS.md was
  rejected, too large, unreadable, or outside the authorized workspace.”

The root cause is an ownership gap: project_bootstrap.py owns file creation and
inspection, while session_context.py and agent_turn.py own runtime prompt
construction, but no component connects those boundaries.

## 3. Goals

1. Make applicable AGENTS.md guidance available to all eligible agent turns
   through one session-owned, immutable instruction snapshot.
2. Define deterministic discovery, precedence, size, encoding, and path
   containment rules.
3. Preserve prompt-cache efficiency by putting unchanged instructions in the
   stable system-prompt region and changing the cache identity only when their
   content or applicable file set changes.
4. Keep instructions separate from lauren-ai provider memory, the durable
   conversation journal, and the reactive TUI transcript.
5. Make direct turns, workflow phases, retries, plan approval, background
   execution, headless execution, subagents, and resume use the same snapshot
   semantics.
6. Preserve workflow-scoped memory and checkpoint context while recording enough
   redacted provenance to explain an instruction change during recovery.
7. Ensure create_workflow teaches and validates the inherited instruction
   contract rather than generating a second AGENTS.md reader.
8. Provide bounded, redacted diagnostics for operators and deterministic tests
   for discovery, rendering, refresh, caching, resume, and failure paths.
9. Preserve backwards compatibility for projects with no usable instructions,
   existing bootstrap commands, existing workflow files, and older checkpoints.

## 4. Non-goals

- AGENTS.md does not become executable code, a plugin manifest, a tool
  permission file, or a way to grant network, filesystem, shell, browser, MCP,
  Git, or secret access.
- This PRD does not replace the existing project_bootstrap proposal and
  atomic-write behavior.
- This PRD does not introduce a second conversation store or reconstruct
  provider history from TUI-rendered events.
- This PRD does not implement arbitrary remote instruction URLs. Remote
  instructions, repository-wide instruction registries, and instruction
  synchronization are future work.
- This PRD does not add CLAUDE.md or README.md as implicit instruction sources.
  Existing files may continue to be inspected as project metadata by /init, but
  only AGENTS.md names are runtime instructions in this version.
- This PRD does not let a child workflow or subagent select a broader
  instruction root than its owning session.
- This PRD does not guarantee that a model will follow malicious or ambiguous
  text. It constrains the authority and exposure of that text.
- This PRD does not promise an unbounded prompt. Bounded loading and normal
  provider context-window and compaction policies remain in force.

## 5. Current-state evidence

The implementation must be based on the current source tree, not historical
workflow examples.

| Surface | Current behavior | Gap addressed here |
|---|---|---|
| project_bootstrap.py | Creates an empty AGENTS.md and proposes a managed section based on local manifests | No runtime loader consumes the file |
| ProjectSnapshot | Records instruction file names as project metadata | Does not provide contents, precedence, hashes, or runtime authority |
| SessionContext | Owns session memory, workspace scope, browser/MCP resources, and the stable session id | Has no session-scoped instruction snapshot |
| WorkflowConfig | Carries the resources shared by all workflow phases | Has no instruction contract for generated or built-in workflows |
| AgentTurnContext | Carries turn memory, tools, prompt contract, mode, and workspace access | Has no project-instruction input |
| AgentTurnRunner._build_agent | Builds the base/role/MCP/tool prompt and the cache contract | Does not load or render AGENTS.md |
| PromptContract | Separates stable system/tool regions from dynamic phase context and fingerprints the stable contract | Does not include project-instruction identity |
| TUISession and headless runner | Reuse session resources and the stable conversation id | Cannot refresh or explain instruction state |
| Workflow checkpoints | Preserve typed workflow state and recovery metadata | Do not record the instruction snapshot identity |
| create_workflow | Teaches generated workflows to use the existing cache, context, workspace, and checkpoint contracts | Generated workflows could independently read AGENTS.md or omit inherited metadata |
| project_bootstrap tests | Cover creation, preservation, force, atomicity, and safe target checks | No discovery, prompt, cache, workflow, or resume coverage |

## 6. Product contract

### 6.1 Source names and precedence

Runtime discovery uses only these names:

- user-global: ~/.agenthicc/AGENTS.md, when present;
- per-directory override: AGENTS.override.md; and
- per-directory normal guidance: AGENTS.md.

The canonical workspace root comes from the existing WorkspaceScope. If a
caller has no WorkspaceScope, the session constructor resolves the configured
project root using the existing workspace rules; it must not silently use an
arbitrary process current directory when a safer root is available.

For a session whose effective working directory is workspace-root/sub/area,
discovery examines the workspace root, sub, and area in that order. It never
walks above the authorized workspace root. At each directory:

1. if AGENTS.override.md exists, it is used for that directory and
   AGENTS.md in the same directory is ignored;
2. otherwise a non-empty AGENTS.md is used; and
3. an absent, empty, or whitespace-only file contributes no prompt content.

The rendered order is broad to narrow:

    user-global AGENTS.md
    workspace-root AGENTS.md or AGENTS.override.md
    workspace-root/sub AGENTS.md or AGENTS.override.md
    workspace-root/sub/area AGENTS.md or AGENTS.override.md

Later, more-specific content can refine earlier guidance. It cannot override
the agenthicc runtime contract. The renderer labels every source and clearly
states that capability and security rules remain authoritative.

The user-global file is a user-owned instruction source, but it is still
untrusted text from the perspective of the runtime. It receives the same
delimiter and authority treatment as a project file.

### 6.2 Configuration

Add an additive configuration section with these defaults:

    [instructions]
    enabled = true
    include_global = true
    max_file_bytes = 65536
    max_total_bytes = 262144

The implementation may expose the names through the repository's existing
configuration model rather than requiring this exact TOML spelling, but the
defaults and semantics are fixed:

- enabled=true makes the feature active for new and resumed sessions;
- include_global=false disables only ~/.agenthicc/AGENTS.md;
- limits are hard upper bounds, not advisory values;
- there is no allow_symlinks setting; symlink instruction files are always
  rejected; and
- a project can disable runtime loading without deleting its AGENTS.md.

Invalid, negative, or unreasonably large configured limits are rejected during
configuration validation. Existing configurations with no instructions table
use the defaults.

### 6.3 InstructionSnapshot

Introduce a typed, immutable runtime model owned by the session layer. The
names may vary, but the implementation must provide equivalent data:

    InstructionDocument
      display_path       redacted, stable user-facing path
      scope              global or workspace
      relative_path      relative to its owning root, when applicable
      sha256             content digest
      byte_length
      line_count
      content            bounded normalized UTF-8 text, runtime-only

    InstructionSnapshot
      schema_version
      snapshot_id        digest of ordered source identities and content digests
      workspace_root     canonical root identity, not raw secret-bearing paths
      effective_directory
      documents          ordered InstructionDocument values
      skipped            bounded metadata and reason codes only
      total_bytes
      stable_fingerprint

The snapshot must be JSON-safe for diagnostics except that document content is
excluded from diagnostics and checkpoints. Content may exist in process memory
for prompt construction. Do not include callable repr values, object
addresses, environment values, API keys, authorization headers, MCP payloads,
or provider conversation messages.

Text is normalized deterministically for hashing and rendering: UTF-8 is
required, newline style is normalized to LF, and a final newline does not
change the semantic document identity. Hashes are computed over the normalized
bounded content.

The snapshot is immutable after construction. Refresh creates a new snapshot;
it mutates neither the prior snapshot nor workflow context in place.

### 6.4 Safe discovery and failure behavior

The loader must:

- reject paths that resolve outside the owning root;
- reject symlinks, directories, devices, sockets, and non-regular files;
- reject files larger than max_file_bytes before reading unbounded content;
- stop adding files when max_total_bytes would be exceeded;
- reject invalid UTF-8 without passing replacement characters to the model;
- read only the known candidate paths; no recursive globbing or arbitrary
  directory enumeration is permitted;
- avoid following a file that changes identity during the read where the
  platform supports a no-follow open; otherwise re-check containment and file
  metadata after reading;
- catch permission and operating-system errors and continue with the rest of
  the session; and
- return a reason code such as missing, empty, too_large, invalid_encoding,
  symlink, outside_workspace, not_regular, or unreadable.

Optional project guidance must not make the whole session unusable. A rejected
file is omitted, surfaced through redacted diagnostics, and does not grant
access to the agent. A future strict mode can turn selected reason codes into
startup failures; strict mode is not part of this PRD.

The loader must not execute markdown, import a Python module named AGENTS, use
the shell, make a network request, invoke an MCP server, launch a browser, or
read secrets while discovering instructions.

### 6.5 Prompt placement and authority

The effective system prompt is assembled in a deterministic order compatible
with the current AgentTurnRunner and PromptContract:

    universal agenthicc runtime and safety contract
      -> mode and capability enforcement contract
      -> bounded PROJECT INSTRUCTIONS block, if non-empty
      -> role/workflow stable contract
      -> stable MCP metadata/instructions, if present
      -> stable prompt/cache contract
      -> dynamic phase/tool/question context

The exact existing role/MCP ordering may be preserved if the implementation
maintains the following invariants:

1. project instructions are in the system prompt, never in a synthetic
   conversation message;
2. the instructions are clearly delimited and labelled as project guidance;
3. the block explicitly says it cannot authorize tools, change mode policy,
   bypass approval, weaken workspace boundaries, expose secrets, or override
   the system/runtime contract;
4. the same snapshot produces byte-for-byte identical rendered stable text;
5. document paths and source labels are stable and deterministic; and
6. dynamic phase state, artifacts, questions, tool results, and summaries do
   not enter the stable instruction block.

If the snapshot is empty, no content block is rendered. The absence identity
is stable and does not create prompt churn.

### 6.6 Refresh and cache semantics

The session manager loads an initial snapshot before the first provider call.
It checks for changes only at logical boundaries:

- before a new direct user turn;
- before a workflow phase turn;
- before retrying a failed provider turn;
- before restoring a workflow checkpoint for execution; and
- before starting a child turn that is not already covered by the parent
  turn's immutable snapshot.

It does not poll or reload during a streaming response, tool execution,
approval overlay, question overlay, or a single inner agent-turn loop.

A refresh compares the deterministic snapshot identity, not file mtime alone.
If the identity is unchanged, the existing PromptContract and cache epoch are
reused. If it changes, the next turn receives a new stable prompt fingerprint
and cache epoch with reason instruction_snapshot_changed. The current
conversation_id and session memory object remain unchanged.

This is an intentional cache tradeoff: changing project instructions should
invalidate the stable prompt prefix, but ordinary phase-state changes should
remain dynamic and should not invalidate it. The implementation must never
put a timestamp, refresh counter, current duration, question answer, artifact
summary, or mutable diagnostic in the stable instruction block.

### 6.7 Session, memory, and transcript boundaries

AGENTS.md content is prompt context, not conversation history:

    AGENTS.md files
      -> InstructionLoader
      -> session InstructionSnapshot
      -> AgentTurnContext
      -> stable PromptContract system prefix
      -> provider request

The provider conversation path remains:

    user input + assistant/tool exchanges
      -> one session conversation_id
      -> one session-scoped ShortTermMemory or ConversationStore contract
      -> journaled provider history
      -> provider request

The UI path remains:

    provider/tool events
      -> kernel/session event projection
      -> TUI ConversationStore

The instruction loader must never reconstruct provider history from the TUI
ConversationStore and must never append AGENTS.md as a user message. A resumed
workflow rehydrates its existing workflow context, session memory, journal
position, and stable conversation_id first; it then loads the current safe
instruction snapshot before the next provider call.

### 6.8 Checkpoint and resume provenance

Every workflow checkpoint created after this feature is enabled records a
bounded instruction reference:

    instruction_snapshot:
      schema_version
      snapshot_id
      stable_fingerprint
      ordered_documents:
        - display_path
          scope
          sha256
          byte_length
      skipped_reason_codes

The checkpoint never stores raw AGENTS.md content. It also never stores
absolute home paths, environment variables, secrets, browser/MCP clients, tool
instances, locks, provider messages, or object reprs.

On resume:

1. restore the existing workflow context and session conversation;
2. discover the current safe snapshot using the same workspace scope;
3. compare its identity with the checkpoint reference;
4. use the current snapshot for the next provider call; and
5. if it differs, add a bounded dynamic resume notice and advance the cache
   epoch without resetting the workflow phase or discarding valid memory.

If a previously present instruction file was deleted, the workflow resumes with
an instruction_removed notice. If it became unreadable, the workflow resumes
with the corresponding redacted reason. This preserves progress while making
the change explicit. A corrupted workflow checkpoint or conversation remains
subject to the existing fail-closed recovery rules in PRD-170 and PRD-173.

The same reference must be included in the generated-workflow provenance
contract taught by create_workflow. Downstream workflows inherit it from
WorkflowConfig and must not serialize the snapshot content.

### 6.9 Workflows and subagents

Built-in workflows and generated custom workflows receive the snapshot through
the existing session-scoped WorkflowConfig and AgentTurnContext path. A phase
does not call project_bootstrap.py, open AGENTS.md directly, or construct a
second memory/instruction manager.

The create_workflow authoring prompts must teach that generated workflows:

- accept the framework-provided instruction snapshot;
- pass WorkflowConfig and the stable session conversation_id through every
  phase and retry;
- use the normal run_phase or framework turn helper so system prompt and cache
  construction remain centralized;
- preserve instruction_snapshot metadata in checkpoint-safe context;
- treat project instructions as guidance rather than capability authority;
- do not copy AGENTS.md content into generated source, workflow artifacts,
  prompts, logs, manifests, or summaries; and
- continue to work with an empty or unavailable instruction snapshot.

Subagents created within the same session inherit the parent turn's immutable
snapshot and the same workspace policy. They do not reload between individual
subagent tool calls. A separately launched session has its own snapshot.

### 6.10 Interaction with modes and tools

AGENTS.md is not a policy override. The following remain authoritative:

- Safe, Plan, and Yolo mode capability filtering;
- workspace roots and mode-aware outside-workspace policy;
- tool approval and phase allowlists;
- browser backend and domain policy;
- MCP connection state and tool timeouts;
- cache contract and stable/dynamic prompt partitioning;
- checkpoint codecs, workflow ownership, and recovery classification; and
- provider/tool transaction integrity.

An instruction that says “always run this command,” “visit this URL,” “ignore
approval,” or “print the token” is merely model-visible text. The runtime still
filters or rejects the operation and never treats the instruction as an
authorization decision.

## 7. Required implementation changes

The implementation may choose different module names, but it must preserve the
following ownership boundaries.

### 7.1 Instruction service

Add a small, independently testable project-instruction module responsible for:

- candidate discovery from WorkspaceScope and the configured user-global root;
- safe bounded reads and normalized content;
- deterministic source ordering and snapshot hashing;
- immutable runtime and redacted checkpoint representations;
- stable prompt rendering;
- change comparison and refresh diagnostics; and
- no runtime dependency on the TUI or provider transport.

Do not put this logic in project_bootstrap.py. Bootstrap owns authoring and
atomic file writes; the instruction service owns safe runtime reads.

### 7.2 Session construction

Extend SessionContext with one optional/required session-owned instruction
manager or initial snapshot. Construct it alongside WorkspaceScope, session
memory, MCP, browser, and the stable session conversation. Load it before the
first direct or workflow provider call.

The manager must be safe for concurrent read access. Refresh is serialized and
returns a new snapshot. It must not hold a filesystem lock across an LLM call.

### 7.3 Turn and workflow plumbing

Extend WorkflowConfig and AgentTurnContext with a typed instruction snapshot or
manager reference, using the repository's existing TYPE_CHECKING and
dataclass conventions. Update the compatibility _run_agent_turn shim and all
workflow call sites.

AgentTurnRunner must consume the snapshot in the one canonical prompt-building
path. There must not be a second prompt assembly implementation in a workflow
or subagent.

The snapshot's stable fingerprint must contribute to PromptContract's stable
fingerprint and cache epoch. Dynamic phase context must remain dynamic.

### 7.4 Checkpoint codecs and recovery

Extend generic workflow checkpoint metadata and the create_workflow context
provenance with the redacted instruction reference. Preserve decoding of
older checkpoints that have no instruction field by treating them as an
unknown/empty historical snapshot and loading the current snapshot before
resume.

Checkpoint serialization must remain deterministic, versioned, and free of
raw instruction content. A decode failure follows existing workflow recovery
classification rather than silently starting a new run.

### 7.5 create_workflow authoring

Update the create_workflow design, generate, validate, and summary guidance to:

- name AGENTS.md as a framework-provided project instruction source;
- explain its stable prompt/cache behavior and authority limits;
- require generated workflows to use framework-provided instructions;
- forbid direct AGENTS.md reads and embedded copies;
- require checkpoint provenance and resume notices; and
- test empty, changed, removed, and unreadable instruction snapshots.

Extend generated-workflow validation and smoke checks to verify that:

- generated phase turns route through the framework turn helper;
- the workflow accepts WorkflowConfig/AgentTurnContext instruction context;
- instruction metadata is checkpoint-safe and content-free;
- the cache contract keeps instructions stable and phase state dynamic;
- a changed snapshot changes the stable fingerprint without resetting phase
  state or session conversation_id; and
- a missing/empty snapshot does not fail a valid generated workflow.

The authoring workflow itself must use the current session snapshot while
designing and validating a workflow, so its guidance is consistent with the
session in which it runs.

### 7.6 Configuration, diagnostics, and docs

Update the typed configuration model and commented config template. Add a
read-only diagnostic surface, preferably through the existing command/diagnostic
registry, that reports:

- enabled/include_global settings;
- effective directory and workspace scope;
- ordered source labels;
- byte and line counts;
- content hashes;
- skipped reason codes; and
- snapshot/cache fingerprints.

It must not print instruction contents by default. A user can use normal
filesystem inspection under the existing workspace policy if they explicitly
need to read the file.

Update the workflow guide, architecture guide, storage/checkpoint reference,
llms-full.txt, and relevant contributor guidance once the implementation is
complete. The guide must include the distinction between runtime instructions,
provider memory, TUI transcript, and tool authorization.

## 8. Data flow

### 8.1 New session and direct turn

    CLI/TUI session construction
      -> WorkspaceScope(root, effective_directory)
      -> InstructionManager.load()
           -> global AGENTS.md (optional)
           -> root-to-effective-directory candidates
           -> safe bounded reads
           -> ordered InstructionSnapshot
      -> SessionContext.instruction_manager
      -> user input
      -> refresh_if_boundary()
      -> AgentTurnContext(snapshot, session_memory, conversation_id, tools, policy)
      -> AgentTurnRunner._build_agent()
           -> universal contract
           -> rendered snapshot in stable system region
           -> role/workflow/cache contract
           -> dynamic phase context
      -> lauren-ai with the same session conversation_id and memory
      -> kernel events and TUI projection

### 8.2 Workflow phase

    WorkflowConfig
      -> shared session memory and conversation_id
      -> shared WorkspaceScope and instruction manager
      -> phase boundary refresh
      -> phase AgentTurnContext
      -> stable prompt fingerprint = base + role + tools + instruction snapshot
      -> dynamic prompt = phase state + artifacts + questions + transitions
      -> agent turn and tool calls
      -> typed workflow context/artifact
      -> checkpoint reference containing snapshot hashes only

### 8.3 Interrupt and resume

    active phase
      -> interrupt/cancellation preserves valid memory and workflow context
      -> checkpoint stores phase/context/journal position/conversation_id
         plus redacted instruction reference
      -> process restart or explicit resume
      -> acquire existing session/workflow owner
      -> rehydrate memory, journal, workflow context, and conversation_id
      -> reload current safe AGENTS.md snapshot
      -> compare checkpoint reference
      -> continue exact phase
      -> emit bounded changed/removed/unreadable notice when needed

### 8.4 Generated workflow

    create_workflow design/generate/validate
      -> receives current effective snapshot
      -> generates source that calls framework turn plumbing
      -> validator rejects direct AGENTS.md reads or raw-content serialization
      -> smoke test uses empty and non-empty fake snapshots
      -> publication includes instruction-contract evidence
      -> downstream session supplies its own current snapshot at runtime

## 9. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-1 | Discover global and workspace AGENTS.md candidates using the defined precedence and root boundary | P0 |
| FR-2 | Load only bounded regular UTF-8 files and return structured reason codes for skipped files | P0 |
| FR-3 | Produce deterministic immutable snapshots with stable content identity | P0 |
| FR-4 | Inject the snapshot into direct, workflow, retry, headless, background, resume, and eligible subagent turns | P0 |
| FR-5 | Render instructions in the stable system-prompt/cache region with explicit authority delimiters | P0 |
| FR-6 | Refresh only at logical boundaries and invalidate cache identity only on actual snapshot change | P0 |
| FR-7 | Preserve the same session conversation_id, session memory, journal, and workflow context | P0 |
| FR-8 | Persist checkpoint-safe snapshot provenance without raw contents or secrets | P0 |
| FR-9 | Rehydrate current instructions on resume and report changed/removed/unreadable sources without resetting valid progress | P0 |
| FR-10 | Keep mode, capability, workspace, approval, browser, MCP, tool-transaction, and checkpoint policies authoritative | P0 |
| FR-11 | Make create_workflow-generated workflows inherit the contract and reject unsafe duplicate readers | P0 |
| FR-12 | Preserve empty/missing instruction compatibility and older checkpoint decoding | P0 |
| FR-13 | Expose bounded redacted diagnostics for source order, hashes, skip reasons, and cache identity | P1 |
| FR-14 | Document the runtime, memory, transcript, cache, security, and generated-workflow contracts | P1 |

## 10. Non-functional requirements

### Security and privacy

- Instruction content is untrusted input. Delimit it and preserve runtime
  authority statements.
- Never log or checkpoint raw content.
- Never include secret-like environment values, authorization headers, provider
  messages, or callable representations in diagnostics.
- Enforce workspace containment and symlink rejection.
- Use bounded reads and deterministic truncation refusal; never silently
  truncate instructions because truncation can change their meaning.
- Do not make any network or external tool call during discovery or refresh.

### Determinism and caching

- Identical ordered content produces the same snapshot and prompt bytes across
  processes.
- Source ordering, normalized newlines, skip ordering, and fingerprints are
  deterministic.
- File mtime alone never invalidates the prompt cache.
- Phase artifacts, answers, elapsed time, and mutable workflow state never enter
  the stable instruction region.

### Performance

- Initial discovery is bounded by the configured file and byte limits.
- A boundary refresh must not perform a provider round trip.
- Unchanged snapshots should reuse the existing cache epoch.
- File reads must not occur in the inner agent-turn loop.
- Refresh and diagnostic operations must be cancellable at session shutdown.

### Reliability

- A bad optional instruction file does not crash an otherwise valid session.
- A refresh failure does not erase the previous valid in-memory snapshot during
  the active boundary; the next turn receives a reason-coded diagnostic.
- Resume never silently starts a workflow from its first phase because an
  instruction snapshot is unavailable.
- Checkpoint and prompt schema versions are explicit and forward-compatible
  where possible.

### Maintainability

- There is one loader, one snapshot model, and one prompt-rendering path.
- Workflow code does not duplicate file discovery.
- Public types have concrete annotations and documentation.
- Tests use temporary directories, fake snapshots, and fake provider/tool
  boundaries; no real provider or network is required.

## 11. Acceptance criteria

### Discovery and safety

1. A project with no AGENTS.md has the same effective prompt and no extra
   provider-history message.
2. Global, root, nested, and override files appear in documented broad-to-
   narrow order.
3. A nested directory cannot cause discovery above WorkspaceScope.
4. Symlinks, directories, oversized files, invalid UTF-8, and outside-root
   paths are rejected with deterministic reason codes.
5. The loader performs no imports, shell calls, network calls, browser calls,
   MCP calls, or secret reads.
6. Limits stop work before unbounded content enters memory.

### Prompt and cache

7. A direct turn's built agent includes a labelled project-instruction block
   when the snapshot is non-empty.
8. The same block is present for every built-in workflow phase and is absent
   only when the snapshot is empty.
9. The instruction block explicitly cannot authorize tools or override
   runtime policy.
10. Repeated turns with unchanged instructions reuse the stable fingerprint and
    do not create a new cache epoch.
11. Editing an applicable file changes the snapshot and stable fingerprint at
    the next boundary only; it does not change conversation_id or memory.
12. Editing a file while a tool or stream is active does not alter that active
    turn's snapshot.
13. Dynamic phase state and user question answers remain outside the stable
    instruction fingerprint.

### Memory, workflow, and resume

14. AGENTS.md content is absent from ConversationStore display messages,
    provider memory entries, tool history repair, and journaled conversation
    messages.
15. A workflow checkpoint contains only redacted instruction hashes and
    metadata.
16. A resumed workflow restores the same workflow phase, context, journal
    position, session memory, and conversation_id.
17. Changed, removed, and unreadable instruction files produce bounded resume
    notices without restarting the workflow.
18. An older checkpoint without instruction metadata remains loadable.
19. Subagents inherit the parent snapshot and cannot widen its workspace.

### create_workflow and compatibility

20. create_workflow prompts name and explain the framework instruction
    contract.
21. A generated workflow does not read AGENTS.md directly or serialize raw
    instruction content.
22. Generated workflow validation/smoke checks cover empty, non-empty, changed,
    removed, and unavailable snapshots.
23. Existing generated workflows that use the current WorkflowConfig and
    run_phase contracts continue to load; missing optional instruction fields
    default safely.
24. Existing project_bootstrap behavior, preservation rules, force behavior,
    and atomic writes remain unchanged.

### Observability and quality

25. Redacted diagnostics identify source order, byte counts, hashes, skip
    reasons, snapshot identity, and cache status without printing content.
26. Unit, integration, and E2E tests cover all P0 acceptance criteria.
27. Ruff, formatting, mypy, type-audit, and the relevant unit/integration/E2E
    suites pass, or an environment blocker is explicitly reported.

## 12. Test plan

### 12.1 Unit tests

Add focused tests for:

- candidate path ordering and override replacement;
- global inclusion and disabling;
- workspace-root and nested-directory containment;
- symlink, non-regular, unreadable, oversized, invalid-encoding, empty, and
  whitespace-only inputs;
- byte and file limits;
- newline normalization and deterministic hashes;
- stable snapshot equality and changed/removed source detection;
- redacted diagnostic and checkpoint serialization;
- prompt rendering, authority delimiters, empty snapshot behavior, and
  stable/dynamic separation;
- cache epoch reuse and invalidation reasons;
- configuration defaults and invalid limits; and
- refresh serialization and preservation of the previous valid snapshot on
  transient read failure.

### 12.2 Integration tests

Use temporary workspaces and fake session resources to verify:

- SessionContext constructs one instruction manager with WorkspaceScope;
- direct AgentTurnRunner calls receive the snapshot;
- code_plan, create_workflow, and at least one other workflow receive the same
  snapshot and conversation_id across all phases;
- headless and background paths use the same loader contract;
- MCP/browser availability and mode filtering remain unchanged when
  instructions contain conflicting text;
- checkpoint write/read preserves metadata but not content;
- resume reloads current instructions and keeps exact workflow context; and
- source changes at a phase boundary alter the stable prompt but not provider
  memory or workflow ownership.

### 12.3 End-to-end tests

With a deterministic fake transport:

1. start a session with nested AGENTS.md files and assert the first request's
   system prompt contains the ordered, labelled content;
2. submit multiple workflow phases and assert one snapshot identity is used
   throughout an unchanged session;
3. edit AGENTS.md between phases and assert one controlled cache identity
   change;
4. interrupt, persist, restart, and resume a workflow after changing the
   file; assert exact phase/context continuation and a bounded notice;
5. run create_workflow with a fake snapshot and assert generated validation
   reports instruction-contract evidence; and
6. submit malicious instructions that request a denied command, outside path,
   secret, browser destination, or MCP action and assert runtime policy still
   rejects the operation.

No E2E test may require a real provider, real browser, real MCP server, or
network access.

## 13. Rollout and migration

1. Add the typed loader and snapshot model behind a feature flag/defaulted
   configuration section.
2. Add redacted diagnostics and unit tests before changing prompt assembly.
3. Wire session and turn plumbing with an empty-snapshot compatibility path.
4. Enable the feature by default for new and resumed sessions.
5. Add workflow checkpoint metadata and generated-workflow validation.
6. Update docs and perform the full quality-gate matrix.

Rollback is the configuration setting enabled=false. Disabling runtime loading
must not delete AGENTS.md, rewrite checkpoints, or alter existing conversation
history. A later re-enable loads a fresh snapshot at the next logical
boundary.

## 14. Assumptions and decisions

- WorkspaceScope is the authoritative project boundary. The implementation
  must not invent a second project-root algorithm.
- AGENTS.override.md is the only same-directory override name in v1.
- Runtime AGENTS.md content is considered user/project data, not a trusted
  security policy.
- Content is intentionally not copied into workflow checkpoints. Hashes and
  bounded metadata are sufficient to detect changes while avoiding persistence
  of potentially sensitive guidance.
- When source files change or disappear, preserving valid workflow progress is
  more important than refusing to resume solely because guidance changed.
  The model receives an explicit bounded notice and the cache prefix changes.
- Existing lauren-ai memory and agenthicc journal contracts remain authoritative
  for provider history. Same conversation_id does not mean unbounded context;
  compaction and context-window safeguards still apply.
- The diagnostic surface reports hashes rather than contents by default because
  AGENTS.md can contain credentials, internal URLs, or proprietary guidance.
- Remote instructions and CLAUDE.md compatibility can be evaluated in a
  follow-up PRD after local AGENTS.md behavior is stable.

## 15. Definition of done

- FR-1 through FR-14 are implemented with no duplicate loader or prompt path.
- All P0 acceptance criteria are covered by deterministic tests.
- Existing bootstrap, workflow, memory, checkpoint, mode, workspace, browser,
  MCP, and tool-transaction behavior has no known regression.
- Relevant source, guides, checkpoint/storage references, and llms-full.txt
  document the new public types and behavior.
- Ruff, format, mypy, type-audit, and unit/integration/E2E checks pass, with
  blockers recorded if an environment dependency is unavailable.
- The implementation section is appended to this PRD with changed files,
  compatibility notes, test evidence, and any intentionally deferred items.
