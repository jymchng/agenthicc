---
name: authoring-workflows
version: 1.0.0
tags: [workflows, plugins, phasespec, authoring, registry, checkpoints]
description: >-
  Author, register, and debug agenthicc workflows: the WorkflowPlugin contract,
  PhaseSpec fields and transition gates, custom runners and checkpoint hooks,
  discovery order and precedence, and how to validate a generated workflow.
---

# Skill: Authoring Workflows

A workflow is a named, multi-phase agent run. Each phase is one bounded agent
turn (or a set of turns) with its own agent role, tool-capability ceiling, and
transition rule. This skill covers the contract you implement, how agenthicc
finds your workflow, and how to prove it works.

## When to use this skill

Use this skill when you need to:

- Add a new workflow to a project or to your user-global configuration
- Understand which `PhaseSpec` field controls a phase's behaviour
- Decide whether you need a custom runner or can rely on the declarative one
- Diagnose why your workflow is not offered by `/workflow` or `workflows list`
- Make a workflow resumable across restarts

Do **not** use this skill for running an existing workflow headlessly — see
`headless-mode`. For the modes and approvals a phase can use, see
`security-modes-and-approvals`.

---

## The plugin contract

A workflow is a subclass of `WorkflowPlugin`
(`src/agenthicc/workflows/plugin.py:457`). You declare identity and structure as
class attributes and override factory classmethods to return specialised
objects.

```python
from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin


class ReleaseNotesParams(WorkflowParams):
    """Tunable parameters; each field maps to a [workflows.release_notes] key."""

    draft_model: str = ""


class ReleaseNotes(WorkflowPlugin):
    name = "release_notes"
    description = "Draft -> Review (summarise changelog entries)"
    mode_bindings = ["Plan"]
    phases = [
        PhaseSpec(
            name="draft",
            agent_type="auto",
            max_turns=20,
            next="review",
            on_reject="draft",
            max_iterations=10,
        ),
        PhaseSpec(
            name="review",
            agent_type="auto",
            max_turns=8,
            on_reject="draft",
        ),
    ]

    @classmethod
    def build_params(cls, source):
        return ReleaseNotesParams(draft_model=str(source.get("draft_model", "")))
```

### Class attributes

| Attribute | Purpose |
|---|---|
| `name` | Registry key. Lower snake case by convention (`code_plan`, `make_book`) |
| `description` | One-line summary shown by `/workflows` and `workflows list` |
| `mode_bindings` | Modes that offer this workflow. Empty means manual-only |
| `phases` | Ordered `PhaseSpec` list defining the graph |
| `max_total_phase_runs` | Hard ceiling on total phase runs; `0` means no cap |
| `required_startup_phases` | Readiness phases that must succeed before the first turn |

`required_startup_phases` is for hard dependencies. The docstring is explicit
that optional integrations must not be listed, because their failure would then
break unrelated local turns. Built-in examples include `mcp` and `extensions`.

### Factory classmethods

Override only what you need. All three default to generic behaviour:

| Method | Default | Override when |
|---|---|---|
| `build_runner(config, mode_manager)` (`workflows/plugin.py:519`) | Generic `WorkflowRunner` driven by `phases` | Your phases need retries, loops, conditional routing, or phase-local tools |
| `build_params(source)` (`workflows/plugin.py:534`) | `WorkflowParams()` | You want typed per-phase model overrides |
| `create_initial_context(intent, run_id, memory)` (`workflows/plugin.py:543`) | `None` | You want typed state before the runner is constructed |

Query helpers you do not override: `first_phase()`, `get_phase(name)`,
`phase_names()` (`workflows/plugin.py:486-496`).

---

## `PhaseSpec` in full

`PhaseSpec` is a frozen dataclass (`src/agenthicc/workflows/plugin.py:226`).
The transition fields are the ones that decide whether a phase actually ends.

| Field | Default | Meaning |
|---|---|---|
| `name` | required | Unique within the workflow; used as the `next`/`on_reject` target |
| `agent_type` | `"auto"` | Key into the agents registry selecting prompt and role defaults |
| `system_prompt_override` | `""` | Phase-specific prompt seed added to the *dynamic* region |
| `mode_override` | `None` | Mode to activate for this phase, e.g. `"Yolo"` to permit writes |
| `allowed_capabilities` | `None` | Capability ceiling; `None` falls back to the role default then the mode ceiling |
| `allowed_capabilities_override` | `None` | Per-instance override; wins over the two sources above |
| `max_turns` | `20` | Maximum LLM sub-turns inside one phase run |
| `output_schema` | `None` | Named schema used to parse structured output (`plan`, `review_result`, `free_text`) |
| `next` | `None` | Phase on success; `None` ends the workflow |
| `on_reject` | `None` | Phase to run when the output has `approved=False`; this is how retry loops are built |
| `on_error` | `None` | Reserved; not yet used |
| `max_iterations` | `-1` | Per-phase entry ceiling; `-1` is unlimited |
| `require_explicit_completion` | `False` | Loop until the phase's completion tool fires |
| `require_plan_finalization` | `False` | Loop until `finalize_plan()` is called |
| `require_explicit_review` | `False` | Require `approve_review()`/`reject_review()` instead of parsing prose |
| `parallel_with` | `()` | Sibling phases to run concurrently via `asyncio.gather` |
| `terminal_wait_policy` | `"foreground"` | `foreground` or `background` terminal default for the phase |
| `command_lifecycle` | `"oneshot"` | `oneshot` or `service` |
| `require_successful_commands` | `False` | Gate the transition on successful command outcomes |
| `require_readiness` | `False` | Require a successful service readiness result before transitioning |

