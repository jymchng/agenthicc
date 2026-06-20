# PRD-111 — Per-Workflow Tunable Parameters (`WorkflowParams`)

## Summary

Each `WorkflowPlugin` may declare a typed `WorkflowParams` subclass that holds
its tunable parameters (phase model overrides, turn limits, etc.).  Parameters
are loaded from TOML, CLI `--set`, and environment variables using the existing
config layering model and flow to the runner via `WorkflowConfig`.

The primary use case is per-phase model selection: the execute phase of
`code_plan` can be run with a cheaper model than the plan or review phases,
reducing cost without sacrificing plan quality.

---

## Motivation

All workflow phases currently use the single global model from
`[execution] model`.  There is no way to configure the execute phase of
`code_plan` to use a cheaper model, even though execution is a mechanical
task that does not require a flagship model.

---

## Design

### Separation of concerns

| Object | Role |
|---|---|
| `WorkflowConfig` | Session-scoped infrastructure (agent_runner, processor, conv_store, …). Unchanged. |
| `WorkflowParams` | Per-workflow tunable parameters (phase models, future: per-phase turn limits). New. |

`WorkflowParams` travels alongside `WorkflowConfig` in a single new field:
`WorkflowConfig.params: WorkflowParams | None`.

### `WorkflowParams` base class (`workflows/plugin.py`)

```python
@dataclass
class WorkflowParams:
    phase_models: dict[str, str] = field(default_factory=dict)

    def model_for_phase(self, phase_name: str, fallback: str) -> str:
        m = self.phase_models.get(phase_name, "")
        return m if m else fallback
```

Empty string in `phase_models` means "use the global model".

### Specialised params class per plugin (optional)

```python
class CodePlan(WorkflowPlugin):
    @dataclass
    class Params(WorkflowParams):
        plan_model:    str = ""
        execute_model: str = ""   # cheaper model for execute phase
        review_model:  str = ""
        summary_model: str = ""

        @property
        def phase_models(self) -> dict[str, str]:
            return {"plan": self.plan_model, "execute": self.execute_model,
                    "review": self.review_model, "summarize": self.summary_model}
```

### `params_factory` classmethod on `WorkflowPlugin` (mirrors `runner_factory`)

```python
class WorkflowPlugin:
    @classmethod
    def params_factory(cls, source: dict[str, Any]) -> WorkflowParams:
        return WorkflowParams()   # default: no overrides
```

`CodePlan` overrides it to construct `CodePlan.Params` from the source dict.

### `build_params(source)` on `WorkflowDefinition`

`to_definition()` binds the class's `params_factory` on the definition (same
pattern as `runner_factory`). `build_params(source)` calls it:

```python
def build_params(self, source: dict[str, Any]) -> WorkflowParams:
    if self.params_factory is not None:
        return self.params_factory(source)
    return WorkflowParams()
```

### `AgenthiccConfig.workflows` — TOML source

```toml
[workflows.code_plan]
execute_model = "claude-haiku-4-5"
plan_model    = ""   # empty → use execution.model
```

`AgenthiccConfig` gains a `workflows: dict[str, dict[str, Any]]` field,
populated by `_dict_to_config()` from the `[workflows]` TOML section.

### Session startup — build and store params

```python
wf_params = _wf_defn.build_params(ctx.cfg.workflows.get(_wf_defn.name, {}))
self._wf_config_base = WorkflowConfig(..., params=wf_params)
```

### Runner — per-phase model override

In `WorkflowRunner._run_phase()`, before building `_turn_kwargs`:

```python
_phase_model = (
    self._cfg.params.model_for_phase(spec.name, self._model_id)
    if self._cfg.params else self._model_id
)
_turn_kwargs = dict(
    ...
    exec_cfg=dataclasses.replace(self._cfg.cfg.execution, model=_phase_model),
)
```

`AgentTurnRunner._resolve_model()` reads `exec_cfg.model` and uses it in
`@agent_decorator(model=...)`.  No changes to `AgentTurnRunner`.

---

## Configuration Sources (priority order)

1. `--set workflows.code_plan.execute_model=claude-haiku-4-5` (CLI — highest)
2. `.agenthicc/agenthicc.toml` `[workflows.code_plan]` (project)
3. `~/.agenthicc/agenthicc.toml` `[workflows.code_plan]` (user)
4. `Params` dataclass field defaults (lowest)

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `WorkflowParams` base dataclass with `phase_models` dict and `model_for_phase()`. |
| 2 | `WorkflowPlugin.params_factory(source)` returns `WorkflowParams()` by default. |
| 3 | `CodePlan.Params` specialises `WorkflowParams` with named per-phase model fields. |
| 4 | `CodePlan.params_factory(source)` constructs `CodePlan.Params` from the source dict. |
| 5 | `WorkflowDefinition.params_factory` field carries the bound factory. |
| 6 | `WorkflowDefinition.build_params(source)` delegates to the factory or returns base params. |
| 7 | `WorkflowPlugin.to_definition()` stores `type(self).params_factory` on the definition. |
| 8 | `WorkflowConfig.params: WorkflowParams \| None` field added. |
| 9 | `AgenthiccConfig.workflows: dict[str, dict[str, Any]]` field added. |
| 10 | `_dict_to_config()` reads the `[workflows]` TOML section into `AgenthiccConfig.workflows`. |
| 11 | `TUISession.__init__` calls `build_params(cfg.workflows.get(name, {}))` and passes params to `WorkflowConfig`. |
| 12 | `WorkflowRunner._run_phase()` resolves the per-phase model via `params.model_for_phase()`. |
| 13 | Empty `execute_model = ""` in TOML falls back to the global `execution.model`. |
| 14 | Workflows without a `params_factory` override get `WorkflowParams()` with no overrides. |

---

## Files Changed

| File | Change |
|---|---|
| `workflows/plugin.py` | `WorkflowParams` base; `params_factory` classmethod on `WorkflowPlugin`; `params_factory` field + `build_params()` on `WorkflowDefinition`; `to_definition()` updated |
| `workflows/builtins.py` | `CodePlan.Params` dataclass; `CodePlan.params_factory()` override |
| `workflows/config.py` | `WorkflowConfig.params: WorkflowParams \| None` field |
| `config.py` | `AgenthiccConfig.workflows: dict[str, dict[str, Any]]` field; `_dict_to_config()` reads `[workflows]` section |
| `runners/tui_session.py` | Build params and pass to `WorkflowConfig` |
| `workflows/runner.py` | Apply per-phase model override via `dataclasses.replace(exec_cfg, model=…)` |
