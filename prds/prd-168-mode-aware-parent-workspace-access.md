---
title: "PRD-168: Mode-Aware Parent-Workspace Access"
status: Implemented
version: 1.1.0
created: 2026-08-04
related_prds:
  - PRD-04
  - PRD-78
  - PRD-79
  - PRD-91
  - PRD-155
  - PRD-167
tags:
  - security
  - workspace
  - filesystem
  - safe-mode
  - yolo-mode
  - approval
  - tui
---

# PRD-168 — Mode-Aware Parent-Workspace Access

## 1. Executive summary

agenthicc currently has two independent decisions for a filesystem request:

1. the runtime mode decides whether a tool capability is free, approval-gated,
   or hard-blocked; and
2. `WorkspaceView`/`ToolSandbox` decide whether the resolved path is inside the
   current workspace root.

The second decision occurs inside filesystem adapters. Consequently, a request
such as `../statement/report.md` can be rejected as a path escape without the
user receiving the Safe-mode approval opportunity they expect. It also means
that mention injection, filesystem tools, completion, command working
directories, and workflows can make different decisions about the same path.

This PRD explores and specifies a mode-aware implementation that preserves one
canonical workspace resolver while changing its policy by runtime mode:

| Request | Safe | Yolo |
|---|---|---|
| Path resolves inside the primary/allowed workspace | Continue using existing capability and approval policy | Continue without a workspace-boundary prompt |
| Path resolves above or outside the workspace | Seek explicit path-access approval before any I/O | Allow the path through the agenthicc workspace boundary without an approval prompt |
| Path is blocked by the operating system, container, or process permissions | Fail with a structured OS/access error | Fail with the same structured OS/access error |

“Without restrictions” in Yolo means without agenthicc's workspace-root
restriction. It does not grant operating-system privileges, escape a container,
follow a broken path, or turn an unavailable file into an available one.

The implementation must make the decision before content is read, written,
listed, searched, executed, or used as a command working directory. Safe-mode
approval must be a path-scope request, not an accidental side effect of a
later `PermissionError`. Yolo must use a deliberately explicit unrestricted
workspace policy, not a missing or `None` sandbox that could be interpreted
differently by different tools.

This PRD extends PRD-167's canonical resolver and revises its default
out-of-scope policy. PRD-167's resolver, exact-target, symlink, diagnostics,
and cross-surface consistency requirements remain authoritative; its
fail-closed default is replaced by the Safe/Yolo policy defined here.

## 2. Evidence-backed problem statement

### 2.1 Reported requirement

Assume the session starts in `/workspaces/play-rust`:

```text
/workspaces/
├── play-rust/
│   ├── README.md
│   └── src/
└── statements/
    └── report.md
```

The user or agent requests:

```text
Read ../statements/report.md
```

The desired behavior is:

- in **Safe**, resolve the exact target, show an approval request describing
  the outside-workspace read, and perform no read until the user approves;
- in **Safe**, denial, cancellation, timeout, or a missing approval service
  must prevent all I/O and return a structured denial;
- in **Yolo**, resolve and read the exact target without an agenthicc
  workspace-boundary prompt;
- in both modes, a symlink or path race must not cause a different target to
  be silently substituted;
- the transcript must identify the requested path, canonical target, mode,
  decision, and actual result.

The same policy applies to `@../statements/report.md`, `write_file`,
`list_directory`, `search_files`, `grep_files`, globbing, batch operations,
and a command whose `cwd` is outside the primary workspace.

### 2.2 Current implementation evidence

| Concern | Current implementation | Gap addressed by this PRD |
|---|---|---|
| Runtime mode | `src/agenthicc/tui/runtime/mode_manager.py` defines Safe, Plan, and Yolo. Safe puts side-effecting capabilities in `approval_required`; Yolo has no capability restriction. | Workspace scope is not a first-class mode decision. |
| Capability approval | `src/agenthicc/tools/approval.py` asks through `ApprovalService` based on tool capabilities. | The request contains no canonical path, operation, or outside-root reason. |
| Workspace boundary | `src/agenthicc/tools/sandbox.py:WorkspaceView` resolves real paths and raises `PermissionError` outside its root. | The rejection occurs inside the adapter and cannot ask Safe approval first. |
| Filesystem backend | `src/agenthicc/tools/fs/linux.py` owns a `WorkspaceView` rooted at the current directory. | It cannot select a Safe-approved outside target or an explicit Yolo unrestricted view. |
| Callable filesystem tools | `src/agenthicc/tools/fs/agent_tools.py` builds context from `os.getcwd()`. | Tool context can disagree with mentions and configured session scope. |
| Mention injection | `src/agenthicc/mentions/parser.py`/`injector.py` currently resolve and read paths separately from `WorkspaceView`. | A mention can read a target under a different policy before tool approval. |
| Command execution | Command tools use a workspace/sandbox context but do not share one mode-aware path decision with filesystem tools. | An outside `cwd` can bypass or fail differently from a file access. |
| Headless operation | `src/agenthicc/runners/headless.py` uses a fail-closed approval adapter unless explicitly enabled. | Outside-root requests need deterministic Safe denial or explicit automation policy, without hanging. |