The three "require_*_completion" flags exist because prose is not a transition
signal. `require_explicit_review` is documented as replacing the brittle XML
parse where "The code is approved" was misread as a rejection.

### Validation in `__post_init__`

`PhaseSpec` validates its own consistency and raises `ValueError`
(`workflows/plugin.py:333-341`):

- `terminal_wait_policy` must be `foreground` or `background`
- `command_lifecycle` must be `oneshot` or `service`
- `command_lifecycle="service"` requires `terminal_wait_policy="background"`
- `require_readiness=True` requires `command_lifecycle="service"`

Two traps follow. A background terminal policy makes `run_bash`/`run_command`
return an owned handle rather than a result, so the phase must call
`wait_terminal`. And `require_explicit_completion` is described as capping
continuation turns (default 10 when `max_iterations` is `-1`).

### A worked example from a built-in

`code_plan` (`src/agenthicc/workflows/code_plan/definition.py:48`) declares
`mode_bindings = ["Plan"]` and four phases. Its `execute` phase sets
`require_explicit_completion=True` and `require_successful_commands=True`, and
deliberately leaves `mode_override=None` with a comment explaining why: the plan
review handoff chooses `Safe` or `Yolo` at runtime, and a static override would
clobber that decision when generic code inspects the metadata.

---

## Discovery and precedence

`build_workflow_registry` (`src/agenthicc/workflows/registry.py:182`) loads
three tiers in order:

| Tier | Location | `source` |
|---|---|---|
| Built-in | packaged with agenthicc | `builtin` |
| User | `~/.agenthicc/workflows` | `user` |
| Project | `.agenthicc/workflows` | `project` |

Later tiers win, matching the skills loader's precedence model. The published
directory name is fixed at `.agenthicc/workflows`
(`src/agenthicc/workflows/create_workflow/runner.py:85`).

Built-ins are registered lazily as descriptors
(`src/agenthicc/workflows/loader.py:79`). External directories are scanned by
`_scan_workflow_dir` (`workflows/registry.py:219`), which sorts files before packages and
skips anything whose name starts with `_` or `.`. That skip is deliberate:
drafts, publication backups, and half-written rename directories must never be
executed during a loader refresh.

`load_python_workflows` (`src/agenthicc/workflows/loader.py:100`) accepts
`WorkflowPlugin` subclasses that have a **non-empty `name`** and returns `[]`
with a logged warning if import fails. A silent "my workflow is not listed"
almost always means either an empty `name`, a filename starting with `_`, or an
import error that was swallowed into that warning.

Check what the registry actually sees:

```bash
agenthicc workflows list
```

That prints each workflow with its source tier, description, phases, and modes.
For a single intent there is also:

```bash
agenthicc workflows run code_plan --intent "add a glossary page" --json
```

`workflows run` maps its parameters straight onto argparse
(`src/agenthicc/cli/commands/workflows.py:98`): `workflow_name` has no default so
it is positional, `intent` becomes `--intent TEXT`, and `json` becomes `--json`.

In the TUI, `/workflows` lists runs and phases, `/workflows reload` re-scans, and
`/workflow <name>` switches (`src/agenthicc/commands/builtins.py:932`, `:1048`).
A workflow with empty `mode_bindings` is manual-only: it appears in `/workflow`
but is not offered by a mode.

---

## Custom runners and checkpoints

`BaseWorkflowRunner` (`src/agenthicc/workflows/base_runner.py:8`) defines two
methods: `run(intent)` and `resume(context)`. You need a real runner when a bare
`PhaseSpec` graph cannot express what you want. The authoring tooling states the
trigger list plainly
(`src/agenthicc/workflows/create_workflow/inspection_tools.py:1023`):

- any phase that must retry until a specific tool is called
- any conditional or looping transition (approve -> next, reject -> back)
- context accumulated across phases and injected into later prompts
- phase-local tools that do not belong in the session tool set
- a human approval gate that blocks the handoff
- bounded failure handling with an explicit terminal state

