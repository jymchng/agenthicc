---
title: "TOOLR-07: Capability and approval gates fail closed"
status: Implemented (2026-11-04; wave 1, pending merge)
version: 1.0.0
date: 2026-11-04
scope: "what happens when ToolCapabilityGate, ApprovalGate or the workspace preflight raises: today the tool runs; it must not"
related_prds: [TOOLR-00, TOOLR-04, PRD-75, PRD-78, SUBAGENT-05]
tags: [tools, security, approvals]
---

# TOOLR-07: Gates fail closed


## Implementation status (2026-11-04)

Delivered in wave 1 (branch `tools-wave-1-gates-and-edits`).

- `agenthicc.tools.capability_gate.FailClosedGate`: `ToolCapabilityGate` and
  `ApprovalGate` implement `decide()`. Any exception other than cancellation
  aborts the call with `error_code="internal"` and records `gate_failure` in
  the call metadata.
- **Amended (goal 2):** the security gates run *last*, not first.
  `order_security_gates` moves them after every other hook in parent turns and
  subagent workers, so they always decide on the final input and no re-run is
  needed.
- **Not delivered (goal 3):** the lauren-ai `hook_errors="abort"` flag is
  upstream work. Goal 1 alone closes the hole for agenthicc's gates.
- Tests: `tests/unit/test_gates_fail_closed.py`. This includes a worker whose
  approval service raises, run through the real lauren-ai executor, and the
  tool never runs. A mutation check confirmed the tests fail without the
  `except`.

## 1. Problem

TOOLR-00 P1. Mode enforcement and approval are `before_tool_call` hooks:
`ToolCapabilityGate` (`tools/capability_gate.py:48`) and `ApprovalGate`
(`tools/approval.py:556`). lauren-ai's executor runs before-hooks like this:

```python
for hook in before_hooks:
    try:
        decision = await hook.before_tool_call(hook_ctx)
    except Exception as exc:
        logger.warning("... raised %s — ignoring", ...)
        continue            # ← the next hook runs, then the tool dispatches
```

An exception inside a gate therefore **skips the gate and executes the tool**.
`ApprovalGate.before_tool_call` calls:
- `policy.authorize_tool(...)` (path resolution and approval);
- `ApprovalRequest(...)` construction with model-supplied input;
- the approval service, which may suspend on the UI.

Any defect along that path is a silent bypass: a malformed `tool_input`
shape, an approval-service failure, a cancelled UI future converted to an
error, or a bug in a future capability. The bypass applies in Safe and Plan
modes, and in subagents whose type denies a capability (SUBAGENT-05). Nothing
tests this property.

## 2. Goals

1. **Gates contain their own failures.**
   - Each security hook catches `Exception` around its whole decision and
     returns `BeforeToolHookDecision.abort(...)` with
     `{ok: false, error_code: "internal", error: "permission check failed: <type>"}`.
   - It logs the exception with the tool name and tool-use id.
   - It emits a `tool_gate_failed` diagnostic event.
   - `CancelledError` propagates unchanged.

   One base class, `FailClosedGate`, provides this.
2. **Ordering is explicit.** Security gates run first, in a fixed order:
   1. capability gate;
   2. workspace preflight;
   3. approval.

   No other before-hook can run before them or modify the input they decided
   on. If a later hook modifies the input, the gates re-run on the modified
   input.
3. **Upstream fix.** lauren-ai gains an opt-in flag. With
   `ToolExecutor(..., hook_errors="abort")`, a raising before-hook aborts the
   call. agenthicc sets it for every executor it builds — the parent turn,
   subagent workers and workflow phases — so the guarantee holds even for
   hooks that do not use `FailClosedGate`. Until that release, goal 1 alone
   closes the hole.
4. **Tested as a property.** For each gate, and in each mode, a test injects
   an exception at each await point of the decision. It asserts that the tool
   function never runs and that the model sees the envelope.

## 3. Non-goals

- Changing what any mode allows.
- Changing the approval UI.

## 4. Acceptance criteria

1. A Safe-mode write whose approval service raises `RuntimeError` does not
   write the file. The model receives `error_code: "internal"`.
2. A Plan-mode write whose capability lookup raises does not run.
3. A subagent of a type that denies `execute` cannot run a command when the
   gate raises.
4. A before-hook registered by a plugin cannot run before the security gates,
   and cannot change the input after they approved it without the gates
   re-running.
5. With the lauren-ai flag available, a raising non-gate before-hook also
   aborts the call (`tests/unit/test_three_mode_gates.py`).