### 2.3 Root cause

The current dataflow has no shared policy checkpoint:

```text
tool call
  └─ capability hook
       └─ filesystem function
            └─ WorkspaceView.resolve()
                 └─ PermissionError outside root
```

By the time `WorkspaceView.resolve()` sees the escape, the approval service has
not received a request containing the path. Adding a special case to one
filesystem function would leave mentions, batch tools, search, commands,
plugins, workflows, and subagents inconsistent.

The solution must introduce one mode-aware access decision before each actual
filesystem or command I/O, while retaining a final resolver check immediately
before the operation. The first check is for policy and approval; the final
check is for canonical-target and TOCTOU safety.

## 3. Goals

1. Make Safe, Plan, and Yolo workspace-boundary behavior explicit and
   testable.
2. In Safe, seek approval before any access to a canonical target outside the
   session's configured workspace scope.
3. In Yolo, remove only agenthicc's workspace-root restriction for explicit
   agent/user paths, without fabricating OS privileges or bypassing unrelated
   tool, network, resource, or provider failures.
4. Reuse the canonical resolver and `WorkspaceScope` proposed by PRD-167 for
   mentions, completion, injection, tools, commands, workflows, subagents,
   replay, and diagnostics.
5. Ensure Safe path approval and ordinary capability approval can be composed
   into one understandable approval interaction rather than two prompts for
   the same tool call.
6. Ensure no path content or metadata is read before Safe approval when the
   target is outside the workspace.
7. Preserve exact-target semantics: `../x/file` may never silently become a
   same-named file in the current workspace.
8. Make TUI and headless decisions deterministic and prevent an unattended
   headless run from waiting forever for a prompt.
9. Preserve current in-root behavior and Plan's read-only safety contract.
10. Make mode changes take effect at the next access decision, including
    accesses initiated during a workflow phase or subagent turn.

## 4. Non-goals

This PRD does not:

- grant root, administrator, container, SELinux/AppArmor, or operating-system
  privileges;
- make remote URLs, network shares, or arbitrary network filesystems local
  filesystem targets;
- bypass `NetworkGuard`, resource limits, provider policy, or Plan's hard
  capability restrictions;
- infer permission from a path's basename, repository name, symlink location,
  or the model's claim that access is safe;
- add a second allow-list separate from `security.allowed_paths`;
- silently add a parent or sibling repository to the configured workspace;
- use Yolo as a reason to remove canonicalization, audit events, output bounds,
  or path-type validation;
- make completion enumerate every filesystem location merely because Yolo is
  active;
- change the existing ordinary Safe approval meaning for in-root writes,
  execution, network calls, or undeclared tools;
- provide a UI prompt from a parser/completion keystroke that has not yet
  attempted an access;
- change the user-visible mode catalogue beyond the Safe/Plan/Yolo semantics
  required here.

## 5. Product decisions

### 5.1 One canonical workspace policy

The implementation must build one immutable `WorkspaceScope` at session
construction as described in PRD-167. It contains the primary root, explicit
additional roots, a stable scope identity, and the canonical resolver.

The resolver must return a structured result rather than making callers infer
policy from an exception:

```python
@dataclass(frozen=True)
class ResolvedWorkspacePath:
    requested: str
    absolute: Path
    root: Path | None
    root_id: str | None
    display: str
    operation: str
    exists: bool
    scope: Literal["in_scope", "outside_scope"]
```

The exact names may change, but the result must preserve the original request,
canonical path, operation, existence state, and scope classification. For a
nonexistent write target, canonicalize the target and its existing parent
before deciding whether it is outside the scope. For symlinks, resolve the
actual target before access and revalidate at the I/O boundary.

`WorkspaceScope` must not silently change when Safe approval is granted. An
approval is a decision for a specific canonical target/operation or an
explicitly scoped temporary grant; it is not a mutation of the configured
workspace roots.

### 5.2 Mode policy matrix

The policy is evaluated using `AppState.active_mode()` at the moment of the
access, not only at turn start.

| Mode | In-scope path | Outside-scope path | Explanation |
|---|---|---|---|
| Safe | Continue to capability gate/ordinary approval policy. | Build a `WorkspaceAccessRequest`; await explicit approval before I/O. | Safe supervises access escalation rather than treating the path as an invisible backend error. |
| Plan | Preserve read-only access only; outside-scope access is denied or hard-blocked without reading. | No approval overlay for an access that Plan cannot execute. | Plan remains a planning/read-only boundary. |
| Yolo | Continue without a workspace-root prompt. | Use an explicit unrestricted workspace policy and perform the exact operation, subject to OS/runtime limits. | Yolo is the intentional workspace-boundary bypass. |

For Safe, an outside-scope write may have two reasons to require approval:
the path is outside the workspace and the operation has a side-effecting
capability. The user must see both reasons in one request. Approval of the
path must not automatically approve unrelated future writes, commands, or
network operations.

