# Custom workflows and TOML configuration

This guide shows how to define a project workflow and configure its phase
models. The workflow graph is Python; values that vary between projects or
environments belong in `agenthicc.toml`.

| Concern | Configure it in |
|---|---|
| Phase order, transitions, roles, tools, and turn limits | `PhaseSpec` in a Python workflow plugin |
| Workflow-specific values such as phase model IDs | `[workflows.<name>]` in TOML, read by `build_params()` |
| Provider, base URL, credentials, and session defaults | `[execution]` in TOML or provider environment variables |

## 1. Create the project layout

Put the workflow and its configuration in the project:

```text
my-project/
├── .agenthicc/
│   ├── agenthicc.toml
│   └── workflows/
│       └── release_review.py
└── ...
```

Workflows can also be installed globally in `~/.agenthicc/workflows/`. The
discovery order is built-ins, user-global plugins, then project-local plugins;
a later workflow with the same `name` replaces an earlier one. Files whose
names start with `_` are ignored. See the [workflow guide](workflows.md) for
the complete discovery and trust model.

## 2. Define the workflow phases

The default runner uses `PhaseSpec` to execute a phase graph. This example has
three phases and gives each phase a stable name that can be used by its model
configuration:

```python
"""Project-local release review workflow."""

from collections.abc import Mapping
from dataclasses import dataclass

from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin


@dataclass
class ReleaseReviewParams(WorkflowParams):
    plan_model: str = ""
    execute_model: str = ""
    verify_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        return {
            "plan": self.plan_model,
            "execute": self.execute_model,
            "verify": self.verify_model,
        }


def _string(source: Mapping[str, object], key: str) -> str:
    value = source.get(key, "")
    return value.strip() if isinstance(value, str) else ""


class ReleaseReview(WorkflowPlugin):
    name = "release_review"
    description = "Plan a release, implement the work, and verify the result."
    mode_bindings = ["Auto", "Plan"]
    phases = [
        PhaseSpec(name="plan", agent_type="planner", max_turns=12, next="execute"),
        PhaseSpec(name="execute", agent_type="executor", max_turns=40, next="verify"),
        PhaseSpec(name="verify", agent_type="reviewer", max_turns=16),
    ]

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> WorkflowParams:
        return ReleaseReviewParams(
            plan_model=_string(source, "plan_model"),
            execute_model=_string(source, "execute_model"),
            verify_model=_string(source, "verify_model"),
        )
```

`build_params()` receives the merged `[workflows.release_review]` table.
The `WorkflowParams.model_for_phase()` method uses a configured model when
its value is non-empty and otherwise falls back to `[execution].model`.

The default `WorkflowPlugin.build_runner()` is sufficient for this example.
Use a custom `build_runner()` only when the workflow needs execution logic
that the generic phase runner does not provide. A custom parameter is only
useful if the selected runner reads it; adding an arbitrary TOML key does not
change runtime behaviour by itself.

## 3. Configure the workflow in TOML

Create `.agenthicc/agenthicc.toml`:

```toml
[execution]
provider = "anthropic"
model = "claude-sonnet-4-5"
max_agent_turns = 200

[workflows.release_review]
# Empty means: use [execution].model for that phase.
plan_model = "claude-sonnet-4-5"
execute_model = "claude-haiku-4-5"
verify_model = "claude-sonnet-4-5"
```

This lets the inexpensive execution phase use a different model while the
planning and verification phases use the session model. Keep API keys and
tokens in the provider environment instead:

```bash
export ANTHROPIC_API_KEY="..."
```

The built-in `code_plan` workflow uses the same mechanism:

```toml
[workflows.code_plan]
plan_model = ""
execute_model = "claude-haiku-4-5"
review_model = "claude-sonnet-4-5"
summary_model = ""
```

Its accepted parameter names are `plan_model`, `execute_model`,
`review_model`, and `summary_model`. Custom workflows choose their own
names and map them to phase names in `get_phase_models()`.

## Use `create_workflow` to author this shape

The built-in authoring workflow can generate the Python side of this design
from a natural-language request:

```text
/workflow create_workflow
Create a release workflow with plan, execute, and verify phases. Use a custom
runner, make the execute model configurable through TOML, and include the
copy-ready configuration template in the module documentation.
```

