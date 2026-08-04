---
title: "PRD-167: Workspace-Scoped @Mentions and Cross-Repository Target Consistency"
status: Proposed
version: 1.0.0
created: 2026-08-04
related_prds:
  - PRD-04
  - PRD-14
  - PRD-32
  - PRD-33
  - PRD-35
  - PRD-51
  - PRD-161
tags:
  - mentions
  - filesystem
  - workspace
  - security
  - multi-root
  - tui
---

# PRD-167 — Workspace-Scoped @Mentions and Cross-Repository Target Consistency

## 1. Executive summary

When a user asks agenthicc to work on a file outside the current project, the
`@`-mention pipeline and the filesystem-tool pipeline disagree about what the
agent is allowed to access. A request such as:

```text
Write an extensive README for @../agenthicc/README.md.
```

may cause the mention parser/injector to resolve and read the sibling
repository, while the `read_file` and `write_file` tools are restricted to the
current `play-rust` workspace. The agent is consequently given no single,
authoritative target. In the observed failure it issued:

```text
Read(play-rust/README.md)
```

instead of reading the requested `../agenthicc/README.md`.

This PRD introduces one canonical workspace-path resolver used by mention
completion, mention parsing, content injection, filesystem tools, tool
arguments, and transcript diagnostics. The default remains fail-closed: a
single-root session cannot access a sibling repository merely because a user
typed `..`. Explicitly configured additional roots may be used for legitimate
cross-repository work, with the same read/write/execute policy applied to every
root.

The fix must make an inaccessible target explicit to the user and the model;
it must never silently substitute a similarly named file in the current
workspace.

## 2. Evidence-backed problem statement

### 2.1 Reported reproduction

Assume this directory layout and a session started in `play-rust`:

```text
/workspaces/
├── play-rust/
│   └── README.md
└── agenthicc/
    └── README.md
```

Submit:

```text
Write an extensive README for @../agenthicc/README.md.
```

Expected behavior:

1. The exact requested path is resolved relative to the session workspace:
   `/workspaces/agenthicc/README.md`.
2. The user is told whether that path is permitted.
3. If permitted, the agent reads and writes the `agenthicc` README.
4. If not permitted, the run stops at a clear access error or asks for an
   explicit scope decision; it does not read `play-rust/README.md`.

Observed behavior:

```text
❯ Write an extensive README for @../agenthicc/README.md.
● assistant (agnes-2.0-flash)
  ⎿ Read(play-rust/README.md)
```

The displayed read target is not the target named by the user. The transcript
does not make clear whether the mention was injected, denied by a tool, or
replaced by an LLM-generated fallback. That ambiguity is itself part of the
bug and must be removed.

### 2.2 Current implementation evidence

| Concern | Current implementation | Defect |
|---|---|---|
| Mention path resolution | `src/agenthicc/mentions/parser.py:78-123` resolves `(cwd / path).resolve()` | Resolves `..` without checking the session's allowed roots. |
| Mention file injection | `src/agenthicc/mentions/injector.py:78-87` reads a `Path` directly; file resolution is consumed at `:294-336` | Bypasses `WorkspaceView` and can read a path that normal filesystem tools cannot read. |
| Mention completion | `src/agenthicc/tui/triggers/at_mention.py:62-68` searches `cwd / dir_part` | Offers paths outside the active workspace without an explicit scope. |
| Session mention CWD | `src/agenthicc/runners/agent_turn.py:764-773` passes `Path(os.getcwd())` | Uses process CWD rather than one shared, policy-aware workspace context. |
| Filesystem tool context | `src/agenthicc/tools/fs/agent_tools.py:52-97` builds `workspace_root` from `os.getcwd()` | The tools have no access to the mention's canonical resolution or configured multi-root view. |
| Filesystem security boundary | `src/agenthicc/tools/sandbox.py:19-47` rejects paths outside one `WorkspaceView` root | Correctly blocks sibling access, but is not shared by mention resolution. |
| Input completion root | `src/agenthicc/runners/tui_session.py:2221-2228` passes `Path(os.getcwd())` | Completion and execution do not receive an explicit root policy. |

### 2.3 Root cause

There are two incompatible path authorities:

```text
Mention path                        Filesystem tool path
─────────────                       ────────────────────
process CWD                         WorkspaceView(os.getcwd())
Path.resolve()                      os.path.realpath + root check
allows ../agenthicc                  rejects ../agenthicc
direct Path.read_text()              read_file returns permission_denied
```

The mention system classifies a sibling file as a valid `FILE` and may inject
its content. The agent tool system sees the same relative path as a workspace
escape. Since the injected `Mention` currently carries only `path`, `kind`, and
`resolved`, it does not carry an access decision or a stable workspace/root
identity that the prompt, tool layer, and transcript can share.

The observed `Read(play-rust/README.md)` is therefore a likely fallback caused
by target inconsistency: the requested sibling file is not available through
the tool contract, and the model chooses a local README. The implementation
must instrument and test this path so future diagnostics distinguish:

- exact requested target;
- canonical resolved target;
- policy decision;
- actual tool argument;
- actual file read/write result.

The PRD does not assume that the LLM itself rewrote the path without evidence;
the fix makes either source of substitution observable and prevents silent
substitution regardless of where it occurs.

## 3. Goals

1. Establish one canonical path-resolution and authorization service for the
   TUI, headless runner, workflows, mention parser/injector, and filesystem
   tools.
2. Preserve current single-workspace security by default. Typing `..` must not
   grant access to a parent or sibling repository.
3. Support deliberate cross-repository workflows through explicit additional
   allowed roots, without creating a second filesystem or permission system.
4. Ensure a permitted `@path` and a subsequent `read_file`/`write_file` call
   identify exactly the same canonical file.
5. Ensure an inaccessible or nonexistent mention produces a structured,
   user-visible diagnostic and never triggers a local-file fallback.
6. Make completion, injection, tool execution, transcript rendering, resume,
   and headless operation use the same workspace scope.
7. Preserve relative paths in user-facing output while retaining canonical
   identity and root provenance internally.
8. Keep symlink, absolute-path, `..`, glob, and multi-root behavior safe and
   deterministic.

## 4. Non-goals

This PRD does not:

- make all parent directories accessible by default;
- weaken `WorkspaceView`, `ToolSandbox`, capability gates, approvals, or mode
  restrictions;
- allow a mention to bypass write approval or Yolo/Safe policy;
- infer that a path outside the root is safe because it has a familiar name;
- add arbitrary remote filesystem access;
- make all discovered repositories part of a session automatically;
- redesign the `@` syntax or URL fetching;
- change the content budget, truncation, or mention cache policy except where
  root identity must be included in cache keys;
- silently rewrite `@../agenthicc/README.md` to `@README.md`;
- change the semantics of ordinary in-root paths.

## 5. Product decisions

### 5.1 Default access policy: one root, fail closed

Every new session has one canonical primary workspace root, normally the
resolved current project directory. All filesystem mentions and tools are
restricted to that root, including symlink targets. A path that resolves
outside it is classified as `out_of_scope`, not as a valid file.

For the reported request, the default result is:

```text
@../agenthicc/README.md
└─ Not available: path is outside the workspace root (play-rust)
```

The agent receives a structured failure such as:

```json
{
  "ok": false,
  "code": "outside_workspace",
  "requested_path": "../agenthicc/README.md",
  "workspace": "play-rust",
  "hint": "Add the target repository as an explicit allowed root or start agenthicc from that repository."
}
```

No content is read and no write tool is called for the rejected target.

### 5.2 Explicit multi-root access

Users who intentionally work across repositories may configure additional
roots. The implementation should reuse the existing security configuration
surface rather than introducing a parallel allow-list. The proposed shape is:

```toml
[security]
# The primary root is still derived from the project/session.
allowed_paths = [
  ".",
  "../agenthicc",
]
```

Relative entries are resolved relative to the project configuration file (or
the explicitly selected project root), canonicalized with symlink resolution,
and shown to the user before access. Absolute paths remain supported for
operator-controlled environments. The implementation must define and test:

- whether an allowed path must be a directory;
- duplicate and nested-root normalization;
- behavior when an allowed root does not exist at startup;
- whether a root may be added during a session;
- how project and user configuration are merged;
- how CLI overrides are represented without exposing secrets.