For Plan, the PRD intentionally does not convert an outside read into a
prompt. Plan is the mode in which the user asked the agent to plan without
performing the action. The model receives a structured `outside_workspace`
denial and can ask the user to switch mode or configure a root.

### 5.3 Safe approval semantics

Safe approval is required when all of the following are true:

1. the operation would perform or initiate filesystem/command I/O;
2. canonical resolution classifies the target outside every configured root;
3. the active mode is Safe; and
4. an explicit scoped grant does not already cover the exact operation.

The request must be published before reading file contents, directory entries,
file metadata beyond what is necessary for canonicalization, search results,
or command output. The approval overlay must show:

- tool or source (`read_file`, `@mention`, `run`, `search_files`, etc.);
- operation (`read`, `write`, `list`, `search`, `execute_cwd`, or equivalent);
- original requested path;
- canonical target or a safe display form;
- primary workspace root and the reason the target is outside it;
- whether the target is an existing file/directory or a new path whose parent
  is outside the scope;
- any additional capability approval required by the operation.

The default action is **Allow once** or **Deny**. If the existing overlay keeps
its remember options, they must become explicitly scope-aware:

- “allow this target once” grants only the current canonical target and
  operation;
- “allow this root for this turn” grants only that canonical outside root for
  the current turn and the operations named in the request;
- “allow this root for this session” is optional and must be a separate,
  clearly labelled choice;
- capability remembers (`WRITE this turn`, for example) must never imply
  outside-workspace access;
- a grant must not survive a session restart unless a future product decision
  explicitly adds durable consent storage.

If a user denies, presses Escape, cancels the run, the approval service is
missing, or the request times out, the operation must not execute. The model
gets a stable structured result with `outside_workspace` and
`approval_denied`/`approval_unavailable` details as appropriate.

The existing `ApprovalService`/`ApprovalOverlay` should be extended rather
than creating a second modal coordinator. `ApprovalRequest` should gain a
structured scope reason, and `ApprovalResponse` should carry a scope grant
separately from capability remember flags. `ApprovalService` must retain its
single pending request and cancellation cleanup behavior.

### 5.4 Yolo unrestricted workspace policy

Yolo must select an explicit policy object or view such as
`WorkspaceAccessMode.UNRESTRICTED`, `UnrestrictedWorkspaceView`, or an
equivalent named implementation. It must not rely on `ToolSandbox()` with no
paths, a missing context key, or an `except PermissionError` fallback.

In Yolo:

- relative `..` paths are resolved against the session's declared base
  directory;
- absolute paths are accepted by the workspace policy;
- symlinks are resolved to the actual target for diagnostics and race checks;
- the exact target is passed to the tool;
- no workspace-boundary approval request is created;
- OS permissions, missing paths, invalid paths, resource limits, and tool
  capability failures still return structured errors;
- the user-visible transcript records that Yolo bypassed the agenthicc
  workspace boundary, without exposing file contents in a policy event.

Yolo is not an implicit change to the process current directory. Tools must
receive the resolved path/context explicitly so concurrent workflows and
subagents cannot race through a shared `os.chdir()`.

### 5.5 Explicit configuration remains meaningful

`security.allowed_paths` remains the source of configured Safe roots. A path
inside an explicitly configured root is not an outside-scope access and does
not require the new scope approval solely because it is not under the primary
root. Configuration merging, canonicalization, nested roots, and reload rules
remain those defined by PRD-167.

Yolo does not mutate `security.allowed_paths`; it selects its mode policy for
the current session. Switching back to Safe immediately re-enables the
configured-root boundary.

### 5.6 Dangerous-skip-permissions compatibility decision

`--dangerously-skip-permissions` may continue to auto-approve ordinary
capability prompts according to PRD-79, but it must not silently turn Safe into
Yolo or bypass the workspace-boundary policy. A Safe outside-root request must
still be either explicitly approved through the scope policy or denied. The
user can intentionally select Yolo when they want the workspace boundary
removed.

This distinction must be documented in CLI help and tested so a flag intended
for unattended capability approval cannot accidentally become a process-wide
filesystem escape.

## 6. Proposed architecture and dataflow

### 6.1 Access decision model

Introduce a shared policy contract next to the PRD-167 resolver. Suggested
types are illustrative:

```python
class WorkspaceAccessMode(StrEnum):
    SCOPED = "scoped"          # Safe/Plan resolver behavior
    UNRESTRICTED = "unrestricted"  # Yolo workspace-boundary behavior


@dataclass(frozen=True)
class WorkspaceAccessRequest:
    requested: str
    canonical: Path
    root: Path | None
    operation: str
    tool_name: str
    reason: str                 # e.g. "outside_workspace"
    capabilities: frozenset[str]
    mode: str


@dataclass(frozen=True)
class WorkspaceAccessDecision:
    allowed: bool
    status: str                  # allowed, approval_required, denied, blocked
    canonical: Path
    display: str
    reason: str = ""
    grant: str | None = None
```

