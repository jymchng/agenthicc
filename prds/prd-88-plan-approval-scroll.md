# PRD-88 — Scrollable Plan Approval Overlay

## Background

`PlanApprovalOverlay` (PRD-86) shows plan content truncated to
`_PLAN_PREVIEW_LINES = 6` lines, with an `… and N more lines` hint.
Users cannot read long plans before deciding to approve or reject.
The `[ and 66 more lines ]` truncation blocks informed decision-making.

---

## Goals

- The plan viewport inside `PlanApprovalOverlay` is scrollable so the
  user can read the full plan before acting.
- Option navigation (UP/DOWN) is unchanged.
- No new `Key` enum values are required.
- A visible scroll position indicator shows where in the plan the
  viewport currently is.
- Footer hints are updated to reflect the new keys.

## Non-Goals

- Scrolling inside the PROMPTING state (the prompt line is always short).
- Mouse scroll support.
- Changes to any other overlay.

---

## Key bindings

| Key | Action |
|---|---|
| `[` (`Key.CHAR`, `ch == "["`) | Scroll plan viewport up one line |
| `]` (`Key.CHAR`, `ch == "]"`) | Scroll plan viewport down one line |
| `↑` / `↓` | Navigate options (unchanged) |
| `Enter` | Select highlighted option (unchanged) |
| `Esc` | Deny / go back (unchanged) |

`[` and `]` are available as `Key.CHAR` with no new enum values.
They do not conflict with option navigation or future prompt typing in
SELECTING mode.

---

## Data model changes

`PlanApprovalOverlay` gains one new field:

```python
self._plan_scroll: int = 0   # index of first visible plan line
```

`_PLAN_PREVIEW_LINES` renamed to `_PLAN_VISIBLE_LINES` and increased
from 6 to 10.

`on_mount()` resets `_plan_scroll = 0`.

---

## Render — SELECTING state

```
  📋 Plan Review
  ──────────────────────────────────────────────────────────
  ## Enhancement Plan for Python Password Generator
  The repository is a well-structured cryptographically...
  ### 1. Passphrase Generator (New Feature)
  **Files:** `password_generator/passphrase.py` (new)...
  ### 2. Bug Fixes
  **1a.** Fix CLI --symbols logic broken...
  **1b.** Fix strength scoring — length_score overwritten...
  **1c.** Fix Hexadecimal preset charset (a-z, not a-f)...
  **2.** Add missing tests for strength.py, presets.py...
  **3.** Expose validate_comprehensive() via __init__.py
  ─ lines 1–10 of 67 ─────────────────────────────── ↓ more
  ▶ Approve
    Reject — add feedback
    Approve — add instructions
  ──────────────────────────────────────────────────────────
    ↑↓ options  [ up  ] down  Enter select  Esc deny
```

**Scroll indicator line** (between plan viewport and options):

| Condition | Indicator text |
|---|---|
| At top, more below | `─ lines 1–N of Total ──────── ↓ more` |
| Mid-document | `─ ↑ above · lines A–B of Total ─── ↓ more` |
| At bottom | `─ ↑ above · lines A–Total of Total ────────` |
| Plan fits entirely | *(no indicator — existing border suffices)* |

---

## Key handling — SELECTING state additions

```python
case Key.CHAR if ch == "[":
    self._plan_scroll = max(0, self._plan_scroll - 1)
case Key.CHAR if ch == "]":
    max_scroll = max(0, total_plan_lines - _PLAN_VISIBLE_LINES)
    self._plan_scroll = min(max_scroll, self._plan_scroll + 1)
```

`total_plan_lines` is computed from the plan text at handle time (same
source as render).

---

## File changes

| File | Change |
|---|---|
| `tui/workspace/overlays/plan_approval.py` | Add `_plan_scroll`; rename/increase `_PLAN_VISIBLE_LINES`; update `_render_selecting()` (viewport slice + scroll indicator); update `_handle_selecting()` (`[`/`]` cases); update footer hint; reset in `on_mount()` |

---

## Acceptance criteria

- [ ] Plan longer than 10 lines shows lines 1–10 with `↓ more` indicator.
- [ ] Pressing `]` scrolls down one line; pressing `[` scrolls up one line.
- [ ] Cannot scroll past the first or last line (clamped).
- [ ] When the plan fits entirely in the viewport, no scroll indicator is
      shown and `[`/`]` are no-ops.
- [ ] UP/DOWN still navigate options; behaviour is unchanged.
- [ ] `on_mount()` resets the scroll position to 0.
- [ ] All existing `PlanApprovalOverlay` unit tests pass.
