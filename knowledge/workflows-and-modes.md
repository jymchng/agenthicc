# Workflows and Modes

Workflows and modes are independent entities. A mode declares *how the agent
is allowed to behave*; a workflow declares *what sequence of phases to run*. The
relationship between them is a loose binding — established at registry build time
— not a structural dependency.

---

## Core types

### `RuntimeMode`

A named execution context. Lives in `tui/runtime/mode_manager.py`.

```python
@dataclass(frozen=True)
class RuntimeMode:
    name:                 str
    badge:                str
    blocked_capabilities: frozenset[str]   # tool caps the mode disallows
    approval_required:    frozenset[str]   # tool caps that need HITL confirmation
    system_prompt_suffix: str              # appended to every agent turn in this mode
    default_workflow:     str | None       # workflow run on user submit (None = plain turn)
    workflows:            tuple[str, ...]  # all workflows available in this mode
```

A mode says nothing about *what sequence of phases* to run. It only constrains
tool access and optionally names the workflow to dispatch on submit.

### `WorkflowDefinition`

An ordered phase graph. Lives in `workflows/plugin.py`.

```python
@dataclass(frozen=True)
class WorkflowDefinition:
    name:          str
    description:   str
    phases:        tuple[PhaseSpec, ...]
    mode_bindings: tuple[str, ...]   # modes that may trigger this workflow
    source:        str               # "builtin" | "user" | "project"
```

`mode_bindings` is the only coupling to modes — it is a declaration of
*affinity*, not a hard dependency. A workflow can run in any mode it is bound
to; it can also be invoked explicitly regardless of mode bindings.

---

## Many-to-many binding

The relationship is built by `WorkflowRegistry` at session startup:

```python
class WorkflowRegistry:
    def mode_default_map(self) -> dict[str, str]:
        """First registered workflow per mode → that mode's default."""
        result: dict[str, str] = {}
        for defn in self._defs.values():
            for mode_name in defn.mode_bindings:
                result.setdefault(mode_name, defn.name)   # first wins
        return result

    def mode_available_map(self) -> dict[str, list[str]]:
        """All workflows bound to each mode."""
        result: dict[str, list[str]] = {}
        for defn in self._defs.values():
            for mode_name in defn.mode_bindings:
                result.setdefault(mode_name, []).append(defn.name)
        return result
```

`build_default_registry()` in `mode_manager.py` calls both maps and stamps the
results onto each `RuntimeMode` when constructing the `ModeRegistry`.

### What this enables

| Scenario | How it works |
|---|---|
| One mode, many workflows | Multiple `WorkflowDefinition`s each list the same mode in `mode_bindings`. `mode_available_map` collects all of them under that mode key. |
| One workflow, many modes | One `WorkflowDefinition` lists several modes in its `mode_bindings`. It appears in every one of those modes' `workflows` tuple. |
| Workflow available but not default | If a second workflow also binds to the same mode, only the first registered becomes `default_workflow`; the rest appear in `workflows` for explicit dispatch. |
| Workflow with no mode binding | `mode_bindings = []` — the workflow exists in the registry and can be triggered explicitly, but it never auto-dispatches on user submit. |

---

## Builtin examples

```python
class CodePlan(WorkflowPlugin):
    name          = "code_plan"
    mode_bindings = ["Plan"]          # bound to exactly one mode

    mode_bindings = ["Review"]        # bound to a different mode

class Supervised(WorkflowPlugin):
    name          = "supervised"
    mode_bindings = []                # not bound to any mode — explicit dispatch only

class Architect(WorkflowPlugin):
    name          = "architect"
    mode_bindings = []                # same
```

After `build_workflow_registry()` + `build_default_registry()` runs:

| Mode | `default_workflow` | `workflows` |
|---|---|---|
| `Plan` | `"code_plan"` | `("code_plan",)` |
| `Review` | `"plan_only"` | `("plan_only",)` |
| `Auto` | `None` | `()` — plain agent turn |
| `Guard` | `None` | `()` — plain agent turn with HITL |

`supervised` and `architect` live in the registry but appear in no mode's
`workflows` tuple until a user or project config adds a binding.

---

## Trigger path at runtime

When the user presses Enter in the TUI, `TUISession._handle_user_message` does:

```python
active_wf_name = ctx.app_state.active_mode().default_workflow
if active_wf_name is not None:
    # dispatch to the named workflow runner
else:
    # plain _run_agent_turn
```

The mode supplies the workflow *name*; the `WorkflowRegistry` holds the
definition; the runner (`CodePlanRunner` or the generic `WorkflowExecutor`)
owns all execution logic. The mode is not consulted again until a
`PhaseSpec.mode_override` temporarily switches it mid-workflow (see below).

---

## `PhaseSpec.mode_override` — workflow controlling mode, not the reverse

Individual phases can temporarily switch the session mode for their duration:

```python
PhaseSpec(
    name="execute",
    mode_override="Auto",   # lifts Plan-mode tool restrictions during execution
    ...
),
PhaseSpec(
    name="review",
    mode_override=None,     # stays in Plan mode → read-only, no writes
    ...
),
```

`_run_turn` in `CodePlanRunner` saves the original `RuntimeMode`, calls
`ModeManager.set_by_name(mode_override)` before the turn, and restores it in
the `finally` block. This means:

- A workflow running inside `Plan` mode can grant itself `Auto` capabilities
  for a single phase without permanently changing the session mode.
- The workflow drives the temporary mode change; the mode does not drive
  the workflow.

This is the *only* direction in which modes and workflows interact at runtime.

---

## Adding a new binding

To bind an existing workflow to a new mode, add the mode name to the
workflow's `mode_bindings`. No changes to the mode definition are needed:

```python
class CodePlan(WorkflowPlugin):
    mode_bindings = ["Plan", "Safe"]   # now also runs in Safe mode
```

To create a mode that uses multiple workflows — one default, one on-demand:

```python
class FastPlan(WorkflowPlugin):
    name          = "fast_plan"
    mode_bindings = ["Plan"]           # also binds to Plan

# Registration order determines the default.
# If CodePlan is registered first, it remains the default for Plan.
# FastPlan appears in Plan's `workflows` tuple for explicit dispatch.
```

The mode object itself never needs to be modified. Bindings are always
declared by the workflow, never by the mode.