The implementation may use different names, but it must separate:

- path canonicalization;
- scope classification;
- runtime-mode policy;
- human approval;
- the final I/O operation.

No tool should decide Safe versus Yolo by inspecting a raw mode name in its
own function. No UI should decide whether a path is inside the workspace by
string-prefix comparison.

### 6.2 Access pipeline

Every access-capable surface must follow this flow:

```text
User text / model tool call / workflow phase
  │
  ├─ requested path, operation, tool name, capability metadata
  │
  ▼
Session WorkspaceScope
  │  canonicalize relative/absolute path
  │  resolve symlink/parent and classify scope
  │
  ▼
WorkspaceAccessPolicy(active_mode)
  ├─ Safe + in scope
  │    └─ return allowed; continue ordinary capability policy
  ├─ Safe + outside scope
  │    └─ publish WorkspaceAccessRequest
  │         ├─ approved → return scoped grant
  │         └─ denied/cancelled/timeout → structured denial
  ├─ Plan + outside scope or forbidden operation
  │    └─ hard denial; no I/O and no prompt
  └─ Yolo + outside scope
       └─ return allowed through explicit unrestricted policy
  │
  ▼
Final canonical revalidation
  │  verify target identity/scope has not changed
  │
  ▼
Filesystem/command I/O
  │
  ▼
Tool result + journal/transcript event
```

The final revalidation is mandatory even after approval. A user approving
`../statements/report.md` must not authorize a symlink that is changed to
`/etc/passwd` before the read. If the canonical target changes, fail closed in
Safe and return a target-changed error in Yolo rather than silently following
the changed target.

### 6.3 Mention dataflow

Mentions are an access surface, not merely prompt formatting:

```text
@../statements/report.md
  │
  ├─ parser records raw token and requested path
  ├─ WorkspaceScope resolves canonical target and scope
  ├─ Safe outside scope → ApprovalService request before read_text()
  ├─ Plan outside scope → explicit mention denial, no content
  ├─ Yolo outside scope → unrestricted resolver-backed read
  └─ prompt/injection records exact target and decision
```

Mention completion may display candidates, but completion alone must not ask
for approval or read file contents. Approval begins when injection or another
operation actually accesses the target. `injector.py` must no longer call
`Path.read_text()` without the shared access policy.

A denied mention must produce an exact-target diagnostic and must not be
removed in a way that encourages the model to select a local same-named file.

### 6.4 Tool and command dataflow

Filesystem tools must receive the session scope and access policy through the
tool context. The context must distinguish at least:

```text
workspace_scope       canonical roots and scope id
workspace_access      mode-aware access policy
workspace_root        primary root for display/backward compatibility
active_mode           current RuntimeMode identity
approval_service      shared session coordinator (when interactive)
```

The following argument forms require preflight and final validation:

| Surface | Arguments to validate |
|---|---|
| `read_file`, `read_lines`, `get_file_info`, `file_exists` | target path |
| `write_file`, `append_file`, `patch_file`, `truncate_file`, `touch_file` | target path and existing/new parent |
| copy/move | source and destination independently |
| delete/batch operations | every target independently; no partial implicit approval |
| list/search/grep/glob | root path and every discovered target before reading metadata/content |
| `@mention` injection | exact mentioned target |
| command/run tools | executable policy plus `cwd`, declared input/output paths, and any shell path arguments according to the command contract |
| workflow/subagent/plugin tools | all delegated filesystem arguments and inherited scope |

Batch operations must not turn one approved target into blanket approval for
the rest of the batch. The implementation may request one review containing a
bounded list of canonical targets, but every target must be checked and the
user's decision must be unambiguous.

### 6.5 Hook and adapter ordering

The existing global hook order remains important:

1. capability hard-block (`ToolCapabilityGate`);
2. combined scope/capability approval (`ApprovalGate` or its extension);
3. tool-specific preflight and final resolver validation;
4. actual tool function/backend I/O.

Because a generic hook cannot reliably discover path fields in arbitrary plugin
tools, the canonical access policy must also be called from every built-in
filesystem/command adapter. Plugin tools that declare filesystem access must
use the documented context API; undeclared tools remain capability-gated and
must not receive a hidden unrestricted path helper.

For built-in tools, the preferred implementation is to make the backend
request a policy decision through the context before opening a file. A generic
preflight hook may supplement this for declared path metadata, but it cannot be
the only enforcement point.

### 6.6 Workflow, subagent, and resume inheritance

`SessionContext` owns the `WorkspaceScope`, active access policy source, and
approval service. Workflow runners, phase turns, subagents, retries, and
resume/replay receive the same session-owned scope and policy adapter.

They must not:

- reconstruct a root from `os.getcwd()`;
- create a new approval service that cannot reach the TUI overlay;
- capture Safe/Yolo only once and ignore a live mode switch;
- persist a temporary approval as a durable workspace permission;
- use a different resolver for generated workflows.

