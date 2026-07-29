# Hooks and lifecycle extension status

The repository exposes a small compatibility layer in
`src/agenthicc/tools/hooks.py` and `src/agenthicc/tools/executor.py`, but it is
not a second tool-runtime implementation. `LifecycleHook`, `HookRegistry`, and
`HookRunner` adapt configuration and tests to lauren-ai's canonical
`ToolHook`/decision types; dispatch, ordering, approval signals, and provider
result semantics remain owned by lauren-ai's executor.

The runtime's primary policy boundaries are:

- capability metadata and mode filters before tool selection;
- `PermissionChecker` and `ToolCapabilityGate` for authorization;
- `ApprovalService` for user decisions;
- tool result envelopes and runner retry/error handling;
- kernel `Effect` descriptors for side effects;
- workflow phase transition callbacks and output parsing.

The kernel still has a `HookRegistered` event/state shape for compatibility,
but it is not the source of runtime hook registration. Use the adapter module
only when a lauren-ai hook needs Agenthicc configuration or test integration.

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

Until a broader lifecycle contract is approved, do not expand these adapters
into a second hook engine or import the historical PRD examples as if they
were current APIs. Track any new lifecycle semantics in PRD-138 P2.4.
