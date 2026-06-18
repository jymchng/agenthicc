# PRD-100 — Questions Overlay and `ask_user` Tool

## Problem

Agents in the `code_plan` workflow (and future workflows) sometimes need to
gather structured input from the user before proceeding — for example, choosing
a language, a framework, a testing strategy, or a deployment target.  Today the
only agent-to-user interaction available is `request_plan_approval`, which is a
single binary (approve/reject) gate.  There is no way for the agent to present
multiple questions with selectable options and collect all answers in one
interaction.

---

## Goals

- Provide a `QuestionsOverlay` that presents N questions, each with
  LLM-chosen options and a mandatory free-text fallback.
- Provide an `ask_user(questions)` tool that blocks the agent until the user
  has answered every question, then returns a typed answer dict.
- Reuse the existing `ApprovalService` / `pending_approval` signal path so no
  new kernel signal is required.
- Keep navigation intuitive: arrow keys within a question, left/right between
  questions, a single final Enter to submit everything.

---

## Data types

### `Question`

```python
@dataclass(frozen=True)
class Question:
    id:      str          # dict key in the returned answer map
    text:    str          # question shown to the user
    options: list[str]    # LLM-provided choices (must be non-empty)
                          # overlay always appends "Other — type your answer"
                          # as the final option; do not include it here
```

### Answer dict

```python
# Returned by ask_user() after submission
{
    "language":  "Python",                        # selected option
    "framework": "FastAPI",                       # selected option
    "testing":   "I use pytest with fixture files"  # free text from "Other"
}
```

Answers are encoded as JSON in `ApprovalResponse.message` and parsed by the
tool before returning to the LLM.  Option-selected answers and free-text
answers are indistinguishable from the LLM's perspective.

---

## Tool: `ask_user`

```python
ask_user(questions: list[dict]) -> dict
```

Each element of `questions` must contain `"id"`, `"text"`, and `"options"`.
The tool:

1. Validates the input (non-empty questions list, each entry has the required
   keys, each `options` list is non-empty).
2. Creates an `ApprovalRequest(kind="questions", tool_input={"questions": [...]})`.
3. Calls `await approval_svc.request_approval(req)` — suspends the agent turn.
4. On response, if `allowed=False`, returns `{"cancelled": True}`.
5. If `allowed=True`, parses `response.message` as JSON and returns the answer
   dict.

**Where it lives:** `make_questions_tool(approval_svc)` in `phase_tools.py`,
returning `[ask_user]`.  Injected alongside the base tool set for phases that
need it.

**Headless / no approval_svc:** when `approval_svc is None`, the tool returns
`{"cancelled": True}` immediately without blocking (same pattern as
`request_plan_approval` auto-approve in headless mode).

---

## `QuestionsOverlay`

### Location

`src/agenthicc/tui/workspace/overlays/questions.py`