The `create_workflow` workflow and generated workflows inherit this behavior
through the normal workflow/tool context. A custom workflow does not need to
reimplement path approval, and it cannot opt into Yolo workspace access merely
by omitting a path annotation.

### 6.7 Event, journal, and transcript data

Add structured events or extend existing tool events so a diagnostic can
answer:

1. What path was requested?
2. What operation was attempted?
3. What canonical path was calculated?
4. Which workspace root, if any, contained it?
5. Which mode was active?
6. Was approval required, approved, denied, or bypassed by Yolo?
7. What target was actually opened or executed?
8. What result/error was returned?

Events must not include file contents, command secrets, API keys, or full
unredacted environment values. Compact TUI rendering can show the requested
relative path and status; expanded diagnostics may show canonical paths when
the existing path-display policy permits it.

## 7. User-facing behavior

### 7.1 Safe mode: outside read approval

```text
● Read(../statements/report.md)
└─ Waiting for approval
   Outside the workspace: /workspaces/statements/report.md
   Operation: read
   [Allow once]  [Allow root this turn]  [Deny]
```

While this is displayed, the target has not been read. After approval, the
normal tool event and result are emitted using the same target:

```text
● Read(../statements/report.md)
└─ Completed
```

After denial:

```text
● Read(../statements/report.md)
└─ Denied: outside_workspace approval was not granted
```

The model receives a structured error and must not be encouraged to retry the
same request indefinitely or substitute `play-rust/README.md`.

### 7.2 Safe mode: outside write

The approval surface must identify both scope escalation and side effects:

```text
⚠ Tool Approval Required
  write_file
  target: ../statements/report.md
  operation: write
  reasons: outside workspace, filesystem write
```

One approval response must be sufficient to authorize this exact operation.
If the user allows only the target once, a later write to another outside file
must ask again.

### 7.3 Yolo mode: outside access

```text
⏵⏵ Yolo
● Read(../statements/report.md)
└─ Completed
```

No workspace-boundary overlay is shown. The transcript may include a compact
policy marker such as `workspace: unrestricted` in expanded tool diagnostics,
but ordinary output remains readable.

If the OS denies the operation, the result must still be a normal structured
failure:

```text
└─ Failed: operating system denied access
```

Yolo must not report success merely because it skipped `WorkspaceView`.

### 7.4 Plan mode: outside access

Plan remains non-mutating and does not prompt to authorize an operation it is
not allowed to perform:

```text
└─ Blocked: ../statements/report.md is outside the configured workspace
   Switch to Safe and approve the access, or switch to Yolo intentionally.
```

No file content is read.

### 7.5 Mode changes during a run

The access policy is evaluated immediately before each operation:

| Situation | Required result |
|---|---|
| Safe request is waiting; user switches to Yolo | The pending request is re-evaluated or cancelled safely; the access may proceed only after the policy observes Yolo, with no stale approval required. |
| Yolo request is queued; user switches to Safe before I/O | Safe approval is required before the operation. |
| Safe scope grant exists; user switches to Plan | Plan's hard restrictions win; no access occurs. |
| Workflow changes phase while a request is pending | The same session scope and current mode are used; no phase-local bypass is permitted. |

The implementation must define cancellation behavior so a mode switch cannot
leave an orphaned overlay, an already-authorized stale request, or an
unresumed agent task.

## 8. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-1 | Every session owns one canonical `WorkspaceScope` and resolver. | P0 |
| FR-2 | The resolver classifies relative `..`, absolute, symlink, existing, and new-parent paths before I/O. | P0 |
| FR-3 | Safe outside-workspace access creates a scope-aware approval request before content, directory, metadata, or command I/O. | P0 |
| FR-4 | Safe approval requests identify the exact requested path, canonical target, operation, mode, and outside-scope reason. | P0 |
| FR-5 | Safe denial, cancellation, timeout, missing approval service, and approval failure prevent the operation and return stable structured errors. | P0 |
| FR-6 | Yolo uses an explicit unrestricted workspace policy for outside-root paths without an agenthicc approval prompt. | P0 |
| FR-7 | Yolo does not bypass OS/process/container permissions, path validity, resource limits, or unrelated network/capability restrictions. | P0 |
| FR-8 | Plan remains hard-blocked/read-only for outside-scope operations and performs no access before reporting the denial. | P0 |
| FR-9 | In-scope Safe behavior preserves existing capability approval semantics. | P0 |
| FR-10 | Scope and capability reasons compose into one approval interaction where both apply. | P0 |
| FR-11 | Capability remember flags cannot implicitly grant outside-workspace access. | P0 |
| FR-12 | Mentions use the same policy for injection and never perform direct unapproved reads. | P0 |
| FR-13 | Filesystem tools, search, glob, batch, command `cwd`, workflows, subagents, and plugins use the same scope/policy context. | P0 |
| FR-14 | Source and destination paths in copy/move and every batch member are checked independently. | P0 |
| FR-15 | Final canonical revalidation prevents symlink and TOCTOU target substitution after approval. | P0 |
| FR-16 | Mode changes affect the next access decision, including within a streaming turn or workflow. | P1 |
| FR-17 | Temporary Safe scope grants are explicit, bounded, session-memory only, and distinguishable from capability grants. | P1 |
| FR-18 | Headless runs fail closed without an interactive approval channel and never hang; an explicit automation policy may supply deterministic approval/denial. | P0 |
| FR-19 | Resume/replay preserves access decisions and diagnostics without replaying an unapproved I/O operation. | P1 |
| FR-20 | Tool and policy events preserve requested path, canonical target, mode, decision, actual target, and result without secrets or file contents. | P1 |
| FR-21 | Generated workflows inherit the session policy automatically and cannot bypass it by using a custom runner. | P1 |
| FR-22 | Configuration and CLI documentation explain that Yolo bypasses the agenthicc workspace boundary while Safe requires approval for outside targets. | P1 |

