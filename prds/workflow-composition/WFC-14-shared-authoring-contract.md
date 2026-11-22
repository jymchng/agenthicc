---
title: "WFC-14: A shared authoring contract — runner-owned validation, scoped generation, bounded repair"
status: Proposed
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [WFC-00, WFC-05, WFC-06, WFC-07, WFC-09, WFC-11, prd-154, prd-258]
tags: [authoring, validation, workflows, safety, repair]
---

# WFC-14 — A shared authoring contract

## 1. Summary

Three authoring workflows will exist: `create_workflow`, `create_tools` and
`create_skills`. Each must (a) decide pass/fail by **runner-owned deterministic
validation**, not by the model's say-so; (b) let the generating agent write only
inside a **scoped root the runner chose**; and (c) stop after a **bounded,
persisted repair budget**. `create_workflow` already does most of this in its own
code. This PRD extracts that contract into one shared module, adds the missing
pieces, and makes the other two workflows adopt it — so the contract is
specified once, here, and owned by this series.

## 2. Problem (current source)

| Fact | Where |
|---|---|
| **The tool workflow's verdict belongs to the agent.** The validate phase offers two control tools; `approve_tool(summary)` sets `action = "approve"` and ends the phase. Nothing compares the written file with the plan. | `workflows/make_agenthicc_tool/runner.py` (`_make_validate_tools`, ~`:290-342`) |
| **Its containment check is a substring test.** `confirm_generation_complete` accepts any reported `file_path` that *contains* `.agenthicc/tools/`; the generate phase is told in prose where to write. The runner never scans what was actually written. | `runner.py` ~`:273` and the generate prompt ~`:592` |
| **Its repair loop has no counter.** `_MAX_ATTEMPTS = 5` bounds attempts *within one phase* while it waits for a transition tool; a reject returns to GENERATE with no cycle count, so VALIDATE → GENERATE can repeat indefinitely. | `runner.py:62`, `:663-728` |
| **`create_workflow` already owns its verdict.** The runner overrides an `approve_workflow` when deterministic validation failed, and binds approval to a package fingerprint and evidence id. | `workflows/create_workflow/phase_tools.py:288-345`; `runner.py` (`validation_evidence`, ~`:1595-1640`) |
| **`create_workflow` already scopes generation.** Writer tools are addressable only inside the lease's package root and reject escapes and symlinks. | `workflows/create_workflow/generation_tools.py:131`, `:232` (`make_generation_writer_tools(lease)`) |
| **`create_workflow` already bounds repair.** `repair_cycles` is charged on every VALIDATE → GENERATE route and fails the run when it exceeds `_max_repair_cycles`. | `workflows/create_workflow/runner.py:530`, `:1725-1734` |
| **`create_workflow` loads generated code in-process.** Validation imports package modules through `load_python_workflow_modules` and removes them from `sys.modules` afterwards; the smoke harness drives the runner with a fake config. | `workflows/create_workflow/validation.py:276-284`; `smoke.py:354` |
| **There is no equivalent for skills**, and the three workflows would otherwise grow three copies of the same rules. | — |

## 3. Goals

1. One verdict rule: a failing deterministic report can never be turned into a
   pass by the model.
2. One scoping rule: generation writes only under a root derived by the runner;
   the model never supplies a destination, and claims of "I wrote X" are checked
   by a runner scan.
3. One repair rule: a per-run budget, persisted in the checkpoint, that resume
   cannot reset.
4. One import-validation rule: generated code is never imported in the agent
   process (WFC-11 §5.5), and any harness that does import keeps the package
   namespace registered for the whole execution and removes it afterwards.
5. `create_workflow` loses its private copies; `create_tools` and
   `create_skills` get the contract by adoption, not by re-implementation.

## 4. Non-goals

- The per-kind *rules* (what makes a tool, skill or workflow valid) — those are
  WFC-07 §5.4, WFC-09 §5.3 and the existing `create_workflow` validation.
- Trust and publication (WFC-11) and reload (WFC-05).
- A new phase machine; phases stay as each workflow defines them.

## 5. Design

### 5.1 Module

New package `workflows/authoring/`:

| Module | Contents |
|---|---|
| `scoped_tools.py` | `make_scoped_writer_tools(root: Path, *, suffixes: frozenset[str], max_file_bytes: int, max_files: int) -> list[Callable[..., object]]` and a read-only twin. Generalises `generation_tools.make_generation_writer_tools`. |
| `verdict.py` | `Finding`, `ValidationReport`, `decide()`. |
| `budget.py` | `RepairBudget`, `RepairBudgetExhausted`. |

### 5.2 Runner-owned verdict

```python
FindingSeverity = Literal["error", "warning"]

@dataclass(frozen=True, slots=True)
class Finding:
    code: str                    # stable, e.g. "tool.name_mismatch"
    severity: FindingSeverity
    path: str                    # relative to the authoring root
    line: int | None
    message: str                 # bounded, no file contents

@dataclass(frozen=True, slots=True)
class ValidationReport:
    digest: str                  # content digest of the validated set (WFC-11 §5.2)
    findings: tuple[Finding, ...]
    @property
    def errors(self) -> int: ...

def decide(report: ValidationReport, agent_action: Literal["approve", "reject"]) -> Literal["approve", "reject"]: ...
```

`decide` returns `"approve"` only if `report.errors == 0` **and** the agent chose
approve. The agent may always reject, and may add findings through its own
review (recorded as warnings); it can never remove or downgrade a runner
finding. Approval evidence stores `report.digest`; any content change after
approval invalidates the evidence and sends the run back to validation —
`create_workflow`'s existing fingerprint-bound evidence is the model.

### 5.3 Scoped generation

- The generate and repair phases receive **only**: the scoped writer bound to
  the run's authoring root, a read-only scoped reader for the same root, and the
  existing read-only discovery tools through `WorkspaceView`. No shell or exec,
  no network, no mode or approval change. A Safe or Plan session stays Safe or
  Plan for the whole run.
- The root is derived by the runner from the run id and the approved names
  (`.agenthicc/.staging/<run_id>/<kind>/…` for tools and skills; the existing
  lease package for workflows). The model supplies names only, validated by a
  per-kind identifier rule.
- Writers reject: absolute paths, `..`, symlinks (as the existing workflow
  writers do), disallowed suffixes, files over `max_file_bytes`, more than
  `max_files`, and any attempt to address the runner's metadata
  (the pending marker and the manifest). They never open an overwrite of a
  *live* file; live files are touched only by the publish step (WFC-11).
- **Claims are not evidence.** The runner scans the root after generation and
  builds the manifest itself. Today's `confirm_generation_complete(file_path,
  summary)` pattern, which trusts a reported path, is replaced by a scan.

### 5.4 Bounded, persisted repair

```python
@dataclass(frozen=True, slots=True)
class RepairBudget:
    max_cycles: int
    used: int = 0
    def charge(self) -> "RepairBudget": ...     # raises RepairBudgetExhausted when used + 1 > max_cycles