Extends `PromptOverlay` (for the TYPING state's `InputBuffer`).

### Integration

`_on_approval_change()` in `tui_session.py` gains a new branch:

```python
if getattr(req, "kind", "tool") == "questions":
    overlay = QuestionsOverlay(req, approval_svc, workspace.overlays.hide)
```

No other changes to the signal path.

### Internal state

```python
class _QState:
    cursor:   int  = 0      # highlighted option index (0 = first)
    answer:   str  = ""     # confirmed answer (option label or typed text)
    answered: bool = False  # True once user pressed Enter to confirm
```

```python
class _Mode(Enum):
    SELECTING = auto()   # arrow-key navigation
    TYPING    = auto()   # free-text entry for "Other"
```

The overlay holds:
- `_questions: list[Question]` — parsed from `req.tool_input["questions"]`
- `_states: list[_QState]` — one per question, initialised on mount
- `_current: int` — index of the focused question (0-based)
- `_mode: _Mode`

---

## Rendering

### SELECTING mode

```
──────────────────────────────────────────────────────────────────
  ❓ Questions  (1 of 3 answered)
──────────────────────────────────────────────────────────────────
  ◀  Question 2 of 3  ▶                                  ● ● ○

  Which web framework should be used?

    ▶ FastAPI
      Django
      Flask
      Other — type your answer

──────────────────────────────────────────────────────────────────
  ↑↓ option   ←→ question   Enter confirm   Esc cancel
──────────────────────────────────────────────────────────────────
```

When a question is already answered, the confirmed answer is highlighted with
a `✓` prefix; the cursor may still be moved to it and overridden:

```
    ✓ Python                          ← confirmed (bold or reverse style)
      JavaScript
      TypeScript
      Other — type your answer
```

The dot strip (`● ● ○`) uses filled dots for answered questions and hollow for
unanswered.  It is always rendered as a fixed-width row so the overlay height
does not change as questions are answered.

When **all** questions are answered the hint line changes to:

```
  ↑↓ option   ←→ question   Enter SUBMIT ALL   Esc cancel
```

### TYPING mode

```
──────────────────────────────────────────────────────────────────
  ❓ Question 2 of 3 — type your answer
──────────────────────────────────────────────────────────────────

  Which web framework should be used?

  > My in-house async framework█

──────────────────────────────────────────────────────────────────
  Enter confirm   Esc back
──────────────────────────────────────────────────────────────────
```

The `>` prompt and the buffer text are rendered using `_render_prompt_line()`
from `PromptOverlay`, identical to the PROMPTING state of `PlanApprovalOverlay`.

---

## Key bindings

### SELECTING

| Key | Action |
|---|---|
| `↑` | Move cursor to the previous option of the current question (wraps). |
| `↓` | Move cursor to the next option of the current question (wraps). |
| `←` | Move focus to the previous question. Cursor within that question is preserved. |
| `→` | Move focus to the next question. Cursor within that question is preserved. |
| `Enter` | If cursor is on the "Other" option → enter TYPING mode. Otherwise confirm the highlighted option (mark question answered). If all questions are now answered → submit immediately. Otherwise advance focus to the next unanswered question. |
| `Esc` | Cancel: `service.respond(allowed=False, message="")`, close overlay. |

### TYPING

| Key | Action |
|---|---|
| Printable chars | Append to buffer. |
| `Backspace` | Delete character before cursor. |
| `Enter` | Confirm typed text as the answer for the current question; return to SELECTING. If all questions are now answered → submit immediately. Otherwise advance focus to the next unanswered question. |
| `Esc` | Discard typed text; return to SELECTING. If the question had a prior confirmed answer it is restored; otherwise it remains unanswered. |

---

## "Other" option

- The overlay appends `"Other — type your answer"` as the final option for
  every question automatically.  The LLM must not include it in `options`.
- Once a question is answered via free text, its option list displays:
  `Other: "…text…"` (truncated to available width) with a `✓` prefix.
- Selecting the "Other" option when the question is already answered via free
  text re-enters TYPING with the buffer pre-filled with the previous text.

---

## Submission

There is no separate confirmation step.  The overlay submits as soon as the
final unanswered question receives its answer (either option selection or typed
text confirmed with Enter).  The `service.respond` call carries:

```python
service.respond(
    allowed=True,
    message=json.dumps({q.id: state.answer for q, state in zip(questions, states)}),
)
```

The overlay then closes via `close_fn()`.

---

## File changes

| File | Change |
|---|---|
| `tui/workspace/overlays/questions.py` | New `QuestionsOverlay` class |
| `workflows/phase_tools.py` | Add `make_questions_tool(approval_svc)` |
| `runners/tui_session.py` | Add `kind="questions"` branch in `_on_approval_change()` |

---

## Acceptance criteria

- [ ] `ask_user([{"id": "lang", "text": "Pick a language", "options": ["Python", "Go"]}])` shows the overlay with two selectable options plus "Other".
- [ ] `↑`/`↓` cycles options within a question; `←`/`→` moves between questions without losing previously confirmed answers.
- [ ] Selecting "Other" and pressing Enter enters TYPING mode; Esc returns without overwriting a prior answer.
- [ ] Pressing Enter after all questions are answered submits immediately without an extra confirmation step.
- [ ] Esc at any point calls `service.respond(allowed=False)` and closes the overlay.
- [ ] With `approval_svc=None`, `ask_user` returns `{"cancelled": True}` without blocking.
- [ ] The tool is available in the plan phase of `code_plan` and returns a correctly keyed answer dict to the LLM.