## 9. Non-functional requirements

### NFR-1 — Security

Safe is fail-closed until an explicit scope decision is made. No parser,
injector, completion helper, backend, plugin, workflow, retry, or subagent may
read or write an outside target by bypassing the shared policy. Approval is
bound to canonical target identity and operation. Symlink changes, parent
changes, and path races must be handled at the final I/O boundary.

Yolo's bypass must be explicit in code and telemetry. It must not be produced
by a missing allow-list, an exception fallback, or an accidental context key.

### NFR-2 — Exact-target integrity

Given the same session scope, mode, request, and filesystem state, every
surface computes the same canonical target. A denied or failed target is never
rewritten to a local same-named file.

### NFR-3 — Determinism

Approval requests, headless decisions, batch decisions, mode changes, and
resume behavior must be deterministic. A denied request must not depend on
whether a TUI repaint, retry, or workflow phase transition occurred first.

### NFR-4 — Responsiveness

Approval waits suspend the agent coroutine without blocking the event loop.
Mode changes and denial/cancellation must release the pending overlay and
resume or terminate the waiting task without leaked tasks or duplicated I/O.

### NFR-5 — Compatibility

Existing in-root relative reads, writes, searches, mentions, workflows, and
Yolo behavior remain compatible except for the explicit requirement that Yolo
outside-root access now uses a named, shared policy. Safe users gain an
approval path instead of an opaque workspace escape error.

### NFR-6 — Performance

In-root operations must not wait on the approval service or perform a second
full directory traversal. Canonicalization and scope metadata may be cached
per session, but authorization decisions must be invalidated when mode or
scope changes. Final revalidation must remain bounded.

### NFR-7 — Observability and privacy

Policy decisions and errors are journalable and replayable without storing
file contents, credentials, command arguments containing secrets, or full
environment dumps. TUI rendering must clearly distinguish approval pending,
denied, blocked, OS failure, and successful Yolo bypass.

### NFR-8 — Maintainability

There must be one resolver, one session approval coordinator, and one mode
policy contract. Built-in tools must use documented adapters. New workflows and
plugins must inherit the policy through context rather than copying security
logic.

## 10. Test plan

Tests must be isolated in temporary directory trees and must never use real
home directories, repository parents, API keys, or production paths. No test
may weaken the Safe default to make a failure pass.

### 10.1 Unit tests

Add resolver/policy tests for:

- in-root relative and absolute paths;
- `..` traversal above the root;
- sibling and parent paths;
- configured additional roots;
- missing target with an in-scope parent;
- missing target with an outside parent;
- symlink inside the workspace pointing outside;
- symlink outside pointing inside;
- symlink and parent replacement between preflight and final validation;
- Windows separators and platform-specific absolute paths where supported;
- invalid, empty, NUL-containing, and malformed paths;
- deterministic display/root identity and error codes.

Add mode decision tests for:

- Safe/in-scope → allowed without scope approval;
- Safe/outside → `approval_required` with canonical request data;
- Safe/outside denial/cancellation/timeout → no I/O;
- Safe capability approval plus outside scope → one combined request;
- capability remember does not grant a new outside target;
- Plan/outside → hard denial without approval or I/O;
- Yolo/outside → explicit unrestricted decision without approval;
- Yolo OS denial remains a failure;
- mode changes invalidate cached decisions;
- dangerous-skip-permissions does not bypass Safe scope approval.

Add approval-service tests for:

- request/response rendezvous and pending-state cleanup;
- allow-once target binding;
- root-turn/session grant boundaries;
- concurrent requests serialization;
- cancellation, timeout, and overlay cleanup;
- no capability/scope grant cross-contamination;
- stable headless denial.

### 10.2 Filesystem and mention integration tests

Use a temporary tree containing a decoy file inside the workspace and a
different file outside it. Verify:

- Safe `read_file(../outside/file)` waits for approval and reads only after
  approval;
- Safe denial leaves the outside file untouched and returns the correct error;
- Yolo reads the outside file through the unrestricted policy;
- Safe outside writes, deletes, moves, copies, and directory creation are
  gated before mutation;
