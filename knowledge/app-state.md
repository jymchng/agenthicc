# AppState

`AppState` is the root reactive state container — one instance lives for the
entire application lifetime.

```python
class AppState:
    conversation: ConversationStore   # transcript, agent state, metrics, mode signals
    input:        InputState          # buffer, cursor, paste mode
    overlay:      Signal[str]         # name of the active overlay ("" = none)
    modal_open:   Signal[bool]        # True when any overlay is showing
```

---

## `ConversationStore`

Accessed as `app_state.conversation`.

### Signals

| Signal | Type | Holds |
|---|---|---|
| `turns` | `Signal[list[ConversationTurn]]` | Full transcript history |
| `agent_state` | `Signal[AgentState]` | IDLE / THINKING / RUNNING / RECOVERING / ERROR |
| `active_tool` | `Signal[str]` | Name of the currently-executing tool |
| `elapsed_s` | `Signal[float]` | Seconds since turn started |
| `tokens_in` | `Signal[int]` | Cumulative input tokens |
| `tokens_out` | `Signal[int]` | Cumulative output tokens |
| `cost_usd` | `Signal[float]` | Cumulative cost |
| `session_id` | `Signal[str]` | Current session UUID |
| `model_name` | `Signal[str]` | `"provider/model"` string |
| `active_mode_name` | `Signal[str]` | `"Auto"`, `"Plan"`, etc. |
| `active_mode_badge` | `Signal[str]` | `"⏵⏵"` |
| `mode_str` | `Signal[str]` | Full footer mode line string |
| `notification` | `Signal[str \| None]` | Transient footer notification |

### Computed signals

| Signal | Type | Derived from |
|---|---|---|
| `is_running` | `Computed[bool]` | `agent_state` — True when not IDLE / COMPLETE / ERROR |
| `turn_count` | `Computed[int]` | `len(turns)` |
| `total_tokens` | `Computed[int]` | `tokens_in + tokens_out` |

### `AgentState` enum

| Value | Meaning |
|---|---|
| `IDLE` | No agent turn in progress |
| `THINKING` | LLM generating a response |
| `RUNNING` | A tool call is executing |
| `RECOVERING` | Tool failed; LLM responding to the error |
| `COMPLETE` | Turn finished successfully |
| `ERROR` | Turn ended with a fatal error |

---

## `InputState`

Accessed as `app_state.input`.

| Signal | Type | Holds |
|---|---|---|
| `buf` | `Signal[list[str]]` | Current buffer as a character list |
| `cursor` | `Signal[int]` | Cursor position (index into `buf`) |
| `paste_condensed` | `Signal[bool]` | Whether a paste is shown condensed |
| `paste_label` | `Signal[str]` | `"[Pasted text with N chars]"` label |

---

## Structural gap — no dedicated slot for runtime session state

`AppState` has no top-level slot for mode (split between `ModeManager` and
`ConversationStore` signals), model/provider selection, or future runtime
state (active workspace, skill context, agent permissions).

The proposed clean separation (not yet implemented) would be:

```python
class AppState:
    conversation: ConversationStore   # transcript + agent state + metrics
    input:        InputState          # buffer + cursor + paste
    session:      SessionState        # mode, model, session_id, future runtime state
    overlay:      Signal[str]
    modal_open:   Signal[bool]

class SessionState:
    mode:        Signal[str]          # "Auto", "Plan", etc.
    mode_badge:  Signal[str]          # "⏵⏵"
    mode_str:    Signal[str]          # full footer string
    model_name:  Signal[str]          # provider/model
    session_id:  Signal[str]
    # future: active_workspace, active_skill, agent_scope, …
```

This would let `ModeManager` write to `app_state.session.mode` directly
rather than reaching into `ConversationStore`, and gives all future runtime
state a clear home. See PRD-73+ for planned refactors.