The recommended initial behavior is to reject nonexistent configured roots,
deduplicate nested roots, and require a session restart or explicit reload to
change scope. No additional root is enabled implicitly from an `@../...`
mention.

### 5.3 Exact-target rule

The original mention target is an identity, not merely a hint. Once the user
mentions `../agenthicc/README.md`:

- the prompt context must carry the exact requested display path and its
  canonical resolution;
- a tool may read only that resolved path or a path explicitly requested in a
  later user/agent action;
- a failed read must return the structured failure for that path;
- the runtime must never replace it with `README.md`, `play-rust/README.md`, or
  another same-named candidate;
- if the agent chooses a different file, the transcript must show that it made
  a new, explicit tool call for that different path.

## 6. Proposed architecture

### 6.1 Canonical workspace scope

Introduce a shared immutable scope object at session construction:

```python
@dataclass(frozen=True)
class WorkspaceScope:
    primary_root: Path
    allowed_roots: tuple[Path, ...]
    scope_id: str
```

The scope owns a resolver with one contract:

```python
class WorkspacePathResolver:
    def resolve(
        self,
        requested: str | Path,
        *,
        base: Path | None = None,
        operation: Literal["read", "write", "execute"] = "read",
    ) -> ResolvedWorkspacePath | PathResolutionError: ...
```

The exact class names may change during implementation, but all consumers must
use one implementation. It must:

1. normalize relative and absolute input;
2. resolve symlinks using the same realpath policy as `WorkspaceView`;
3. select the containing allowed root deterministically;
4. reject traversal and symlink escapes;
5. preserve the original requested string for diagnostics;
6. return a stable display path relative to the selected root;
7. distinguish `not_found`, `outside_workspace`, `invalid_path`, and
   `permission_denied`;
8. apply operation-specific policy before I/O;
9. expose no raw file content in diagnostics.

`WorkspaceView` and `ToolSandbox` should be adapted to delegate to or wrap this
resolver. They must not develop a second path-normalization algorithm.

### 6.2 Resolved path data

The resolved result should include at least:

```python
@dataclass(frozen=True)
class ResolvedWorkspacePath:
    requested: str
    absolute: Path
    root: Path
    root_id: str
    display: str
    operation: str
```

`Mention` should retain the user-facing token and carry the scope decision,
for example:

```python
raw="@../agenthicc/README.md"
path="../agenthicc/README.md"
kind=MentionKind.FILE
resolved=Path("/workspaces/agenthicc/README.md")
scope="allowed"
root_id="agenthicc"
```

For the default denied case, introduce `MentionKind.OUT_OF_SCOPE` or an
equivalent explicit status. Do not overload `UNRESOLVED`: nonexistent and
forbidden paths require different user guidance and different security
telemetry.

### 6.3 Unified data flow

```text
Session construction
  └─ WorkspaceScope(primary root + explicitly allowed roots)
       ├─ UnifiedInputSession / AtMentionTrigger
       │    └─ completion candidates only from allowed roots
       ├─ parse_mentions(text, scope)
       │    └─ Mention(requested, canonical path, scope status)
       ├─ build_context_prefix(text, scope)
       │    ├─ allowed → resolver-backed read + bounded injection
       │    └─ denied → structured warning, no direct Path I/O
       ├─ AgentTurnContext.workspace_scope
       │    └─ prompt states exact target and access result
       └─ Tool context / ToolSandbox
            └─ read_file, write_file, search, glob, run tools use same resolver

Transcript and journal
  └─ mention event records requested path, status, root id, and canonical
     display path; tool events record the actual argument and result.
```

### 6.4 Tool context propagation

`AgentTurnContext` must carry the session `WorkspaceScope`. The tool registry
and callable tools must receive a context containing the same scope rather than
reconstructing `workspace_root` from `os.getcwd()`.

The following surfaces must be updated together:

- `src/agenthicc/runners/session_context.py` — construct and retain scope;
- `src/agenthicc/runners/tui_session.py` and `headless.py` — pass scope into
  turns, workflows, and tools;
- `src/agenthicc/runners/agent_turn_context.py` and `agent_turn.py` — use scope
  for mention injection and prompt metadata;
- `src/agenthicc/mentions/parser.py` and `injector.py` — remove direct
  unscoped filesystem reads;
