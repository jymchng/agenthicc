# Custom workflows and TOML configuration

This guide shows how to define a project workflow and configure its phase
models. The workflow graph is Python; values that vary between projects or
environments belong in `agenthicc.toml`.

| Concern | Configure it in |
|---|---|
| Phase order, transitions, roles, tools, and turn limits | `PhaseSpec` in a Python workflow plugin |
| Workflow-specific values such as phase model IDs | `[workflows.<name>]` in TOML, read by `build_params()` |
| Provider profiles, endpoint, credentials, and request options | `[providers.<name>]` plus `[execution].profile` |

## 1. Create the project layout

Put the workflow and its configuration in the project:

```text
my-project/
├── .agenthicc/
│   ├── agenthicc.toml
│   └── workflows/
│       └── release_review/
│           ├── runner.py
│           └── tools.py        # optional workflow-local tools
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
    mode_bindings = ["Yolo", "Plan"]
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

### Phase prompt semantics

For this inherited generic runner, `system_prompt_override` is the phase's
role-prompt override:

```python
PhaseSpec(
    name="verify",
    agent_type="reviewer",
    system_prompt_override=(
        "You are in VERIFY. Inspect the artifact, run the checks, and call "
        "approve_review(summary) or reject_review(reason)."
    ),
)
```

The non-empty override replaces the `AgentsRegistry` prompt for the selected
role. The runtime still adds the global/base system prompt, requirements
clarification policy, transition-tool instructions, capability filtering, and
the current phase context. With the prompt-cache contract enabled, the phase
override is dynamic context rather than part of the stable system prefix.

If you implement `build_runner()` and write phase functions yourself, the
phase function is the prompt owner instead:

```python
await self.run_phase(
    intent=context.intent,
    text=phase_context,
    system_prompt="You are in VERIFY. Check the generated artifact.",
    stable_system_prompt=CACHE_CONTRACT,
    shared_memory=context.shared_memory,
    tools=phase_tools,
)
```

`run_phase(system_prompt=...)` does not automatically consult
`PhaseSpec.system_prompt_override`; the explicit argument wins. This prevents
metadata from silently overriding a specialized state machine. If the custom
runner wants one source of truth, it must explicitly read the spec and pass its
override into `run_phase()`. Keep phase state, artifacts, questions, answers,
and summaries in `text`/dynamic context, not in `CACHE_CONTRACT`.

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
and writes nothing. The generate phase writes a complete package to a run-owned
draft at `.agenthicc/workflows/.drafts/<run-id>/<name>/`, with
`runner.py` and optional workflow-local helpers. It records a deterministic
manifest (relative paths, sizes, line counts, and hashes), and repair cycles
reuse that same draft. The normal registry ignores drafts, so partial source
cannot become runnable accidentally.

The authoring runner also gives the agent a live, bounded, redacted snapshot of
the effective tool catalog, phase capabilities, mode filtering, workspace and
cache contracts, and browser/MCP availability. The snapshot is fingerprinted
for checkpoint provenance; secrets, headers, prompt contents, and tool
arguments are not included. The inspection tools
`describe_authoring_session()` and `explain_authoring_tool_access(name)` expose
the same snapshot and its availability decisions.

The validate phase first imports and checks the draft the way the loader will,
then runs a bounded fake-provider smoke contract. For a custom runner this
checks event-backed transitions, prose-only non-transition, checkpoint JSON
round-tripping with memory reattachment, resume at the saved state, and error
propagation without network/browser/MCP calls. Only after deterministic
validation, smoke success, and `approve_workflow(summary)` does the framework
atomically publish the package to `.agenthicc/workflows/<name>/`. Existing
published packages are backed up and restored if publication fails; the draft
and checkpoint remain recoverable.

Every generated custom runner must use the cache-stable `CACHE_CONTRACT`, pass
it as `stable_system_prompt` to `CodePlanRunner.run_phase()`, keep phase state
and artifacts dynamic, use the parent session's `conversation_id`, memory,
workspace policy, and browser/MCP tools, and ask the user focused questions for
material ambiguity instead of guessing. It must provide JSON-compatible
checkpoint codecs; checkpoint contexts have no framework-imposed serialized
byte ceiling. The framework still validates JSON shape and rejects unsupported
runtime objects, and the filesystem remains the practical capacity boundary.
It must re-raise ordinary errors to the framework failure finalizer. A
simple unconditional graph may use the inherited generic runner. The generated
TOML is a template for you to copy into `.agenthicc/agenthicc.toml`; the
authoring workflow does not silently modify configuration files or write
secrets.

When a generated workflow is used as a downstream stage of `reconstruct_site`,
it should treat the reconstruct evidence manifest as an external artifact
reference: carry its path, revision, and content hashes in typed context, and
rehydrate/verify them before use. Keep those references in the dynamic phase
context, not in `CACHE_CONTRACT` or a stable tool schema. This preserves the
same prompt-cache epoch and lets the generated workflow resume with the
session's existing conversation and memory without duplicating research bodies.

### Optional integration declarations

If a generated plugin uses an optional service, declare its dependency on the
plugin rather than relying on a prompt convention:

```python
required_integrations = ("playwright",)       # validation fails if unavailable
optional_integrations = ("mcp",)             # unavailable is reported as degraded
integration_fallbacks = {"playwright": "text-only report"}
```

Use `cloakbrowser`, `playwright`, `mcp`, or `mcp:<server>` as names. The
validation report records safe integration states without URLs, headers, or
credentials. A missing required integration must have a declared, usable
fallback or the package is rejected with installation/configuration guidance.

## 4. Choose a provider

Provider selection is session-wide and profile-aware:

```toml
[execution]
profile = "modal_kimi"

[providers.modal_kimi]
provider = "openai" # native or OpenAI-compatible endpoint
model = "your-default-model"
base_url = "https://your-endpoint.modal.run/v1"
api_key_env = "MODAL_API_KEY"

[providers.modal_kimi.request_options.provider]
reasoning_effort = "none"
```

The profile is resolved once when a session starts and is inherited by every
phase, including generated custom workflows and subagents. A resumed workflow
stores only the profile name; the current environment is consulted again for
rotated secrets. `provider = "openai"` is the OpenAI-compatible adapter and
does not require a Modal-specific SDK.

Per-phase model selection is supported, but per-phase provider/profile selection
is not. A setting such as
`[workflows.release_review.phases.plan].provider` is not consumed by the
current runner. The session creates one provider transport and phase overrides
replace its model only; the profile's endpoint, headers, request options, and
credentials remain session-wide.

If phases must use different provider backends, use one of these patterns:

1. Run separate workflow invocations with different `[execution].profile` settings.
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
`/workflows reload` command reloads Python workflow files/packages, not the TOML
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
