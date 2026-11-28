---
title: "PRD-265: Make the UI/UX Pro Max Route Profile a Validated Enum Instead of a Free-Text Field"
status: Proposed
version: 1.0.0
date: 2026-11-28
repository: jymchng/agenthicc
priority: P1
related_prds: [prd-219, prd-245, prd-254, prd-264]
tags: [workflows, ui-ux-pro-max, checkpoint, phase-topology, diagnosability, tuple-index]
---

# PRD-265 — Make the UI/UX Pro Max Route Profile a Validated Enum Instead of a Free-Text Field

## Summary

A `ui_ux_pro_max` run aborts with an opaque exception **after a successful
turn**, on both attempts, and the paused run can no longer be resumed:

```text
✾ Worked for 4m 43s | 7:53 AM
  └ 26 tools · edited 1 file +178 −0
 ✗ ValueError: tuple.index(x): x not in tuple
```

The message names neither the workflow, the phase, nor the offending value. It
is `tuple.index` failing inside the workflow's own phase bookkeeping, and it is
**not** caused by the model's work: the turn completed, its edits were counted,
and its footer printed before the crash.

The chain, reproduced in § *Incident analysis*:

1. `complete_product_discovery(...)` takes `details` as a **free-form dict**
   (`phase_tools.py:47-53`). The runner then reads the route profile from
   `details["scope"]` (`runner.py:449`) — a key that neither the tool
   description nor the phase prompt names.