- source and destination in a move/copy are checked separately;
- list/search/grep/glob do not enumerate or read outside targets before Safe
  approval;
- `@../outside/file` uses the same decision and never injects content early;
- failed mention access never causes the model/tool layer to read the decoy;
- command `cwd=../outside` follows the same Safe/Yolo policy;
- workflows, retries, subagents, and plugins receive the same scope identity;
- final revalidation catches a symlink target changed after approval;
- in-root behavior remains unchanged.

### 10.3 Approval and TUI integration tests

Exercise the real `ApprovalService`, `AppState.pending_approval`, and overlay
path for:

- the request displays the exact path and operation;
- approval resumes the same agent task without resending the user turn;
- denial returns a structured tool result and clears the overlay;
- Escape and cancellation clear pending state;
- scope and capability reasons are shown once, not as duplicate prompts;
- a mode switch while waiting is handled without an orphaned overlay;
- Yolo outside access creates no overlay;
- transcript and journal events distinguish Safe approval from Yolo bypass.

### 10.4 Headless and end-to-end tests

Add deterministic headless tests for:

- Safe outside access denied without an interactive approval channel;
- an injected test approval adapter approving/denying a specific canonical
  target;
- no hang when approval is unavailable;
- Yolo outside access completing with the exact target;
- Plan outside access blocked;
- resume/replay retaining the decision event but not repeating unapproved I/O;
- a complete workflow that reads an approved outside artifact, writes an
  approved outside artifact, and then returns to an in-root phase.

The end-to-end acceptance scenario must include:

1. start a session in `play-rust`;
2. request `../statements/report.md` in Safe;
3. assert no outside read occurs before approval;
4. approve and assert the exact outside content is used;
5. deny a different outside target and assert the in-root decoy is not read;
6. switch to Yolo and assert the different exact outside target succeeds with
   no prompt;
7. switch to Plan and assert outside access is blocked;
8. inspect transcript/journal records for mode, target, decision, and result.

## 11. Implementation plan

### Phase 1 — Resolver and policy contract

- Implement the PRD-167 `WorkspaceScope`/canonical resolver contract.
- Add scope classification and operation-aware result types.
- Add explicit `SCOPED` and `UNRESTRICTED` policy modes.
- Keep all path normalization and symlink handling in one module.

### Phase 2 — Approval integration

- Extend `ApprovalRequest`/`ApprovalResponse` with scope-specific data and
  grants.
- Update `ApprovalService` serialization, cancellation, and memory handling.
- Update `ApprovalOverlay` rendering and choices without introducing a second
  overlay host.
- Ensure ordinary capability remembers cannot grant scope access.

### Phase 3 — Tool and mention adoption

- Pass `WorkspaceScope` and policy through `SessionContext`, `AgentTurnContext`,
  tool contexts, workflows, subagents, and headless execution.
- Adapt `WorkspaceView`, `ToolSandbox`, Linux filesystem backends, command
  execution, batch operations, and mention injection.
- Remove direct unscoped mention reads and `os.getcwd()`-derived policy
  contexts.
- Add final canonical revalidation immediately before I/O.

### Phase 4 — Modes and lifecycle

- Wire Safe/Plan/Yolo policy selection to the live canonical mode signal.
- Define mode-switch cancellation/re-evaluation behavior.
- Ensure workflow checkpoints, resume, replay, and generated workflows carry
  policy identity and decision events without durable consent leakage.

### Phase 5 — Diagnostics, documentation, and rollout

- Add event/journal/transcript fields and compact/expanded TUI rendering.
- Document Safe approval, Plan blocking, and Yolo workspace-boundary bypass in
  `README.md`, CLI help, `docs/guides/`, and the relevant architecture/storage
  references.
- Update PRD-167 to link here as the policy revision; do not delete its
  resolver findings.
- Add migration notes for users who previously saw an immediate
  `PermissionError` for `../` paths.

## 12. Compatibility and migration

The migration must preserve the following:

- current in-root access and display paths;
- explicit `security.allowed_paths` configuration;
- the existing `ApprovalService` session lifetime and overlay ownership;
- Plan's no-side-effect guarantee;
- Yolo's existing no-prompt capability behavior;
- headless no-hang behavior;
- generated workflow and subagent construction contracts.

The behavior change is intentional for Safe outside-root access: a request
that previously failed immediately now produces an approval request when an
interactive approval channel exists. Non-interactive Safe runs still deny by
default. Existing deny/error codes should remain machine-readable, with
`outside_workspace`, `approval_required`, `approval_denied`, and
`target_changed` distinguished from `not_found` and OS errors.

No durable permission is migrated from an old session. On resume, an old
transcript may describe a past decision, but a new uncompleted I/O operation
must be reauthorized under the current mode and scope.

## 13. Open questions to resolve during implementation

These questions must be answered in the implementation record before the PRD
can be marked Implemented:

1. Should a Safe user be offered a session-scoped outside-root grant in the
   first release, or only allow-once and turn-scoped grants?