- `src/agenthicc/tui/triggers/at_mention.py` and input setup — filter
  completion candidates through scope;
- `src/agenthicc/tools/sandbox.py`, `tools/fs/agent_tools.py`, and the
  executor context adapter — use the canonical resolver;
- workflow and subagent construction — inherit the same scope, so a workflow
  cannot see a different filesystem than its parent turn;
- session replay and logs — preserve scope identity and diagnostics without
  persisting secrets.

### 6.5 Prompt and model contract

The agent's context must distinguish the user instruction from mention
resolution metadata:

```text
[MENTION TARGET]
requested: ../agenthicc/README.md
resolved: /workspaces/agenthicc/README.md
access: denied (outside_workspace)
instruction: Do not substitute another README. Ask the user to allow the
repository or start the session there before reading or writing it.
[/MENTION TARGET]
```

Absolute paths should be redacted or replaced with root-relative display paths
in normal TUI output when policy requires it. The model-facing context may use
the canonical path only when it is needed for an allowed tool call. A denied
mention must not include file contents.

The base system contract should explicitly say:

> A file mention is an exact target. If its access result is denied or
> unresolved, report that result and do not substitute a same-named local file.

## 7. User-facing behavior

### 7.1 In-root mention — unchanged

```text
@src/README.md
  └─ Read(src/README.md) ✓
```

The existing injection, caching, truncation, and `Explored` rendering behavior
remains unchanged for an allowed primary-root file.

### 7.2 Out-of-scope mention — safe failure

```text
@../agenthicc/README.md
  └─ ✗ Read(../agenthicc/README.md)
     outside workspace: agenthicc is not an allowed root
```

The agent must receive a failed result or clarification requirement. It must
not issue `Read(play-rust/README.md)` unless the user separately asks for that
file.

### 7.3 Explicitly allowed sibling repository

After configuring `../agenthicc` as an allowed root:

```text
@../agenthicc/README.md
  └─ Read(../agenthicc/README.md) ✓
```

The subsequent write must show the same target. The selected root should be
identifiable in expanded diagnostics, while compact output remains readable.

### 7.4 Completion behavior

- Default completion lists only primary-root entries.
- `..` and absolute paths do not reveal entries outside the scope.
- Allowed additional roots appear under an explicit root label, for example
  `agenthicc/README.md`, or preserve the requested relative form when that is
  unambiguous.
- A denied manually typed path produces a diagnostic rather than silently
  disappearing.
- Completion and manual input use the same resolver; selecting a completion
  must never create a path that later fails solely because completion used a
  different root policy.

## 8. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-1 | Every session constructs one canonical workspace scope. | P0 |
| FR-2 | Mention parser classifies nonexistent and out-of-scope paths separately. | P0 |
| FR-3 | Mention injection performs no filesystem I/O outside the scope. | P0 |
| FR-4 | All filesystem tools resolve through the same scope and resolver. | P0 |
| FR-5 | A denied mention never causes same-name fallback or automatic path rewriting. | P0 |
| FR-6 | The original requested path, canonical target, status, and root identity are available for diagnostics. | P0 |
| FR-7 | In-root behavior remains backward compatible. | P0 |
| FR-8 | Additional roots are opt-in, validated, canonicalized, and inherited by workflows/subagents. | P1 |
| FR-9 | `read_file`, `write_file`, `read_lines`, batch reads/writes, search, glob, and command `cwd` use the same root policy. | P1 |
| FR-10 | TUI and headless sessions produce equivalent path decisions. | P1 |
| FR-11 | Resume/replay preserves mention and scope diagnostics without re-reading denied paths. | P1 |
| FR-12 | Symlink targets are checked after realpath resolution for every root. | P0 |
| FR-13 | The agent prompt explicitly forbids substituting another file for a mention. | P0 |
| FR-14 | Multi-root scope changes require explicit configuration/reload and are shown to the user. | P1 |

## 9. Non-functional requirements

### NFR-1 — Security

The default must remain fail-closed. No parser, injector, completion provider,
plugin, workflow, subagent, or retry path may access a path that the canonical
resolver rejects. Symlink escapes, absolute paths, `..` traversal, and nested
roots must be tested.