2. The model answered the question the prompt actually asked ("Classify the
   request as broad / targeted / accessibility_audit", `runner.py:99-101`) and
   submitted `{"classification": "broad", …}` — no `scope` key.
   `_str()` returns `""` for a missing key (`runner.py:1254-1256`), so
   `context.route_profile` became `""`.
3. `_product_discovery` decides the next state with
   `if context.route_profile == "broad"` (`runner.py:467`). `""` is not
   `"broad"`, so the run routed to `TARGETED_DESIGN_RESEARCH` even though the
   model classified it broad.
4. `_active_phases` resolves the phase tuple with
   `PHASES_BY_PROFILE.get(profile, PHASES_BY_PROFILE["broad"])`
   (`runner.py:1199-1200`). `""` is not a key, so it silently fell back to the
   **broad** tuple — which does not contain `targeted_design_research`.
5. `_checkpoint_boundary` then indexes that tuple without a guard
   (`runner.py:891`): `phases.index(next_phase)` → `ValueError`.

Two components therefore disagree about what an unknown profile means: the
*transition* treats it as "not broad", the *phase list* treats it as "broad".
Every non-canonical value (missing, `"Broad"`, `"broad (full redesign)"`,
a retired profile name) lands the runner in a state that is absent from the
phase tuple it was just handed.

The same disagreement is latent in the resume path (`runner.py:318`,
`runner.py:855`), and the crash is **terminal for the run**: the checkpoint
persists `route_profile: ""`, and `from_checkpoint_payload` rejects exactly
that value (`state.py:282-284`), so `/workflow resume` answers
`ValueError: ui_ux_pro_max checkpoint contains an unknown route profile`.

## Incident analysis and confirmed cause

### Evidence

Two runs in one TUI session (`/root/typescript_projects/pyapps`,
`source: tui`), recorded in
`~/.agenthicc/session-service/50d18253-753b-4a43-960d-b8dde550fc76.jsonl`:

| seq | time | kind | payload |
|---|---|---|---|
| 47 | 07:53:16 | `turn_complete` | `tools: 26, files: 1, added: 178, removed: 0, wrote: true` |
| 48 | 07:53:16 | `error` | `{"message": "ValueError: tuple.index(x): x not in tuple"}` |
| 49 | 07:53:16 | `workflow_paused_after_error` | `phase: product_discovery`, `failure_kind: workflow_error`, `run_id: 8dfc3276…`, `revision: 5` |
| 51 | 07:53:16 | `turn_failed` | `{}` |
| 90 | 08:09:59 | `turn_complete` | `tools: 19, files: 1, added: 159, removed: 135, wrote: true` |
| 91 | 08:09:59 | `error` | `{"message": "ValueError: tuple.index(x): x not in tuple"}` |
| 92 | 08:09:59 | `workflow_paused_after_error` | `phase: product_discovery`, `failure_kind: workflow_error`, `run_id: a2e1b1ef…`, `revision: 5` |

Both runs crash **after** `turn_complete` and **during** the
`product_discovery` → next-phase boundary, i.e. inside
`_drive` → `_checkpoint_boundary` (`runner.py:336-338`), never inside the tool
call itself. The `error` event has `turn_id: null`, which is why the row is
rendered *below* the completed-turn footer instead of replacing it.

The two `complete_product_discovery` submissions carried
`{"classification": "broad", …}` and no `scope` key; `route_profile` in the
checkpoint written by the same phase is `''`:

```text
~/.agenthicc/sessions/50d18253-…/workflows/8dfc3276239e4bb8a61eb2bff105241d/checkpoint.json
  /context/fields/route_profile = ''
~/.agenthicc/sessions/50d18253-…/workflows/a2e1b1ef1cb04cceb23929f093391d24/checkpoint.json
  /context/fields/route_profile = ''
```

### The failing path

```text
tui_session run_turn
  → workflow runner _drive()                          # runner.py:314 while not terminal
  → _product_discovery()                              # runner.py:441
  → _run_phase(..., "complete_product_discovery")     # data = details ∪ {decision, summary}
  → context.route_profile = _str(data, "scope")       # runner.py:449 → "" (key absent)
  → return TARGETED_DESIGN_RESEARCH                   # runner.py:467-469: "" != "broad"
  → _drive(): _checkpoint_boundary(context, "product_discovery", TARGETED_DESIGN_RESEARCH)
      phases = _active_phases(context)                # runner.py:1199 → PHASES_BY_PROFILE["broad"]
      phases.index(next_phase)                        # runner.py:891, unguarded
      → ValueError: tuple.index(x): x not in tuple
  → tui_session.py:6380  except Exception → _fmt_exc(exc)            # "ValueError: tuple.index(…)"
  → tui_session.py:6387  _fail_workflow_run(error, kind="workflow_error")
  → conv.append_event("error", {"message": error})                   # the ✗ row
  → workflow_paused_after_error → turn_failed
```

`_fmt_exc` (`tui_session.py:556-558`) formats `f"{type(exc).__name__}: {exc}"`,
which is exactly the observed line; `_workflow_failure_kind`
(`tui_session.py:561-572`) only upgrades to `configuration` when the message
contains `config` or `profile`, so this failure is filed as a generic
`workflow_error` and the recovery notice suggests resuming a run that cannot be
resumed.

### Reproduced

Against the shipped tree (`uv run python`), no network, no provider:

```python
from types import SimpleNamespace
from agenthicc.workflows.ui_ux_pro_max.runner import UiUxProMaxRunner
from agenthicc.workflows.ui_ux_pro_max.state import UiUxProMaxState

runner = object.__new__(UiUxProMaxRunner)          # only _active_phases is needed
for profile in ("", "Broad", "broad-design"):
    ctx = SimpleNamespace(route_profile=profile, last_boundary={})
    runner._checkpoint_boundary(                  # runner.py:867
        ctx, "product_discovery", UiUxProMaxState.TARGETED_DESIGN_RESEARCH
    )
```

```text
profile=''                     phases=11 next_state=TARGETED_DESIGN_RESEARCH next_phase='targeted_design_research' in_phases=False
   -> ValueError: tuple.index(x): x not in tuple
profile='Broad'                …  -> ValueError: tuple.index(x): x not in tuple
profile='broad-design'         …  -> ValueError: tuple.index(x): x not in tuple
profile='broad'                …  -> (proceeds; guarded path)
profile='targeted'             …  -> (proceeds)
profile='accessibility_audit'  …  -> (proceeds)
```

The same probe shows the **latent resume defect**: any state that the selected
profile's tuple does not contain raises the identical exception at
`runner.py:318` before the loop body runs.

```text
profile='accessibility_audit' state=DESIGN_SYSTEM_REVIEW     in_phases=False -> ValueError: tuple.index(x): x not in tuple
profile='targeted'            state=DESIGN_SYSTEM_SYNTHESIS  in_phases=False -> ValueError: tuple.index(x): x not in tuple
```

And the recorded checkpoints cannot be restored:

```python
UiUxProMaxContext.from_checkpoint_payload(checkpoint["context"]["fields"])
# ValueError: ui_ux_pro_max checkpoint contains an unknown route profile
```

### Three sources of truth that disagree

| Site | Code | `""` / unknown means |
|---|---|---|
| `runner.py:449` | `context.route_profile = _str(data, "scope")` | accepted verbatim, empty |
| `runner.py:467-469`, `runner.py:499-501` | `if context.route_profile == "broad": … else …` | **not broad** → targeted route |
| `runner.py:1199-1200` | `PHASES_BY_PROFILE.get(profile, PHASES_BY_PROFILE["broad"])` | **broad** |
| `definition.py:159-165` | `… if profile_value in PHASES_BY_PROFILE else "broad"` | **broad** |
| `state.py:282-284` | `if route_profile not in PHASES_BY_PROFILE: raise ValueError` | **fatal** |

Four of the five agree that an unknown profile degrades to `broad`; only the
transition disagrees, and nothing enforces membership at the point the value is
introduced. The checkpoint validator then turns the surviving `""` into a
hard failure, so the run is unrecoverable even though a durable revision-5
checkpoint exists.

### Why the tuple is indexed unguarded

`_phase_index` already does it correctly — `return phases.index(phase) if phase
in phases else 0` (`runner.py:1203-1207`) — and `phase_lifecycle.reconcile_phase_cursor`
uses the same guarded shape (`phase_lifecycle.py:389`). Two call sites were
missed: the loop head (`runner.py:318`) and the branch classifier
(`runner.py:891`). `runner.py:876` guards `next_phase` but then falls through to
`phases.index(completed_phase)` unguarded, and line 891 indexes `next_phase`
again without the membership test — so a *single* non-membership both
misclassifies the boundary and crashes.

### Consequence for the user

- The work of a 4½-minute, 26-tool turn is kept (the files were written), but the
  workflow is paused at `product_discovery` for revision 5 forever.
- `/workflow resume <run_id>` fails with a *different*, equally unexplained
  `ValueError`, so the run cannot be advanced, only discarded and restarted.
- The user cannot tell whether the failure came from their repository, the
  provider, or a bug. `✗ ValueError: tuple.index(x): x not in tuple` is not
  actionable, and it is not reproducible by the user because nothing about the
  route profile is visible anywhere in the TUI.

## Problem statement

A workflow's routing decision is derived from a free-text field the model may
omit, is not validated against the profile vocabulary, and is then used to index
a tuple selected by a *different*, silently-defaulting rule. The resulting
inconsistency is reported as a bare `ValueError` and leaves the run
unresumable.

## Goals

1. The route profile is a closed vocabulary at every boundary: ingest,
   transition, phase-list selection, checkpoint write and checkpoint read.
2. An unknown or missing profile **never** changes the phase tuple silently and
   never crashes the runner; it is normalised to `broad` (the documented
   default) and recorded as a diagnostic.
3. No phase-tuple lookup can raise `ValueError`; a mismatch produces a typed
   workflow failure with the workflow, phase, profile and tuple in the message.
4. Existing checkpoints whose `route_profile` is `""` (the two recorded here,
   and any produced by a shipped build) resume successfully as `broad`.
5. The user-visible failure names the workflow, the phase and the cause, and
   the recovery hint is correct.

## Non-goals

- Redesigning the `ui_ux_pro_max` phase plan, the profiles, or their order.
- Changing how phases are prompted or how many turns a phase may run.
- Making `complete_product_discovery` reject a submission (it must stay
  non-fatal: the runner, not the tool, owns the transition — see
  `phase_tools.py:24-36`).
- Touching PRD-264's registry work or any other workflow's topology.

## Functional requirements

### FR-1: One canonical profile vocabulary with one normaliser

Add a single `normalise_route_profile(value: object) -> tuple[str, bool]`
(or equivalent) in `ui_ux_pro_max/state.py`, returning the canonical profile and
whether a fallback occurred. Rules:

- exact-match members of `PHASES_BY_PROFILE` pass through;
- string aliases are lower-cased, trimmed, and hyphen/space-normalised before
  matching (`"Broad"`, `"BROAD"`, `" broad "` → `broad`);
- anything else → `broad`, `fallback=True`.

`_active_phases`, `_product_discovery`, `resolve_checkpoint_topology` and
`from_checkpoint_payload` must all call it. No site may compare the raw value to
`"broad"` again.

### FR-2: The transition and the phase tuple read the same value

`_product_discovery` must derive `next_state` from the *normalised* profile, so
`next_state` is always a member of `_active_phases(context)`:

- `broad` → `DESIGN_INTELLIGENCE`;
- `targeted`, `accessibility_audit` → `TARGETED_DESIGN_RESEARCH`.

Add an assertion-style invariant (not a bare `assert`) that raises a typed
workflow error naming the profile and state when it does not hold.

### FR-3: Phase-tuple lookups never raise

Replace every unguarded `phases.index(...)` in `ui_ux_pro_max/runner.py`
(`:318`, `:338`/`:891`, `:855`, `:876`) with `_phase_index`-style guarded
lookups. A phase that is absent from the tuple must be reported as a
`PhaseTopologyError`-style failure containing: workflow name, phase name, the
active profile, and the profile's phase names.

### FR-4: The tool contract names the key

`complete_product_discovery`'s docstring/schema (`phase_tools.py:47-53`) and the
`product_discovery` phase prompt (`runner.py:97-105`) must state the exact
`details` key (`scope`) and its allowed values, and must say that an
unrecognised value is treated as `broad`. If the key stays free-form, the runner
must also accept the obvious synonyms the model actually produced
(`classification`, `route_profile`, `profile`) as input aliases rather than
silently reading `""`.

### FR-5: Checkpoints tolerate a legacy empty profile

`from_checkpoint_payload` (`state.py:282-284`) must normalise instead of raising
for `""`, `None` or an unknown string, and record the fallback in the restored
context (for example `route_profile_source = "normalised_from:<value>"`, in the
same spirit as the existing `resume_resolution_source`). The checkpoint's
`route_profile` must be re-written as the canonical value on the next boundary.
Only a *non-string* profile type may still be rejected.

### FR-6: The user-visible failure is actionable

`_workflow_failure_kind` must classify a topology/profile mismatch as
`configuration`, not `workflow_error`, so the pause notice and
`workflow_paused_after_error.failure_kind` agree. The `error` row must carry the
workflow name, phase, profile and expected phases (bounded to one line, TUX-08
`Notice` conventions), e.g.:

```text
 ✗ Workflow 'ui_ux_pro_max' phase 'product_discovery' routed to
   'targeted_design_research', which profile 'broad' does not define
   (broad: product_discovery, design_intelligence, …).
```

A bare `tuple.index(x): x not in tuple` must never reach a transcript.

### FR-7: A fallback is recorded, not silent

When the normaliser falls back, emit a bounded diagnostic entry on the run
(`context.profile_fallback` or an event payload field) so a resumed run and the
`/workflow status` view can report "profile fell back to broad".

### FR-8: The invariant is testable without a provider

Expose the transition as a pure function of `(profile, state)` or make
`_product_discovery`'s routing decision injectable, so the phase-tuple
invariant is unit-testable without `_run_phase`, a provider, or a TUI.

## Non-functional requirements

- No new dependency; no network.
- All changes confined to `workflows/ui_ux_pro_max/{runner,state,definition,phase_tools}.py`
  plus `runners/tui_session.py` for FR-6.
- Startup/latency budget unchanged: the normaliser is a dict lookup on a
  five-entry map; the TUI first frame is untouched (see
  `tests/e2e/test_first_frame_budget_e2e.py`).
- Behaviour is deterministic and covered on Python 3.11, 3.12 and 3.13.
- ASCII and no-colour rendering of the new diagnostics must be legible
  (no new glyphs; reuse the existing `Notice` markers).

## Acceptance criteria

1. `_checkpoint_boundary(ctx(profile=""), "product_discovery", TARGETED_DESIGN_RESEARCH)`
   no longer raises; with FR-2 in place that transition cannot be produced by
   an empty profile at all (the transition becomes `DESIGN_INTELLIGENCE`).
2. `base - argument` for every profile in `PHASES_BY_PROFILE` and every state
   the profile's edges can reach: `_drive`'s loop head and `_checkpoint_boundary`
   never raise; a synthetic mismatch raises a typed error whose message contains
   the workflow name, phase name and profile.
3. `from_checkpoint_payload({... "route_profile": "" ...})` returns a context
   with `route_profile == "broad"` and a recorded fallback; the two checkpoints
   named in § *Evidence* restore successfully and resume into
   `product_discovery`.
4. `resolve_checkpoint_topology`, `_active_phases` and the transition all agree
   for the six values `""`, `"Broad"`, `"broad "`, `"broad"`, `"targeted"`,
   `"accessibility_audit"`, `"unknown"` (single parametrised test).
5. No `phases.index(` call remains in `workflows/ui_ux_pro_max/runner.py`
   without a membership guard (enforced by a source-level test, § *Test plan*).
6. `_workflow_failure_kind(<topology mismatch>) == "configuration"`.
7. The recorded session replay of the two events above yields one actionable
   error row instead of `ValueError: tuple.index(x): x not in tuple`
   (assert on the rendered text, not on a byte budget).

## Recommended implementation shape

1. `state.py`: add `ROUTE_PROFILE_ALIASES`, `normalise_route_profile()`, and
   `PhaseTopologyError(RuntimeError)` carrying `workflow`, `phase`, `profile`,
   `phases`.
2. `runner.py:449`: `context.route_profile, context.profile_fallback = normalise_route_profile(data.get("scope") or data.get("classification"))`.
3. `runner.py:467-469` and `:499-501`: route on the canonical value only.
4. `runner.py:318`, `:855`, `:876`, `:891`: use `_phase_index` and raise
   `PhaseTopologyError` instead of indexing.
5. `state.py:282-284`: normalise on read, record the source, never raise for a
   string.
6. `tui_session.py:561-572`: classify `PhaseTopologyError` as `configuration`,
   and pass the exception through `_fmt_exc` only after the runner has added its
   context (keep `_fmt_exc`'s class-name guarantee).

## Test plan

### Unit (`tests/unit/test_ui_ux_pro_max.py`)

- `test_route_profile_normaliser` — parametrised over the six values above,
  asserting canonical output and the fallback flag.
- `test_transition_is_always_member_of_active_phases` — for every profile and
  every reachable non-terminal state, `next_state.name.lower()` is in
  `_active_phases(context)`.
- `test_checkpoint_boundary_never_indexes_an_absent_phase` — the reproduction in
  § *Reproduced*, asserting the typed error (or clean `broad` routing).
- `test_empty_profile_checkpoint_restores_as_broad` — feeds the recorded
  `context.fields` shape.
- `test_discovery_selects_the_adaptive_research_route` — extend the existing
  parametrisation with `""`, `"Broad"` and an unknown string, asserting the
  broad route rather than the current `TARGETED_DESIGN_RESEARCH`.
- `test_product_discovery_transition_records_without_validating_details`
  (currently pinning `data["scope"] == "unknown"`) — keep the tool permissive
  but assert the *runner* normalises.

### Integration

- Drive `_drive` with a stub `_run_phase` for the two recorded payloads
  (`classification: broad`, no `scope`) and assert:
  `WorkflowPhaseCompleted` for `product_discovery`, then
  `WorkflowPhaseStarted` for `design_intelligence`, and exactly one
  `WorkflowRunStarted` with the profile's `phase_names`.
- Resume a checkpoint whose `route_profile` is `""` through the real recovery
  path and assert the phase cursor reconciles to `product_discovery`/`broad`.

### End-to-end

- Replay the recorded session events
  (`~/.agenthicc/session-service/50d18253-…`, seq 44-52) through the TUI
  transcript renderer: the turn footer stays "Worked for", and the following row
  is an actionable `error` notice — never `tuple.index`.

### Static/regression

- A source-level test asserting no `phases.index(` in
  `workflows/ui_ux_pro_max/` lacks a membership guard, mirroring the style of the
  existing source-guard tests.
- `ruff check` / `mypy` on the touched modules.

### Performance

- None required beyond confirming the first-frame budget test is unchanged.

## Rollout and recovery

- The fix is backward compatible: `broad`/`targeted`/`accessibility_audit`
  behave exactly as today, and only previously *unreachable* or *crashing*
  values change meaning (they now mean `broad`, which is what
  `_active_phases`, `resolve_checkpoint_topology` and `from_checkpoint_payload`
  already documented).
- Existing paused runs with `route_profile: ""` (two on this machine) become
  resumable after the change; no migration is needed because the value is
  normalised on read and re-written on the next boundary.
- A resumed run continues at `product_discovery` with the broad phase list,
  which is the phase the turn actually completed — so no work is repeated
  beyond re-running the discovery submission.
- Add a `CHANGELOG.md` entry under *Fixed* naming the workflow and the
  user-visible message that disappears.

## Evidence and limitation

- The crash site is confirmed by two independent recorded runs, an offline
  reproduction of the exact exception text, and the checkpoint contents written
  by the same phase.
- The exact reason the model chose `classification` over `scope` cannot be
  recovered from the logs (tool arguments are bounded in the service log), but
  the key is absent from both the tool description and the prompt — the omission
  is confirmed by source inspection, which is sufficient to explain it.
- The `saved checkpoint → resume` failure is demonstrated directly
  (`from_checkpoint_payload` on the recorded fields); the end-to-end
  `/workflow resume` path was not driven interactively.