2. Should a pending Safe request be re-evaluated automatically after a switch
   to Yolo, or cancelled and retried by the agent with a fresh tool call?
3. How should a batch request render a large number of outside targets while
   preserving per-target authorization?
4. Which command arguments can be proven to be filesystem paths, and which
   must remain governed only by the command tool's declared capability?
5. Should configured roots be displayed as absolute paths, project-relative
   paths, or redacted labels in each TUI mode?
6. Which existing journal event type should carry `workspace_policy` metadata,
   and what versioning is required for replay compatibility?

The recommended defaults are allow-once plus explicit turn-scoped grants,
cancel-and-retry on a mode switch, bounded per-target batch review, declared
command path metadata, root-relative TUI display, and additive versioned
policy fields on existing tool events.

## 13.1 Implementation record

The first implementation resolves the open questions as follows:

1. Safe supports exact target-once, target-this-turn, and target-this-session
   grants. Grants are keyed by canonical target plus operation, held only in
   session memory, and are never persisted as configuration or consent data.
2. The live `AppState.active_mode` signal is observed while a Safe scope
   approval is pending. Switching to Yolo cancels the stale overlay and
   re-evaluates the exact request through the explicit unrestricted policy;
   switching to Plan cancels it and returns a hard outside-workspace denial.
   Plain callable policy providers retain a non-observing library contract.
3. Batch and discovery surfaces authorize each canonical target independently.
   Reviews are bounded by the existing discovery/output limits; a denied
   candidate is omitted and cannot authorize another candidate.
4. Only declared path-bearing command fields (`cwd`, `path`, and the built-in
   test path) are classified. Arbitrary shell text is not guessed or parsed;
   command capability and process policy continue to govern it.
5. The overlay displays a relative target when it is within a configured root
   and an absolute canonical display for an outside target. The primary root
   is shown explicitly for the decision context.
6. Workspace policy metadata is additive on completed tool events and bounded
   to requested/canonical/display paths, operations, root identity, status,
   mode, and decision code. File contents, environment values, and secrets
   are excluded.

## 14. Acceptance criteria

The feature is ready for implementation review only when all of the following
are demonstrable:

1. In Safe, `../outside/file` cannot be read, written, searched, listed,
   deleted, copied, moved, or used as a command `cwd` before a scope-aware
   approval response is received.
2. The Safe approval request contains the exact requested path, canonical
   target, operation, active mode, outside-workspace reason, and any additional
   capability reason.
3. Safe denial, cancellation, timeout, unavailable approval, and final target
   mismatch cause no I/O and return stable structured errors.
4. In Yolo, the same explicit outside target can be accessed without a
   workspace-boundary prompt, using an explicit unrestricted policy.
5. Yolo still respects operating-system/process/container permissions and
   reports their failures accurately.
6. Plan cannot access an outside target and does not show an approval prompt
   for an operation it is hard-blocked from performing.
7. A capability remember decision never authorizes a new outside-workspace
   target.
8. Mentions, filesystem tools, search/glob, command `cwd`, workflows,
   subagents, plugins, headless mode, resume, and replay use the same resolver
   and policy identity.
9. A denied or failed mention cannot cause a same-named in-root fallback.
10. A symlink or path changed after approval cannot redirect Safe I/O to a new
    canonical target.
11. TUI approval waits do not block the event loop, leak pending overlays, or
    duplicate the user turn.
12. Headless Safe operation never hangs and defaults to denial when no
    explicit approval adapter is supplied.
13. In-root Safe behavior and existing Yolo capability behavior remain
    backward compatible.
14. Unit, integration, and E2E tests cover all policy branches and pass in a
    clean checkout.
15. Documentation explains the distinction between Safe approval, Plan
    blocking, configured additional roots, and Yolo's workspace-boundary
    bypass.

## 15. Verification commands

During implementation, run the focused tests first:

```bash
uv run pytest tests/unit/test_sandbox.py tests/unit/test_three_mode_gates.py -q
uv run pytest tests/unit tests/integration tests/e2e -k 'workspace or sandbox or approval or mode or mention' -q
```

Then run the repository gates required by `AGENTS.md`:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/ -q
```

The implementation record must report any environment blocker and include
links to the resolver, approval, mode, tool, mention, workflow, headless, and
end-to-end test evidence.

## 16. Implementation assumptions

- “Workspace” means the canonical primary root plus explicitly configured
  `security.allowed_paths`, not the process's arbitrary current directory.
- “Above the workspace” means a canonical target outside every configured root
  after symlink resolution; string-prefix checks are insufficient.
- Safe approval authorizes a concrete access decision and never silently
  modifies configuration.
- Yolo's unrestricted behavior is intentionally limited to the agenthicc
  workspace boundary; operating-system and runtime boundaries remain real.
- The canonical resolver and scope object from PRD-167 are reused. This PRD
  changes their mode policy and approval behavior, not their identity or
  exact-target guarantees.
- Existing mode and approval infrastructure is extended in place so there is
  one live `AppState`, one `ApprovalService`, and one policy path per session.
