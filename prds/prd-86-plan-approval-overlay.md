# PRD-86 — Plan Approval Overlay

## Background

When a workflow runs in Plan mode (or the `supervised` workflow), the
`human_review` phase calls `ApprovalService.request_approval()`, which
shows the generic `ApprovalOverlay`.  That overlay only supports approve /
deny with no text input and no access to the plan content.  The user
cannot give the planner targeted feedback, request changes, or add
instructions to the executor.

`ApprovalResponse` carries no message field, so any typed feedback is
unreachable by the workflow runner.  `WorkflowRunner._run_human_phase()`
discards the prior phase's plan text and returns a bare `PhaseOutput`
with `full_text="[human review]"` — no user input reaches the next phase
agent.

---

## Goals

- Show the plan content produced by the prior phase in the overlay so the
  user can read it before deciding.
- Present exactly three selectable options:
  1. **Approve** — proceed to the next phase immediately.
  2. **Reject — add feedback** — user types a message; workflow loops back
     to the planner with that feedback as context.
  3. **Approve — add instructions** — user types a message; workflow
     proceeds to the executor with the extra instructions as context.
- The user's typed message is injected into `WorkflowContext` so the next
  phase agent receives it via `as_system_block()`.
- All changes are additive.  `ApprovalOverlay` (tool approval) is
  unchanged.  The existing overlay routing and `OverlayHost` machinery
  require no modification.

## Non-Goals

- Multi-line prompt input in the overlay (single-line only).
- Syntax highlighting or scrolling of the plan text within the overlay.
- Changes to how tool approval works (Guard mode, `ApprovalGate`).
- Changes to the headless runner (auto-approve stays as-is).

---

## Architecture

### Layer 1 — `PromptOverlay(Overlay)` — reusable base

**File**: `tui/workspace/overlays/prompt.py`

An abstract base class that adds an embedded `InputBuffer` and a single
editing-key dispatcher to `Overlay`.  Any future overlay that needs a
text input field inherits this instead of `Overlay` directly.

```python
class PromptOverlay(Overlay):
    def __init__(self) -> None:
        self._buf = InputBuffer()

    def on_mount(self) -> None:
        self._buf.clear()

    @property
    def _prompt_text(self) -> str:
        return self._buf.text

    def _handle_prompt_key(self, key: Key, ch: str) -> bool:
        """Delegate one keystroke to the buffer. Returns True if consumed."""
        match key:
            case Key.CHAR if ch and ch != "\n":
                self._buf.insert(ch)
                return True
            case Key.BACKSPACE:
                self._buf.delete_before()
                return True
            case Key.LEFT:
                self._buf.move_left()
                return True
            case Key.RIGHT:
                self._buf.move_right()
                return True
            case Key.HOME:
                self._buf.move_home()
                return True
            case Key.END:
                self._buf.move_end()
                return True
        return False
```

`_handle_prompt_key` is the single canonical place for text-editing key
handling inside overlays.  Subclasses call it and act on the return value
to decide whether to pass the key to their own state machine.

---

### Layer 2 — `PlanApprovalOverlay(PromptOverlay)`

**File**: `tui/workspace/overlays/plan_approval.py`

#### State machine

```
State.SELECTING  (initial)
  │
  ├─ Enter on option 0 (Approve)
  │    → service.respond(allowed=True, message=""); close()
  │
  ├─ Enter on option 1 (Reject — add feedback)
  │    → switch to State.PROMPTING, pending_option = 1
  │
  └─ Enter on option 2 (Approve — add instructions)
       → switch to State.PROMPTING, pending_option = 2

State.PROMPTING
  │
  ├─ Key.CHAR / BACKSPACE / arrows → _handle_prompt_key()
  │
  ├─ Enter
  │    allowed = (pending_option == 2)
  │    → service.respond(allowed=allowed, message=_prompt_text); close()
  │
  └─ Esc → switch back to State.SELECTING; _buf.clear()

Esc in SELECTING → service.respond(allowed=False, message=""); close()
```

#### Options table