The same source lists required elements: a typed `State(Enum)` with an
`is_terminal` property, a `@dataclass` context, one bounded async method per
non-terminal state, a `run(intent)` dispatch loop, and a `resume(context)` that
re-enters that same path. It is explicit that `resume` must never call
`run(context.intent)` for a recoverable checkpoint.

### Checkpoint codecs

Resumability is opt-in through two hooks
(`workflows/plugin.py:562`, `workflows/plugin.py:576`):

- `checkpoint_context_to_payload(context)` returns bounded JSON-compatible
  fields
- `checkpoint_context_from_payload(payload, memory=None)` restores them

The inherited `None` is documented as a **fail-closed default for unsupported
third-party contexts**, not a declaration that the workflow is resumable. Live
resources — session memory, locks, events, clients — must never be serialised;
`memory` is passed in already open and must be reattached rather than rebuilt.
Override both hooks together.

If your workflow filters, skips, generates, or reorders phases, you must also
override `resolve_checkpoint_topology(context_payload)`
(`workflows/plugin.py:501`). The default derives the graph from the static `phases` list,
which would mis-map checkpoint indexes onto a profile-filtered graph.

### Prompt-cache stability

The framework splits prompts into a stable prefix and a dynamic region.
`CACHE_CONTRACT` (`src/agenthicc/workflows/code_plan/runner.py:115`) is the
canonical pattern: immutable workflow policy in a literal class constant, phase
state and artifacts in the dynamic context. `system_prompt_override` lands in
the dynamic region for exactly this reason, so changing phase does not
invalidate the cached prefix.

---

## Validating a generated workflow

If you use the `create_workflow` meta-workflow you get two checks for free.

`ValidationReport` (`src/agenthicc/workflows/create_workflow/validation.py:232`)
imports the generated package deterministically and inspects the plugin, phases,
capabilities, runner, checkpoint contract, topology contract, and error-recovery
contract. `run_generated_workflow_smoke`
(`src/agenthicc/workflows/create_workflow/smoke.py:350`) goes further and
actually exercises the plugin with a faked config.

For a hand-written workflow the cheap equivalent is to build the registry and
assert your plugin loaded:

```bash
python - <<'PY'
from pathlib import Path
from agenthicc.workflows.registry import build_workflow_registry

registry = build_workflow_registry(project_dir=Path(".agenthicc"),
                                  user_dir=Path("~/.agenthicc").expanduser())
print(registry.names())
plugin = registry.get("release_notes")
print(plugin, plugin.phase_names() if plugin else "NOT LOADED")
PY
```

If `plugin` is `None`, check the three causes above before touching the network
of phase definitions.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Workflow absent from `workflows list` | `name` is empty, or the file/dir starts with `_` or `.` | Give it a non-empty `name` and a normal filename |
| Workflow absent with no error | Import failure swallowed into a logged warning | Import the module directly to surface the traceback |
| `ValueError: service command_lifecycle requires terminal_wait_policy='background'` | Inconsistent `PhaseSpec` | Pair `command_lifecycle="service"` with a background wait policy |
| `ValueError: require_readiness requires command_lifecycle='service'` | `require_readiness=True` on a one-shot phase | Switch to a service lifecycle or drop the flag |
| Phase never advances although the agent says it is done | Transitions are tool-driven, not prose-driven | Call the phase's transition tool, or set `require_explicit_completion` |
| Background phase returns a handle instead of output | Background terminals are owned handles by design | Call `wait_terminal` in the phase |
| Resume restarts the workflow from the beginning | `resume` called `run(context.intent)` | Re-enter the state dispatch path instead |
| Checkpoints restore the wrong phase | Computed phase graph with the default topology resolver | Override `resolve_checkpoint_topology` |

---

## Key points

- Subclass `WorkflowPlugin` (`workflows/plugin.py:457`); declare `name`, `description`,
  `mode_bindings`, and `phases`.
- `PhaseSpec` (`workflows/plugin.py:226`) validates itself; `service` lifecycle demands a
  background wait policy, and `require_readiness` demands a `service` lifecycle.
- Phase transitions happen through tools, never through prose. The
  `require_*_completion` flags exist to enforce that.
- Empty `mode_bindings` means manual-only: reachable via `/workflow`, invisible
  to mode cycling.
- Discovery order is built-in, then `~/.agenthicc/workflows`, then
  `.agenthicc/workflows`, with later tiers winning.
- Files starting with `_` or `.` are never scanned.
- Checkpoint support is opt-in; the inherited `None` is a fail-closed default,
  not a promise of resumability.
- Override `resolve_checkpoint_topology` whenever the runner computes its own
  phase graph.
