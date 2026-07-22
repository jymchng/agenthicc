# Hooks and lifecycle extension status

Some historical Agenthicc documents describe a `LifecycleHook`/`HookRunner`
framework with before/after/error stages. Those modules are not present in the
current source tree. The current runtime instead uses explicit services and
boundaries:

- capability metadata and mode filters before tool selection;
- `PermissionChecker` and `ToolCapabilityGate` for authorization;
- `ApprovalService` for user decisions;
- tool result envelopes and runner retry/error handling;
- kernel `Effect` descriptors for side effects;
- workflow phase transition callbacks and output parsing.

The kernel still has a `HookRegistered` event/state shape for compatibility,
but it should not be documented as a working runtime hook executor.

## What to use today

### Tool policy

Use `ToolCapability` metadata for read, write, execute, git, network, and search
capabilities. Modes and agent definitions apply ceilings; child agent scopes
can only restrict their parent.

### Approval

Use `ApprovalService` and an approval request. The TUI maps request kinds to
overlay classes in `TUISession`; headless and test paths can provide recording
or mock approval services.

### Workflow lifecycle

Use `PhaseSpec` transitions, `WorkflowRun` state, explicit kernel events, and
phase output records. Keep success, rejection, retry, and error transitions
observable.

### Plugin lifecycle

Use the discovery result and trust service rather than importing arbitrary
plugin code at a new call site. Record failed imports, missing dependencies,
and trust decisions without leaking credentials.

## Proposed future hook contract

If lifecycle hooks are reintroduced, first specify:

1. the entities and stages that are hookable;
2. synchronous versus asynchronous execution and ordering;
3. whether a hook can reject, retry, mutate, or only observe;
4. timeout and failure isolation;
5. security/trust requirements for hook code;
6. event/audit representation and replay semantics;
7. a stable public API and tests.

Until that design is approved, do not add `tools/hooks.py`-style documentation
or new imports based on the historical PRD examples. Track the decision and
implementation in PRD-138 P2.4.
