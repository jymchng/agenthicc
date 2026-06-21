# PRD-120 — Unified Tick Frame Counter

## Problem

Animation state in `ConversationStore` grew organically into five separate
pieces that require coordinated changes for every new animated UI element:

| Name | Type | Increments when | Purpose |
|---|---|---|---|
| `_thinking_frame` | bare `int` | agent running AND `elapsed_s` changed | thinking + flower animation |
| `_flower_frame` | bare `int` | same condition | flower icon cycle |
| `elapsed_s` | `Signal[float]` | agent running AND Δ≥0.1 s | **display value AND redraw trigger** |
| `compact_tick` | `Signal[int]` | compaction active | compaction spinner redraw only |
| `compaction_active` | `Signal[bool]` | compactor | spinner visibility |

Key pain points:

- `elapsed_s` does double duty: it is both the displayed elapsed timer value
  and the only mechanism that drives redraws for `_thinking_frame` and
  `_flower_frame`. This means animations freeze when `elapsed_s` stops
  changing, which happens the moment the agent goes idle.
- The compaction spinner required a separate `compact_tick: Signal[int]` (PRD-119)
  solely to produce redraws when the session is otherwise idle.
- `_thinking_frame` and `_flower_frame` are bare ints; the workspace cannot
  subscribe to them and must rely on `elapsed_s` changing to repaint them.
- Every new animation requires a new signal, a new `workspace.py` subscription
  entry, and a new branch in `tick()`.

## Goal

Replace all per-feature frame counters with a **single `frame: Signal[int]`**
that increments unconditionally every 50 ms. All animation derives from this
one counter. The workspace subscribes to it once for all animation redraws.

## Solution

### 1. `ConversationStore` — three additions, five removals

**Add:**
```python
frame: Signal[int] = Signal(0)   # universal animation counter; increments every 50 ms
```

**Remove:**
- `_thinking_frame: int`
- `_flower_frame: int`
- `compact_tick: Signal[int]`
- `elapsed_s: Signal[float]`  →  replaced by `elapsed_s: @property → float`

`elapsed_s` becomes a read-only property:
```python
@property
def elapsed_s(self) -> float:
    return time.monotonic() - self._start_time if self._start_time else 0.0
```

**Simplify `tick()`:**
```python
def tick(self) -> None:
    self.frame.set(self.frame() + 1)   # always; drives all animation redraws
```

No branches, no conditions. `frame` always advances; `elapsed_s` is computed
on demand in `render()`.

### 2. `workspace.py` subscriptions

Remove: `conv.elapsed_s`, `conv.compact_tick`
Add: `conv.frame`

One subscription covers all animation redraws.

### 3. `StatusComponent.render()`

All frame indices become `conv.frame() % N`:

```python
flower      = _FLOWERS[conv.frame() % len(_FLOWERS)]
think_text  = _thinking_markup(conv.frame())
compact_sp  = _COMPACT_SPINNER[conv.frame() % len(_COMPACT_SPINNER)]
elapsed     = conv.elapsed_s   # property, no parentheses
```

### 4. `begin_turn()` cleanup

Remove `self.elapsed_s.set(0.0)` and `self._thinking_frame = 0` (both now unnecessary).

## Invariants

- `frame` increments monotonically; never resets to 0 (not needed — all
  consumers use `frame() % N`).
- `elapsed_s` property returns 0.0 when `_start_time` is 0.0 (idle).
- `compaction_active: Signal[bool]` is unchanged — the workspace still
  subscribes to it to show/hide the spinner on toggle.

## Files changed

| File | Change |
|---|---|
| `tui/conversation_store.py` | Add `frame`; remove `compact_tick`, `_thinking_frame`, `_flower_frame`, `elapsed_s` Signal; add `elapsed_s` property; simplify `tick()` |
| `tui/workspace/workspace.py` | Replace `elapsed_s` + `compact_tick` subscriptions with `frame` |
| `tui/workspace/components.py` | All frame indices → `conv.frame() % N`; elapsed → `conv.elapsed_s` |
| `tests/unit/test_unified_tick.py` | New tests |
| `tests/unit/test_workflow_phase_model_display.py` | Update mocks |
