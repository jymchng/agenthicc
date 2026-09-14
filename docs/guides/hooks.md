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

## Try it

The adapters are real importable objects — this is the fastest way to confirm
which module owns the compatibility layer:

```bash
PYTHONPATH=src python -c "
from agenthicc.tools.hooks import HookRegistry, LifecycleHook, HookRunner
print('registry:', HookRegistry.__module__)
print('hook:', LifecycleHook.__module__)
print('runner:', HookRunner.__module__)
"
```

```text
registry: agenthicc.tools.hooks
hook: agenthicc.tools.hooks
runner: agenthicc.tools.hooks
```

All three live in one adapter module. If you were expecting a separate hook
engine package, that absence is the point: dispatch, ordering, approval
signals, and provider result semantics stay with lauren-ai's executor.

To see the policy surfaces that actually govern a tool call, inspect the
capability enum:

```bash
PYTHONPATH=src python -c "
from agenthicc.tools.capabilities import ToolCapability
print(sorted(c.name for c in ToolCapability))
"
```

```text
['CONTROL', 'EXECUTE', 'GIT_READ', 'GIT_WRITE', 'NETWORK', 'READ', 'SEARCH', 'UNDECLARED', 'WRITE']
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ImportError: cannot import name 'LifecycleHook'` | Importing it from a kernel or TUI module | Import from `agenthicc.tools.hooks`; the kernel's `HookRegistered` event is a compatibility shape, not the adapter |
| A hook never fires | The adapter only translates configuration for lauren-ai's executor | Register through the lauren-ai hook path; the adapter cannot add a dispatch stage that the executor does not own |
| Hook ordering is not what you configured | Ordering is owned by lauren-ai | Express ordering in the canonical contract rather than expecting the adapter to reorder |
| A hook raises and the turn continues | Failure isolation belongs to the executor | Decide the intended failure semantics for your hook, and test that a raised error produces the outcome you expect |
| A tool runs with no capability metadata | No decorator means `UNDECLARED` | Add explicit metadata. `UNDECLARED` prompts in Safe and is blocked in Plan |
| A hook sees a credential | Diagnostics must stay bounded | Prompts, arguments, outputs, and credentials are never copied into hook diagnostics; do not log them yourself |
| A kernel `HookRegistered` event exists but nothing changed | Correct — the event is compatibility-only | Do not build new behavior on it |

### Do not add a second hook engine

The guide's own advice is the rule: expand the adapters only when a lauren-ai
hook needs agenthicc configuration or test integration. A parallel lifecycle
engine would duplicate dispatch, run twice against the same tool call, and
produce two competing audit trails.

### The historical PRD examples are not current APIs

The "proposed future hook contract" list in this guide is a specification of
what would need to be decided *before* a lifecycle contract exists. Importing
those sketches as if they were shipped modules is the exact mistake this
section exists to prevent.
