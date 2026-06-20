# PRD-116 — Remove `WorkflowDefinition`: `WorkflowPlugin` as the Registry Artifact

## Summary

`WorkflowDefinition` is a redundant intermediary.  Every field it carries is
already present as a class attribute on `WorkflowPlugin`.  Its factory callables
(`runner_factory`, `params_factory`) are replaced by proper classmethods on
`WorkflowPlugin`.  After this change the registry stores plugin *classes*
directly; `WorkflowDefinition` is deleted entirely.

---

## Motivation

`WorkflowDefinition` was created so the registry could store a data snapshot
independent of the plugin class.  In practice:

- All TUI-visible fields (`name`, `phases`, `mode_bindings`, `max_total_phase_runs`)
  are class attributes on `WorkflowPlugin`.
- The two factory callables (`runner_factory`, `params_factory`) are stored as
  classmethods-used-as-data — a fragile pattern that produces a `# type: ignore`
  in `CodePlan.runner_factory` and confuses `defn` with `cls`.
- `to_definition()` on `WorkflowPlugin` serves no purpose beyond creating an
  object that re-packages what the class already holds.
- The registry's `build_workflow_registry()` calls `cls().to_definition()`
  (instantiating a plugin just to extract its class attributes).

The result is two parallel sources of truth that must stay in sync.

---

## Design

### `WorkflowPlugin` gains factory classmethods and query helpers

```python
class WorkflowPlugin:
    name:                 str            = ""
    description:          str            = ""
    mode_bindings:        list[str]      = []
    phases:               list[PhaseSpec] = []
    max_total_phase_runs: int            = 0   # moved from WorkflowDefinition

    @classmethod
    def first_phase(cls) -> PhaseSpec | None: ...
    @classmethod
    def get_phase(cls, name: str) -> PhaseSpec | None: ...
    @classmethod
    def phase_names(cls) -> list[str]: ...

    @classmethod
    def build_runner(cls, config: WorkflowConfig,
                     mode_manager: ModeManager | None) -> BaseWorkflowRunner: ...
    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams: ...
```

`to_definition()`, `runner_factory`, `params_factory`, and `determine_transition()`
are deleted.

### `WorkflowEntry` — thin provenance record

```python
@dataclass(frozen=True)
class WorkflowEntry:
    plugin_cls: type[WorkflowPlugin]
    source:     str         # "builtin" | "user" | "project"
    path:       str | None  # filesystem path for user/project plugins
```

### `WorkflowRegistry` stores `WorkflowEntry` objects

```python
registry.register(plugin_cls, source="builtin")
registry.get(name) -> type[WorkflowPlugin] | None
registry.get_entry(name) -> WorkflowEntry | None
```

### `WorkflowRunner` takes `type[WorkflowPlugin]` instead of `WorkflowDefinition`

```python
WorkflowRunner(plugin_cls, config, mode_manager)
# self._plugin = plugin_cls  (was self._def = definition)
```

All `self._def.xxx` calls become `self._plugin.xxx` (class attributes / classmethods).

### `loader.py` returns `list[type[WorkflowPlugin]]`

### `tui_session.py` calls `plugin_cls.build_*()` directly

```python
plugin_cls = ctx.workflow_registry.get(wf_name)
wf_params  = plugin_cls.build_params(cfg.workflows.get(wf_name, {}))
runner     = plugin_cls.build_runner(wf_config, ctx.mode_manager)
```

### Deleted

- `WorkflowDefinition` dataclass
- `WorkflowPlugin.to_definition()`
- `WorkflowPlugin.runner_factory` classmethod
- `WorkflowPlugin.params_factory` classmethod
- `WorkflowPlugin.determine_transition()` (dead code)
- `WorkflowDefinition.build_runner()` / `build_params()`
- `workflows/runner.py` (backward-compat shim — no callers)
- `workflows/builtins.py` (backward-compat shim — no callers)
- `build_workflow_runner()` orphan factory (no callers)

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `WorkflowDefinition` does not exist anywhere in the codebase. |
| 2 | `WorkflowRegistry.get(name)` returns `type[WorkflowPlugin] \| None`. |
| 3 | `WorkflowRunner.__init__` takes `type[WorkflowPlugin]` as first arg. |
| 4 | `WorkflowPlugin.build_runner()` and `build_params()` are the factory classmethods. |
| 5 | `WorkflowPlugin.max_total_phase_runs` class attribute replaces the field on `WorkflowDefinition`. |
| 6 | `WorkflowPlugin.first_phase()`, `get_phase()`, `phase_names()` are classmethods. |
| 7 | `loader.py` returns `list[type[WorkflowPlugin]]` with no `to_definition()` call. |
| 8 | `tui_session.py` calls `plugin_cls.build_params()` and `plugin_cls.build_runner()`. |
| 9 | No `typing.Any` in any changed file. |
| 10 | `workflows/runner.py`, `workflows/builtins.py`, `build_workflow_runner()` are deleted. |
| 11 | All existing tests pass after updating test fixtures. |