```

- `workflows.authoring.max_repair_cycles` (default 3) in configuration.
- A VALIDATE → GENERATE round trip charges one cycle. `used` is stored in the
  typed checkpoint context, so `/workflow resume` cannot reset it.
- On exhaustion the run fails with the last `ValidationReport` text and a clear
  next action; **no further provider turn is started**.
- A repair turn receives the previous report, the manifest and the scoped
  reader/writer, and edits the existing candidate in place. It is not asked to
  regenerate from scratch unless the agent chooses to remove files.
- The per-phase attempt bound (`_MAX_ATTEMPTS`-style, waiting for a transition
  tool) stays as a second, independent guard.
- This is a state-machine bound inside the workflow, like `create_workflow`'s
  existing one. It is not a subagent limit and does not change how workers run
  (WFC-04): a worker whose workflow fails reports the typed failure.

### 5.5 Import-validation lifetime

Where validation must load generated Python (the workflow smoke harness; the
tool "would this register?" check), it runs in the WFC-11 §5.5 sandboxed
subprocess. Inside that harness the package namespace stays registered in
`sys.modules` for the **entire** execution, so valid function-local relative
imports (for example `from .state import Context` inside a method) resolve, and
it is removed when the harness finishes, including on failure. Nothing generated
is ever imported in the agent process.

### 5.6 Adoption and deletions

| Workflow | Adopts | Deleted or moved |
|---|---|---|
| `create_tools` (WFC-07) | scoped writer, verdict, budget | `approve_tool`/`confirm_generation_complete` path-claim logic |
| `create_skills` (WFC-09) | scoped writer, verdict, budget | — (new) |
| `create_workflow` (WFC-06) | scoped writer, verdict, budget | `generation_tools.make_generation_writer_tools` generic parts, `_max_repair_cycles`, `CreateWorkflowContext.repair_cycles` (replaced by `RepairBudget`; checkpoint schema bumped) |

## 6. Requirements

- **FR-1** No workflow may end its validate phase on the agent's approval alone;
  a test fails a workflow that does.
- **FR-2** Every generated file is addressed relative to the run's root; the
  model-supplied path never reaches `open()` unnormalised.
- **FR-3** `RepairBudget` is part of each authoring context's checkpoint payload;
  a checkpoint without it fails validation.
- **FR-4** `Finding.message` carries no file bodies; code and path suffice.
- **FR-5** Each new public symbol has an `llms-full.txt` entry; none uses `Any`.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | A scripted agent calls approve over a report with one error finding; `decide` returns reject, and the run goes back to generation. |
| A2 | The scoped writer rejects `../x`, an absolute path, a symlink, an oversize file, an over-count write and the marker path, each with its own code. |
| A3 | After three failed validations and `max_cycles = 3`, the fourth route fails the run, starts no provider turn, and the failure text contains the last report. |
| A4 | Killing and resuming after two cycles resumes with `used == 2`. |
| A5 | Editing a file after approval invalidates the evidence and re-runs validation. |
| A6 | A generated package using a function-local relative import passes the harness; the namespace is absent from `sys.modules` afterwards, also when the generated code raises. |
| A7 | `create_workflow`, `create_tools` and `create_skills` all import the shared modules; `grep -rn "_max_repair_cycles\|repair_cycles" src` returns nothing outside `workflows/authoring/` and its tests. |
| A8 | A workflow that approves on the agent's word alone fails the FR-1 conformance test. |

## 8. Test plan

- Unit: `decide` truth table, `RepairBudget` arithmetic, scoped-writer rejection
  matrix (`tests/unit/test_authoring_contract.py`).
- Integration: a stub authoring workflow driven through generate → validate →
  repair → exhaustion with a real checkpoint store and resume
  (`tests/integration/test_authoring_contract.py`); a conformance test that
  parametrises over all three built-in authoring workflows for FR-1.
- The existing `create_workflow` validation, scope and repair tests move to the
  shared module's tests where they test the contract, and stay where they test
  workflow-specific rules.

## 9. Documentation and cleanup

`docs/guides/workflows.md` (authoring contract), `docs/guides/plugins.md`,
`llms-full.txt`; `configuration.md` for `workflows.authoring.max_repair_cycles`.
The removals in §5.6 land in the same change as the adoption.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Moving `create_workflow`'s repair counter bumps its checkpoint schema. | Deliberate and pre-release; stale checkpoints fail validation with the existing message path. |
| A strict verdict rejects reasonable code. | Findings are specific and the repair loop is the remedy; severity `warning` exists for advisory rules. |
| Scoped writers hinder legitimate multi-file packages. | `max_files`/`max_file_bytes` are per-kind parameters chosen in each workflow's PRD. |

## Amendment — 2026-11-22

The shared authoring contract lands before the sandboxed importer and the
tool and skill workflows, following WFC-00 §5. The existing workflow package
keeps its canonical lease. Its versioned authoring context requires
`RepairBudget` on load, while the outer checkpoint format remains the same
for unrelated workflows. A change that invalidates a prior approval also
spends one repair cycle before the candidate returns to validation. Exhaustion
writes a terminal checkpoint.

A6 will be verified when WFC-11 wires the sandboxed importer. The adoption and
conformance checks across all three authoring workflows follow WFC-07 and WFC-09.