The design phase presents the workflow's name and phase graph for your approval
and writes nothing. The generate phase then writes the complete Python source to
`.agenthicc/workflows/<name>.py`, giving every phase a literal
`system_prompt_override` so the runtime agent knows the objective, tools, inputs,
outputs, verification, completion signal, and handoff. It uses the inherited
generic runner when the phase graph is enough; `build_runner()` and a custom
runner are reserved for genuine orchestration. The validate phase imports the
written file the way the loader will and loops back to generate until it loads
cleanly and its phase graph resolves; nothing is staged, published, or
approved on your behalf. The generated TOML is a template for you to copy into
`.agenthicc/agenthicc.toml`; the authoring workflow does not silently modify
configuration files or write secrets.

## 4. Choose a provider

Provider selection is currently session-wide:

```toml
[execution]
provider = "openai" # anthropic, openai, ollama, or litellm
model = "your-default-model"
base_url = ""
```

Per-phase model selection is supported, but per-phase provider selection is
not. A setting such as
`[workflows.release_review.phases.plan].provider` is not consumed by the
current runner. The session creates one provider transport and phase
overrides replace its model only; `base_url` and credentials are also
session-wide.

If phases must use different provider backends, use one of these patterns:

1. Run separate workflow invocations with different `[execution]` settings.
2. Put a LiteLLM gateway behind `provider = "litellm"` and use the routed
   model IDs exposed by that gateway in the phase model fields.
3. Keep one provider for the workflow and vary only the model per phase.

## 5. Run and reload the workflow

Check discovery before running it:

```bash
uv run agenthicc workflows list --json
```

Run it headlessly:

```bash
uv run agenthicc workflows run release_review \
  --intent "Prepare the release and verify the tests" \
  --json
```

In the TUI, select it with:

```text
/workflow release_review
```

After editing a plugin file, reload the registry in the active session:

```text
/workflows reload
```

Restarting the session reloads both the workflow and configuration. The
`/workflows reload` command reloads Python workflow files, not the TOML
configuration.

You can keep a separate configuration file for a workflow or environment and
select it explicitly:

```toml
# .agenthicc/release.toml
extends = "agenthicc.toml"

[workflows.release_review]
plan_model = "claude-sonnet-4-5"
execute_model = "claude-haiku-4-5"
verify_model = "claude-sonnet-4-5"
```

```bash
uv run agenthicc --config .agenthicc/release.toml \
  workflows run release_review --intent "Prepare the release" --json
```

The same `--config` option selects the file for a TUI session. `extends` is
resolved relative to the file that declares it, so this pattern can share the
base project configuration without duplicating provider credentials.

## Configuration precedence and current limitations

Configuration is merged in this order, from lowest to highest priority:

1. Built-in defaults.
2. The first discovered user configuration under `~/.agenthicc/`.
3. The first discovered project configuration under `.agenthicc/`.
4. Environment overrides and provider shortcuts such as `OPENAI_MODEL`.
5. CLI `--set` overrides for supported configuration fields.

Tables are deep-merged; a higher-priority scalar or list replaces the lower
value. `extends = "..."` can share a base TOML file. For workflow-specific
parameters, use the TOML table shown above: the current generic environment
and `--set` parsers address top-level sections and do not reliably address
nested `[workflows.<name>]` keys.

| Requirement | Supported path |
|---|---|
| Define a workflow and its phase graph | Python `WorkflowPlugin` + `PhaseSpec` |
| Assign a phase role and capabilities | `PhaseSpec.agent_type` and capability fields |
| Assign a different model to each phase | `WorkflowParams` subclass + `[workflows.<name>]` |
| Select the provider | `[execution].provider` for the whole session |
| Select a different provider per phase | Not currently supported by the default runner |
| Change a phase's static turn limit | `PhaseSpec.max_turns` |
| Add custom runtime settings | Typed params plus a custom runner that consumes them |

For transitions, retries, parallel phases, resume state, and generic-runner
caveats, see [Workflows](workflows.md). For provider credentials,
configuration-file discovery, and security settings, see
[Configuration](configuration.md).