| Index | Label | `allowed` | Message |
|---|---|---|---|
| 0 | Approve | `True` | `""` |
| 1 | Reject — add feedback | `False` | user-typed |
| 2 | Approve — add instructions | `True` | user-typed |

#### Render — SELECTING state

```
  📋 Plan Review
  ──────────────────────────────────────────────────────────
  Step 1: Extract the IAuthService interface
  Step 2: Create an adapter that wraps the legacy class
  Step 3: Write unit tests for the adapter
  [and 2 more lines…]
  ──────────────────────────────────────────────────────────
  ▶ Approve
    Reject — add feedback
    Approve — add instructions
  ──────────────────────────────────────────────────────────
    ↑↓ navigate  Enter select  Esc deny
```

Plan content is taken from `req.tool_input["plan"]`, shown up to
`_PLAN_PREVIEW_LINES = 6` lines, with a `[and N more lines…]` note when
truncated.  The `▶` indicator tracks `_selected`.

#### Render — PROMPTING state

```
  📋 Plan Review › Reject — add feedback
  ──────────────────────────────────────────────────────────
  ❯ Please also handle the empty-input edge case▌
  ──────────────────────────────────────────────────────────
    Enter submit  Esc back
```

The prompt line reuses `PROMPT_CHAR` and `CURSOR_CHAR` from
`agenthicc.tui.input.renderer` so the visual style matches the composer.

---

### Layer 3 — `ApprovalRequest.kind` and `ApprovalResponse.message`

**File**: `tools/approval.py`

```python
@dataclass(frozen=True)
class ApprovalRequest:
    tool_name:    str
    tool_use_id:  str
    tool_input:   dict
    capabilities: frozenset
    event:        asyncio.Event = field(compare=False, hash=False)
    kind:         str = "tool"      # "tool" | "plan_review"

@dataclass(frozen=True)
class ApprovalResponse:
    allowed:      bool
    remember:     bool = False
    remember_all: bool = False
    message:      str  = ""         # user-typed feedback / instructions
```

`ApprovalService.respond()` gains a `message: str = ""` parameter and
stores it before setting the event:

```python
def respond(self, allowed: bool, *, remember=False, remember_all=False,
            message: str = "") -> None:
    self._response = ApprovalResponse(
        allowed=allowed, remember=remember,
        remember_all=remember_all, message=message,
    )
    pending = self._app_state.pending_approval()
    if pending is not None:
        pending.event.set()
```

`kind` defaults to `"tool"` so all existing callers (`ApprovalGate`,
tests, etc.) are unaffected.

---

### Layer 4 — Overlay routing in `tui_session.py`

**File**: `runners/tui_session.py`

```python
def _on_approval_change() -> None:
    req = app_state.pending_approval()
    from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
    from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay

    if req is not None:
        if req.kind == "plan_review":
            overlay = PlanApprovalOverlay(req, approval_svc, workspace.overlays.hide)
        else:
            overlay = ApprovalOverlay(req, approval_svc, workspace.overlays.hide)
        workspace.overlays.show(overlay)
    else:
        if isinstance(workspace.overlays.widget, (ApprovalOverlay, PlanApprovalOverlay)):
            workspace.overlays.hide()
```

---

### Layer 5 — `WorkflowRunner._run_human_phase()` update

**File**: `workflow/runner.py`

```python
req = ApprovalRequest(
    tool_name    = f"Review: {spec.name}",
    tool_use_id  = uuid.uuid4().hex,
    tool_input   = {"plan": prior_text} if prior_text else {},
    capabilities = frozenset(),
    event        = asyncio.Event(),
    kind         = "plan_review",       # ← routes to PlanApprovalOverlay
)
response = await self._approval_svc.request_approval(req)

# Embed the user's message so WorkflowContext propagates it to the next phase.
full_text = response.message if response.message else "[human review]"

return PhaseOutput(
    phase_name = spec.name,
    role       = "human",
    full_text  = full_text,
    approved   = response.allowed,
    agent_id   = "human",
)
```

Because `PhaseOutput.full_text` is included in `WorkflowContext.as_system_block()`,
the next phase agent automatically sees the user's message:

```
[WORKFLOW CONTEXT]
Original intent: Refactor the auth module

Completed phases:
- plan (planner): Step 1: Extract the IAuthService interface…
- human_review (human): Please also handle the empty-input edge case
```

On rejection, `PhaseRunRecord.approved = False` causes
`_determine_transition()` to return `spec.on_reject`, sending the
workflow back to the planner.  On approval-with-instructions,
`approved = True` and `full_text` carries the instructions forward.

---

## Context flow diagram

```
WorkflowRunner.run()
│
├─ _run_phase("plan") → PhaseOutput(full_text="Step 1: …")
│
├─ _run_human_phase("human_review")
│    │  creates ApprovalRequest(kind="plan_review", tool_input={"plan": "Step 1:…"})
│    │  → approval_svc.request_approval(req)
│    │     → pending_approval.set(req)
│    │        → _on_approval_change() → PlanApprovalOverlay shown
│    │
│    │  [user navigates, selects "Reject — add feedback", types message]
│    │
│    │  → PlanApprovalOverlay.handle_key(ENTER in PROMPTING)
│    │     → service.respond(allowed=False, message="Please handle edge case")
│    │        → req.event.set() → coroutine resumes
│    │
│    └─ returns PhaseOutput(approved=False, full_text="Please handle edge case")
│
├─ _determine_transition() → approved=False, on_reject="plan" → loop back
│
└─ _run_phase("plan") — context now includes "human_review: Please handle…"
```

---

## Interaction with existing systems

| System | Impact |
|---|---|
| `ApprovalOverlay` | Unchanged — tool approval flow unaffected |
| `ApprovalGate` | Unchanged — `kind` defaults to `"tool"`, no code change |
| `ApprovalService.request_approval()` | Unchanged — serialises both kinds via the same lock |
| `OverlayHost` | Unchanged — routes keys to whichever overlay is active |
| `OverlayCapability` | Unchanged — consumes all keys when any overlay is active |
| `WorkflowContext.as_system_block()` | Unchanged — already includes every PhaseOutput |

---

## File changes

| File | Change |
|---|---|
| `tui/workspace/overlays/prompt.py` | **New** — `PromptOverlay(Overlay)` with embedded `InputBuffer` |
| `tui/workspace/overlays/plan_approval.py` | **New** — `PlanApprovalOverlay(PromptOverlay)`, 3-option state machine |
| `tools/approval.py` | Add `kind: str = "tool"` to `ApprovalRequest`; add `message: str = ""` to `ApprovalResponse`; add `message` param to `ApprovalService.respond()` |
| `runners/tui_session.py` | Route `req.kind == "plan_review"` → `PlanApprovalOverlay` in `_on_approval_change()` |
| `workflow/runner.py` | Set `kind="plan_review"` on request; embed `response.message` in `PhaseOutput.full_text` |

---

## Acceptance criteria

- [ ] In Plan mode, submitting a message shows `PlanApprovalOverlay` with
      the plan content from the prior phase visible.
- [ ] Option 0 (Approve): one keypress closes the overlay and the workflow
      proceeds to the next phase with no message.
- [ ] Option 1 (Reject — add feedback): overlay enters PROMPTING state;
      Enter submits the typed message; `_determine_transition()` returns
      `on_reject`; the next planner phase receives the message in its
      `[WORKFLOW CONTEXT]` block.
- [ ] Option 2 (Approve — add instructions): overlay enters PROMPTING state;
      Enter submits the typed message; workflow proceeds; the next executor
      phase receives the message in its `[WORKFLOW CONTEXT]` block.
- [ ] Esc in PROMPTING returns to SELECTING without submitting.
- [ ] Esc in SELECTING denies (equivalent to Reject with no message).
- [ ] Tool approval in Guard mode still shows the original `ApprovalOverlay`
      unchanged.
- [ ] `ApprovalGate`, `ApprovalService.request_approval()`, and all existing
      tests pass without modification.
- [ ] `PromptOverlay._handle_prompt_key()` correctly handles Char, Backspace,
      Left, Right, Home, End.