### NFR-2 — Determinism

Given the same scope, path, and filesystem state, all clients must produce the
same canonical display path and status. Root selection must not depend on
registration order or dictionary ordering.

### NFR-3 — Observability

Diagnostics must make a wrong-target incident answer all of these questions:

1. What exact path did the user mention?
2. What canonical path was computed?
3. Which root was selected?
4. Was access allowed, denied, or unresolved?
5. What path did the tool actually receive?
6. What result did the tool return?

No file contents, API keys, or secrets may be added to diagnostic events.

### NFR-4 — Performance

In-root completion and mention resolution must remain within current latency
budgets. Scope canonicalization should happen once at session construction;
the resolver may cache root metadata but must not cache stale authorization
decisions across scope reloads.

### NFR-5 — Compatibility

Existing in-root relative paths, URL mentions, glob syntax, mention caching,
transcript rendering, workflows, and headless requests remain compatible.
Configuration without `allowed_paths` retains the current single-root behavior.

## 10. Test plan

### 10.1 Unit tests

Add resolver tests covering:

- relative, absolute, `.` and `..` paths;
- nonexistent paths;
- files and directories;
- symlinks inside the root and symlink escapes;
- Windows-style separators where supported;
- multiple allowed roots, nested roots, duplicates, and deterministic root
  selection;
- read versus write versus execute operation policy;
- invalid and empty path values.

Add mention tests covering:

- `@../agenthicc/README.md` classified as `OUT_OF_SCOPE` by default;
- the same path classified as an allowed file when the sibling root is
  explicitly configured;
- denied mentions produce no file read and no injected content;
- allowed mentions inject content through the resolver, not direct `Path`
  operations;
- mention cache keys include root identity/canonical path;
- glob expansion cannot leave allowed roots;
- completion never lists denied paths and manual denied input remains visible.

Add tool tests covering:

- exact target preservation from mention metadata to `read_file`;
- denied `read_file`/`write_file` structured error codes;
- allowed sibling read/write with the same canonical path;
- no fallback from `../agenthicc/README.md` to `README.md`;
- batch operations apply the decision independently to every path;
- plugin and subagent tool contexts inherit the scope.

### 10.2 Integration tests

Use temporary repositories:

```text
tmp/
├── play-rust/README.md       # decoy
└── agenthicc/README.md       # requested target
```

Cover:

1. Start from `play-rust`, submit the reported request, and assert the default
   decision is `outside_workspace`; assert no target content is injected and
   no tool receives `play-rust/README.md` as a substitute.
2. Configure `agenthicc` as an allowed root, submit the same request, and
   assert mention injection and `read_file` resolve the target identically.
3. Assert a subsequent `write_file` changes only `agenthicc/README.md`.
4. Run the same cases through TUI and headless session construction.
5. Reload/resume the session and assert the scope ID and mention diagnostics
   remain consistent.
6. Assert an allowed root disappearing between startup and use returns a
   structured error rather than escaping to a different path.

### 10.3 End-to-end tests

With a deterministic mock provider:

- issue the exact user message from the report;
- record the provider's requested tool arguments;
- verify the first and only file target is `../agenthicc/README.md` when
  allowed;
- verify the denied case ends with an actionable error and never emits a read
  for `play-rust/README.md`;
- verify the transcript chip, exploratory `Read` row, tool event, and final
  write row all identify the same target;
- verify a user can explicitly authorize/configure the sibling root and rerun
  without restarting the process if reload is part of the selected design;
- verify existing in-root README requests continue to work.

## 11. Acceptance criteria

The PRD is complete only when all of the following are true:

1. Reproducing the reported request from `play-rust` no longer produces
   `Read(play-rust/README.md)` as a substitute for the requested file.
2. In the default single-root session, `@../agenthicc/README.md` is denied
   before filesystem I/O and the user sees the reason.
3. The model receives an explicit instruction not to substitute a same-named
   local file after a denied mention.
4. Mention completion, mention injection, filesystem tools, workflows,
   subagents, TUI, and headless execution use one canonical scope/resolver.
5. An explicitly allowed sibling root permits reading and writing the exact
   requested `agenthicc/README.md`, subject to normal capability/approval
   rules.
