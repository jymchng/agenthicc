# Workflow and Mode Plugins

Users can extend agenthicc with custom workflows and custom modes by placing
Python files in their project's `.agenthicc/` directory.

---

## Workflow plugins ✓ fully working

### Discovery paths (load order)

| Directory | Scope | Precedence |
|---|---|---|
| `~/.agenthicc/workflows/*.py` | User-global | Loaded first |
| `.agenthicc/workflows/*.py` | Project-local | Loaded second — **wins** on name collision |

### Called at runtime

```
_build_session_context()                             tui_session.py:158–161
  → build_workflow_registry(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc"
    )                                                workflows/registry.py:54–73
      → _scan_workflow_dir(".agenthicc/workflows")   registry.py:76–89
      → _scan_workflow_dir("~/.agenthicc/workflows")
      → load_python_workflows(path, source)           workflows/loader.py
  → mode_manager = ModeManager(
        default_map=workflow_registry.mode_default_map(),
        available_map=workflow_registry.mode_available_map(),
    )                                                tui_session.py:170–171
```

### File format

Each `.py` file must export one or more `WorkflowPlugin` subclasses:

```python
from agenthicc.workflows.plugin import WorkflowPlugin, PhaseSpec, PhaseRole

class MyWorkflow(WorkflowPlugin):
    name          = "my_workflow"       # unique identifier
    description   = "Custom workflow"
    mode_bindings = ["Plan"]            # modes that auto-trigger this workflow
    phases        = [
        PhaseSpec(name="plan",    agent_type="auto", max_turns=20, next="execute"),
        PhaseSpec(name="execute", agent_type="auto", max_turns=40),
    ]
```

`mode_bindings` determines which mode automatically runs this workflow when the
user submits a message. If two workflows bind the same mode, the first
registered becomes the default; others are available for explicit dispatch.

---

## Mode plugins ✓ fully working (fixed)

Prior to the fix in `tui/runtime/mode_manager.py`, `discover_mode_plugins()`
existed and was tested in isolation but was never called from the runtime.
The fix adds the call to `build_default_registry()` after builtins are loaded.

### Discovery paths (load order)

| Directory | Scope | Precedence |
|---|---|---|
| `~/.agenthicc/modes/*.py` | User-global | Loaded first |
| `.agenthicc/modes/*.py` | Project-local | Loaded second — **wins** on name collision |

Both can override builtins of the same name.

### Called at runtime

```
build_default_registry()                             mode_manager.py:51–138
  → loads 6 builtin modes from agenthicc.modes.builtin
  → discover_mode_plugins()                          modes/plugin_loader.py:241
      → scan ~/.agenthicc/modes/*.py  (user-global)
      → scan .agenthicc/modes/*.py    (project-local)
      → register each Mode into ModeRegistry
```

### File format

Each `.py` file exports `MODE` (single) or `MODES` (list):

```python
from agenthicc.modes import Mode

MODE = Mode(
    name="Focus",
    label="◎",               # badge shown in footer
    description="Read-only focused mode",
    colour="magenta",         # Rich color for badge + name in footer
    system_patch="You are in Focus mode. Prioritise the single task at hand.",
    tool_filter=None,         # optional: callable(tool) -> bool
    source_id="builtin",      # auto-replaced with "mode-plugin:<stem>"
)

# Multiple modes:
# MODES = [Mode(...), Mode(...)]

# Optional dependency check:
# DEPENDENCIES = ["some-package>=1.0"]
```

### Capability blocking for custom modes

`_BLOCKED` and `_APPROVAL` in `build_default_registry()` are looked up by
`mode.name`. A custom mode named `"Safe"` would inherit the read-only
capability block; any other name gets an empty frozenset (all tools allowed).
To block tools in a new custom mode, the user must also declare it in
`_BLOCKED` — or use `tool_filter` at the `Mode` level.

### Startup confirmation

Failed mode plugin loads are logged at WARNING level with the file path and
error message. No confirmation is printed for successful loads (unlike tools
and commands which print a count).

---

## Comparison

| | Workflows | Modes |
|---|---|---|
| Discovery function | `build_workflow_registry()` | `discover_mode_plugins()` |
| Called at startup | ✓ `tui_session.py:158` | ✓ `mode_manager.py:106` (after fix) |
| User file format | `WorkflowPlugin` subclass | `Mode` instance via `MODE`/`MODES` |
| Override rule | Project over user-global | Project over user-global over builtins |
| Bound to modes via | `mode_bindings = [...]` | N/A — the mode IS the binding |
| Dep check | No | `DEPENDENCIES = [...]` |