6. A symlink or `..` escape cannot read or write outside all configured roots.
7. Transcript and durable events expose requested path, resolution status, and
   actual tool target without exposing file contents or secrets.
8. Existing in-root mention behavior passes its current test suite unchanged.
9. Unit, integration, and E2E tests cover both denied and explicitly allowed
   cross-root flows.
10. A full validation run passes:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

## 12. Implementation plan

### Phase 1 — Canonical resolver and scope model

- Extend `tools/sandbox.py` or add a dedicated workspace-scope module with the
  canonical resolver and structured errors.
- Adapt `WorkspaceView`/`ToolSandbox` to use it.
- Add session construction and configuration parsing for explicit roots.
- Define serialization-safe scope IDs and diagnostics.

### Phase 2 — Mention pipeline

- Pass scope into the parser and injector.
- Add explicit `OUT_OF_SCOPE` status.
- Replace direct `Path.read_*`, `iterdir`, and glob access with resolver-backed
  operations.
- Include scope metadata in `mention_chips` and mention cache keys.
- Filter `AtMentionTrigger` candidates using the same policy.

### Phase 3 — Tool and prompt pipeline

- Pass scope through `AgentTurnContext`, tool contexts, workflows, and
  subagents.
- Remove `_CTX = lambda: {"workspace_root": os.getcwd()}` as the authority for
  session filesystem access; retain a compatibility adapter only where needed.
- Return stable structured path errors from filesystem tools.
- Add the exact-target/no-substitution prompt contract.
- Record requested and actual targets in tool lifecycle events.

### Phase 4 — Client parity and diagnostics

- Wire TUI and headless session creation to the same scope builder.
- Ensure resume/replay displays the same root/status metadata.
- Add diagnostic output and `/config` or startup display for active roots,
  without printing secrets.

### Phase 5 — Verification and rollout

- Add the unit, integration, and E2E tests in §10.
- Run the existing filesystem, mention, workflow, subagent, and TUI suites.
- Roll out default single-root behavior first; enable explicit multi-root only
  after denial and symlink tests pass.
- Document migration for users who currently rely on unscoped `@../` injection.

## 13. Migration and compatibility

Users who only mention files inside the current project need no configuration
change. Existing `@` syntax remains valid. Users who intentionally work across
repositories must add each repository as an explicit allowed root or start the
session from the target repository.

Any existing session or cache record that lacks scope metadata is treated as
legacy primary-root data. It must not be used to authorize a new external
path. A cache hit is valid only when the canonical path and scope identity
match the current session.

## 14. Security and privacy

- Default deny for external roots and symlink escapes.
- No direct mention-injector I/O outside the resolver.
- No automatic trust of parent directories, Git worktrees, or sibling names.
- Root configuration is reviewed as a security-sensitive setting.
- Diagnostics use bounded, escaped path strings and never include file content,
  secrets, or provider credentials.
- Write and command operations retain their existing capability and approval
  gates after path authorization.
- Multi-root access is inherited by workflows/subagents but cannot widen the
  scope from within a tool call or generated workflow.

## 15. Open implementation decisions

The implementation should resolve these before coding is finalized:

1. Should `allowed_paths` be interpreted relative to the configuration file,
   the project root, or the process CWD? Recommendation: project root with an
   explicit documented rule.
2. Should an allowed sibling root display as `../agenthicc/README.md` or
   `agenthicc/README.md` in compact TUI output? Recommendation: preserve the
   user's requested form and show root identity in expanded diagnostics.
3. Should root changes support a live reload, or require restart? Recommendation:
   require restart initially unless a complete scope invalidation path exists.
4. Should the user be able to approve one external path for the current session
   from a question overlay? Recommendation: defer until the static
   configuration path is complete; do not add an ad-hoc bypass.
5. Should command `cwd` accept a path in any allowed root? Recommendation: yes,
   but resolve it through the same operation-aware resolver and retain existing
   command approval/lifecycle policy.

## 16. Verification evidence to record

When implementation starts, append concrete evidence here:

- resolver source and test locations;
- configuration examples and parsed output;
- denied and allowed integration test names;
- E2E transcript/tool-argument assertions;
- full test and static-check results;
- any deviations from the open decisions above.

