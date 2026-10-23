---
title: "PRD-250: Code Quality Evaluation and Improvement Programme"
status: In progress
version: 1.0.0
date: 2026-10-22
repository: jymchng/agenthicc
priority: P0
related_prds: [prd-138, prd-140, prd-169, prd-170, prd-172, prd-207, prd-248, prd-249]
tags: [code-quality, maintainability, architecture, typing, testing, security, ci, process]
---

# PRD-250 — Code Quality Evaluation and Improvement Programme

## Implementation status (2026-10-22)

WS-1 (restore and enforce the gates), WS-2 (the ten verified defects) and WS-3.1–3.3
(the three trust gaps) are implemented in the working tree. WS-3.4–3.5 and WS-4
onward are not started. The study below is unchanged and still describes the
repository at `2d66765`.

| Item | Status |
|---|---|
| 1.1 Format | Every file passes `ruff format --check`; `nox -s lint` runs it on all of `src/`, `tests/` and `scripts/` |
| 1.2 mypy | `mypy src/agenthicc`: 0 errors. Six bundled runners that subclass `CodePlanRunner` (`copy_website`, `goal_flow`, `make_agenthicc_tool`, `make_book`, `reconstruct_site/phase_impl`, `site_imitate`) carry `# mypy: disable-error-code=override` with `owner=PRD-250 WS-4.2; expires=2026-12-03`, enforced by `type_audit` |
| 1.3 Branch protection | `.github/workflows/ci.yml` ends in one aggregate **Required checks** job; `scripts/apply_branch_protection.sh` requires it and code-owner review on `main`. **Not yet applied** — needs a repository administrator |
| 1.4 Release | `release.yml` re-runs CI for the tagged commit, requires the tag to equal `v` + the package version, and checks the wheel's `agenthicc --version`; the CLI reads the version from package metadata. The version stays declared in `pyproject.toml` (no `setuptools-scm`) |
| 1.5 CodeQL and docs | CodeQL installs the declared `dev` extra; `docs.yml` and the CI docs job build with `--strict` |
| 1.6 Dependencies | One `dev` extra (now with `twine` and the book/screenshot test dependency); the dependency group and the `aiohttp`/`httpx` core/extra duplicates are removed. Nox sessions use the uv backend, so each tool runs at its declared version inside the session venv |
| 1.7 Matrix | Unit and integration tests run on Python 3.11, 3.12 and 3.13. Verified locally on 3.11 (3,989 unit and 319 integration tests); the first 3.11 run exposed a real defect — the rewritten kernel `run()` loop polled with `asyncio.wait_for`, which on 3.11 can drop a cancellation (gh-86296) and hang shutdown — now fixed with a regression test. A Windows job runs the lock, filesystem and terminal tests — **unverified until its first Actions run** |
| 1.8 Cassette replay | `test_cassette_replay.py` and the new hermetic `goal_flow` campaign replay run in every integration session |
| 1.9 Baselines | `.github/CODEOWNERS` owns the baselines and gate definitions; `type_audit` fails on a stale baseline and, with `--baseline-ref`, on a raised one (a `type-baseline-increase` label is the explicit approval) |
| D-01–D-10 | Fixed with regression tests (Section 6.2) |
| S-01–S-03 | Closed with acceptance tests: skill context commands never execute; one digest-bound extension trust service gates every loader (`agenthicc trust`); inert settings deleted and a field-reader test added |

Gate measurements after the change:

| Gate | Before | After |
|---|---|---|
| `ruff format --check` | 8 files | 0 |
| `mypy src/agenthicc` | 240 errors | 0 (6 time-boxed module overrides) |
| Coverage (statements + branches, subprocesses measured) | 84.5% statements only | 82.1% combined (85.2% statements, 72.2% branches); floor `fail_under = 81.5` |
| `getattr` calls | 762 (716 in the tightened baseline) | 500 — the 90-day budget in Section 10 |
| `RUF006` dangling tasks | 8 | 0 |
| Inert configuration fields | 15 | 0 |

Found during implementation, not started: every provider step deep-copies the
whole conversation about seven times and rescans its whole tool history about
six times, so per-step CPU grows with conversation length. On the 510-step
`goal_flow` replay that is 3,381 full deep copies — 2,179 from lauren-ai's
`ShortTermMemory.snapshot()` and 1,164 from
`JournaledShortTermMemory.ensure_valid_and_persist()`, which copies the history
twice per call — and 2,935 `_validate_tool_history` scans. Together they take
about 70% of the replay's CPU: 165 s uninstrumented and 481 s under branch
coverage on the development host. The replay's ceilings are sized for that
cost; reducing it needs its own replay-safety analysis.

## 1. Executive summary

This PRD records a whole-repository code-quality study of agenthicc at commit
`2d66765` (2026-10-20) and defines a measurable programme to improve it. Every
number below was produced by a command or analyzer described in Appendix A.

**Bottom line.** agenthicc is *functionally* well-protected and *structurally*
under-governed. Its 4,438 tests pass, its kernel is pure and immutable, and its
lowest layers (kernel, memory, tools, config) have almost no upward
dependencies. But the source tree grew **5× in under four months** (24,158 →
121,928 non-blank lines), and the controls meant to keep that growth healthy
are not holding:

- **Three of the documented quality gates fail on `main`.** `ruff format
  --check` flags 8 files, `mypy src/agenthicc` reports **240 errors** (PRD-140
  recorded zero), and statement coverage is **84.5%** against the `coverage`
  session's 90% floor. The release workflow publishes without re-running tests,
  and the CodeQL workflow cannot install the package.
- **The typing ratchet has become a counter.** Its baseline file changed in 33
  commits since 2026-07-23, and 31 of them raised the `getattr` budget — from
  214 to 777.
- **The lint configuration does not match the code.** Ruff enforces only its 59
  default rules. All **1,305 `noqa` directives** in `src/` are unused, and
  1,285 of them name rules that are not even enabled.
- **The orchestration layer is one tangle.** Counting function-local imports,
  34 modules across 8 packages form a single dependency cycle. `TUISession` is
  a 2,870-line class with 66 methods; its module imports 70 internal modules,
  has 122 commits of churn and 65% coverage.
- **Repository content can execute without consent.** Auto-triggered project
  skills run their `` !`command` `` placeholders through `shell=True`, and
  project tools, commands and workflows are imported at session start;
  `plugins/trust.check_trust` exists but nothing calls it.
- **Fifteen configuration fields are accepted, documented and ignored**,
  including `[security] sandbox_mode`, both tool resource limits, and six
  `[plugins]` controls that the guides tell users to rely on.

The study also verified ten concrete defects (Section 6.2), from leaked SQLite
connections to fire-and-forget asyncio tasks that emit durable kernel events.

The programme in Section 9 is sequenced so the first two weeks stop further
erosion (restore and enforce the gates, fix the verified defects, close the
consent gaps), and the following quarter pays down structural debt behind
fitness functions that make regressions fail CI rather than raise a baseline.

## 2. Scope and method

**In scope:** `src/agenthicc` (308 files), `tests/` (378 files), CI and Nox
configuration, packaging metadata, documentation and the PRD process.

**Out of scope:** product direction, lauren-ai internals, and behaviour that is
already tracked by an open feature PRD (cross-referenced in Section 13).

**Evidence sources:**

| Source | Use |
|---|---|
| Project gates | `ruff`, `ruff format`, `mypy`, `scripts/type_audit.py`, `mkdocs --strict`, `nox -s llms_check`, full `pytest` with coverage and durations, `uv build` |
| Stricter ruff families, run read-only | Size latent defects without changing configuration |
| AST analyzers (Appendix A) | Function size, approximate McCabe complexity, Type-2 clones, import graph and cycles, broad-exception handling, private access, inert configuration |
| `git log` | Growth, churn hotspots, ratchet history, commit and PRD practice |
| Targeted reading and experiments | Every defect in Section 6.2 and every security finding in Section 6.3 was reproduced or traced through the code, not inferred from a metric |

Metric caveats: complexity is an AST approximation of McCabe (decision points
plus one, nested definitions scored separately); "lines" are physical lines
unless stated as non-blank; heuristic counts are inventories, not a claim that
every occurrence is wrong.

## 3. Growth context

| Snapshot | Commit | Source files | Source non-blank lines | Test files |
|---|---|---:|---:|---:|
| 2026-07-01 | `6155935` | 164 | 24,158 | 118 |
| 2026-07-23 (PRD-140) | `36c7e5e` | 183 | 32,032 | 159 |
| 2026-08-15 | `b3e480b` | 246 | 68,735 | 267 |
| 2026-09-15 | `070391e` | 261 | 89,542 | 315 |
| 2026-10-20 | `2d66765` | 308 | 121,928 | 378 |

That is roughly 25,000 source lines per month across 468 commits. Most quality
problems below are the predictable result of that rate meeting non-blocking
gates: each delivery adds code, and consolidation is never a required step.

## 4. Quality gate status

The contributor guides define the definition of done. This is what it reports
today.

| Gate | Documented command | Result on `2d66765` | Enforced in CI? |
|---|---|---|---|
| Lint | `ruff check src tests scripts` | Pass — but only the 59 default rules (E4/E7/E9/F) | Yes |
| Format | `ruff format --check …` | **Fail — 8 files** | Yes, in `nox -s lint`; the check covers only files the user can write |
| Types | `mypy src/agenthicc` | **Fail — 240 errors in 10 files** (235 in bundled domain workflows, 5 in core) | Yes, blocking job |
| Type ratchet | `scripts/type_audit.py --check …` | Pass, because the baseline is raised when it bites (Section 6.7) | Yes |
| Coverage | `nox -s coverage` (`--cov-fail-under=90`) | **Fail — 84.5% statement coverage** | Yes |
| Tests | `pytest tests/` | Pass — 4,438 passed, 16 skipped, 108 warnings, 285 s with coverage | Yes; `test_cassette_replay.py` is `--ignore`d |
| Docs | `mkdocs build --strict` | Pass (one page not in nav) | Built **without** `--strict` in `docs.yml` |
| LLM docs | `nox -s llms_check` | Pass — checks 22 symbols; the package exports 909 unique names | Non-blocking on pull requests |
| Build | `nox -s build`, `build_check` | Build passes; `build_check` needs `twine`, which no dependency group declares | Release only |
| Release | `release.yml` on `v*` tags | Publishes without lint, type or test runs; version is a static `0.1.0` | — |
| Security scan | `codeql.yml` | **Install step fails:** `uv sync --extra all` — extra `all` is not defined | Yes |
| Python support | `requires-python = ">=3.11"` | 3.11 is never tested; CI and Nox use 3.12 and 3.13 | — |

Three blocking jobs fail on `main`, so either CI is not running against `main`,
or failures are being merged. Either way the gates are advisory in practice,
which is the root of most drift recorded below.

## 5. Scorecard

Scale: 5 exemplary and enforced · 4 good, minor gaps · 3 adequate, notable gaps
· 2 weak, systemic gaps · 1 failing or absent.

| # | Dimension | Score | Summary of evidence |
|---|---|:---:|---|
| 1 | Correctness and robustness | 3 | Broad passing suite; ten verified defects (6.2) |
| 2 | Architecture and modularity | 2 | Clean bottom layer; 34-module orchestration cycle; god classes (6.4) |
| 3 | Complexity and size | 2 | 21 functions with complexity > 50; 38 functions > 200 lines; 10 files > 2,000 lines (6.5) |
| 4 | Duplication | 3 | 2,948 redundant lines in Type-2 clones, concentrated in transition tools (6.6) |
| 5 | Type safety | 3 | Strict mypy profile and few `Any`; gate red; `dict[str, object]` payloads re-narrowed everywhere (6.7) |
| 6 | Error handling and observability | 2 | 104 silent broad swallows, including the reactive core and kernel processor (6.8) |
| 7 | Concurrency and resource safety | 2 | Dangling tasks, blocking I/O in async code, fail-open locks, connection leaks (6.2) |
| 8 | Security posture | 2 | Unconsented execution paths, dead trust code, inert security settings (6.3) |
| 9 | Test suite | 3 | 4,438 fast tests; coverage below floor; tests coupled to private seams (6.10) |
| 10 | Tooling and CI enforcement | 1 | Three blocking gates red; ratchet raised in 31 commits; unverified releases (4, 6.9) |
| 11 | Documentation accuracy | 3 | Strong documentation culture; guides promise inert controls; reference tables drift (6.11) |
| 12 | Dependency hygiene | 2 | 270 names imported from lauren-ai private modules, 243 of them public (6.4) |
| 13 | Delivery process | 2 | Feature-only commit labels, very large commits, unmanaged PRD lifecycle, no releases (6.11) |

**Overall: 2.3 / 5 (unweighted mean) — functional and well tested at the
behaviour level, structurally under-governed.**

### 5.1 Strengths to preserve

- `root_reducer` is pure and kernel `AppState` is frozen; no module imports both
  `AppState` types.
- `kernel`, `memory`, and `tools` have zero import-time dependencies on the
  orchestration packages, and there are **no import-time cycles** anywhere.
- The mypy profile is genuinely strict (`disallow_untyped_defs`,
  `disallow_any_generics`, `warn_return_any`), explicit `Any` appears in only 10
  annotations, and the core packages contribute only 5 of the 240 errors.
- 181 frozen dataclasses and 27 `Protocol`s give the domain real types.
- The unit suite runs 3,946 tests in 53 s; integration (317) and E2E (172) add
  about 100 s.
- There are no bare `except:` clauses, no TODO/FIXME debt markers, and secrets
  use the OS keyring with an owner-only file fallback.

## 6. Findings

Severity: **Critical** — exploitable or data-corrupting; **High** — likely
user-visible failure or a gate that no longer protects anything; **Medium** —
maintainability or reliability cost that compounds; **Low** — hygiene.

### 6.1 Verification and enforcement

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| F-01 | High | Three blocking gates fail on `main` | Section 4: format (8 files), mypy (240), coverage (84.5% < 90%) |
| F-02 | High | The release pipeline does not verify what it publishes | `release.yml` runs only `llms_check`, `build`, `build_check`; a tag on any commit publishes it |
| F-03 | High | Every release would publish the same version | `version = "0.1.0"` is static; the CLI hard-codes `"agenthicc 0.1.0"` twice in `cli/parser.py`; TestPyPI `skip-existing` hides the clash |
| F-04 | Medium | CodeQL cannot run | `codeql.yml` installs `--extra all`; `uv` rejects it: *Extra `all` is not defined* |
| F-05 | Medium | Gate coverage depends on file permissions | `nox -s lint`/`format` pass only writable files (`_writable_python_files()`) to `ruff format`; read-only files are silently skipped |
| F-06 | Medium | Tooling is undeclared or split | `twine` is used by `build_check` but declared nowhere; there are two `dev` sets (`[project.optional-dependencies].dev` and `[dependency-groups].dev`); `aiohttp` and `httpx` are listed in both core and extras |
| F-07 | Medium | The supported Python matrix is not tested | `requires-python >=3.11` and mypy targets 3.11; CI tests 3.12 and 3.13 only; CI runs Linux only although Windows terminal support is advertised |
| F-08 | Medium | The most realistic regression test is excluded from CI | `tests_integration` passes `--ignore tests/integration/test_cassette_replay.py`, although its fixtures are checked in and it passes locally (27.6 s) |

### 6.2 Verified correctness defects

Each item was reproduced by reading the code path and, where possible, by a
standalone experiment.

| ID | Severity | Defect | Location and evidence |
|---|---|---|---|
| D-01 | High | **Project and global memory leak one SQLite connection per operation.** `with self._connect() as conn:` uses the connection's context manager, which commits or rolls back but **does not close**. | `memory/layers.py` — 6 call sites in `ProjectMemoryLayer`, inherited by `GlobalMemoryLayer`. Reproduced with a minimal script. The three project-memory test files alone (29 tests) emit 69 `ResourceWarning: unclosed database` warnings; the full suite emits about 105. |
| D-02 | High | **Fire-and-forget tasks.** The loop keeps only weak references to tasks, so these can be garbage-collected mid-flight and their exceptions are never observed. | 8 `RUF006` sites, including the durable `WorkflowRunDiscarded` kernel event (`runners/tui_session.py:2782`), `/compact` (`:3079`), `/mcp` actions (`commands/builtins.py:586`), semantic indexing (`runners/agent_turn.py:2517`), and every kernel effect and snapshot (`kernel/processor.py:156,163`) |
| D-03 | High | **Unbounded in-memory kernel event log.** `EventProcessor._event_log` retains every event for the life of the process, and `event_log` returns a full copy. Production code never reads it; only tests do. | `kernel/processor.py:57,70,135` |
| D-04 | High | **Fail-open cross-process locks.** Four store `_lock()` helpers catch `(ImportError, OSError)` around `fcntl.flock` and proceed with no lock — always on Windows, and on any filesystem where `flock` fails. | `background/store.py:203`, `background/terminals.py:353`, `runs/store.py:32`, `worktrees/store.py:33`; `runners/process_lease.py` and `worktrees/environment.py` already use `msvcrt` correctly |
| D-05 | Medium | **Plugin modules execute twice.** The dependency probe imports each tool and command plugin solely to read `DEPENDENCIES`, then imports it again; probe failures are swallowed. Module side effects run twice. | `plugins/discovery.py:140-175`, `commands/plugin_loader.py:113-138`; an AST reader (`_infer_missing_from_ast`) already exists |
| D-06 | Medium | **The reactive core hides UI errors.** `Signal.set` and `Computed` catch every subscriber exception with `pass` and no log. `Computed` never releases its dependency subscriptions. | `reactive.py:59,99` |
| D-07 | Medium | **Kernel event contract drift.** 7 emitted event types have no reducer handler (for example `WorkflowPhaseStarted`, `SubagentPoolStarted`), 10 handled types are not mentioned anywhere in `src/` outside the reducer (`TaskCreated`, `WorkflowNodeAdded`, `AgentSpawnRequest`, …), and unknown events are silently ignored. | `kernel/reducer.py` (`_HANDLERS`: 20 types) versus 17 literal `Event.create(...)` types in `src/` |
| D-08 | Medium | **Event-processor failure semantics are still open from PRD-138 P1.2.** A reducer exception drops the event; a full subscriber queue drops the snapshot with `pass`; `drain()` busy-polls with `sleep(0.001)`; each event is written and flushed synchronously on the event loop. | `kernel/processor.py:104-170` |
| D-09 | Medium | **Late-binding closures and mutable defaults.** Closures capture loop variables (`B023`) and one function takes a mutable default (`B006`). | `tui/diff_renderer.py:511-520`, `tui/workspace/background_manager.py:2918`, `cli/registry.py:318`; `workflows/site_imitate/runner.py:286` |
| D-10 | Medium | **Blocking I/O inside async code.** 55 `ASYNC` findings: blocking path methods (32), `open()` (5), busy-waits (2) and synchronous subprocesses (2), contrary to the async-first goal of PRD-207. The skill shell injection in S-01 runs a synchronous `subprocess.run` inside the turn builder for up to 15 s per command. | `ruff --select ASYNC` |

### 6.3 Security

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| S-01 | **Critical** | **Repository-supplied skills can run shell commands without consent.** Project skills under `.agenthicc/skills/` are auto-triggered when a word of the user's message matches a `suggested_topics` entry. `process_skill_body` then runs every `` !`command` `` placeholder with `subprocess.run(..., shell=True)`. The path bypasses the capability gate, approvals, Safe/Plan restrictions and the worktree sandbox, and no trust check exists anywhere in `skills/`. | `skills/runner.py:26-44,85-115`; called from `runners/agent_turn.py:1338-1357` |
| S-02 | High | **Project code is imported at session start without a trust decision.** Tools, slash commands and workflows under `.agenthicc/` are loaded with `exec_module`. `plugins/trust.check_trust` is never called outside its own unit test. A second, separate trust mechanism covers only CLI subcommand plugins (`cli/registry.py:_maybe_load_trusted`). PRD-138 P0.4 remains open. | `plugins/discovery.py:170-175`, `commands/plugin_loader.py:133-138`, `workflows/loader.py:188-216` |
| S-03 | High | **Fifteen settings are accepted, documented and ignored.** No code reads `[security] sandbox_mode`, `max_tool_cpu_seconds`, `max_tool_memory_mb`; `[plugins] auto_install`, `install_target`, `allowed_modules`, `timeout_seconds`, `trust_file`, `audit_file`; `[memory] project_memory_path`, `vector_db`, `session_ttl_seconds`; `[behaviour] verbose`, `confirm_exits`; or `[storage] default_backend`. `docs/guides/plugins.md` describes `allowed_modules` as an enforced allow-list, and the quickstart and configuration guides set `sandbox_mode = true`. | Field-reader audit, Appendix A |
| S-04 | Medium | **Configuration errors are silent.** Unknown keys and misspelled sections are ignored, and invalid values fall back to defaults (`"twenty"` → 20, `"soon"` → 0.0). Retry counts validate strictly, so validation policy is inconsistent within one module. | Reproduced with `[execution] max_ouput_tokens`, `[exectuion]`, `config.py:1908-1975` |
| S-05 | Medium | **Network egress policy is not centralized.** Four clients bypass the shared HTTP client: `session_service/clients.py`, `background/terminals.py`, `background/notifications.py` (its own private-network guard), and `workflows/name_that_ui.py` (blocking `urllib`). Both `httpx` and `aiohttp` are core dependencies. | `grep` audit |
| S-06 | Low | The 8 `S608` string-built SQL hits are constant `WHERE` fragments with bound parameters and are **not** injectable; suppress them with a justification rather than leaving the rule disabled. `tools/exec` writes reports to a hard-coded `/tmp` prefix instead of `tempfile`. | `background/inbox.py:441`, `schedules/store.py:331`; `tools/exec/__init__.py:700` |

### 6.4 Architecture and design

**A-01 (High) — The orchestration layer is one dependency cycle.** With
function-local imports counted, 34 modules across `background`, `commands`,
`runners`, `runs`, `subagents`, `tui`, `workflows` and `worktrees` form a single
strongly connected component. Bidirectional package edges include
runners↔tui (14/7), runners↔workflows (11/12), background↔runners (12/5) and
commands↔tui (10/9). The presentation store `tui/conversation_store.py` imports
`cli.context` and `workflows.plugin`. The package hides this with **1,303
function-local imports**, so the cycle never fails at import time and nothing
prevents it from growing.

**A-02 (High) — God modules sit exactly where churn is highest.**

| Unit | Size | Shape | Churn | Coverage |
|---|---|---|---:|---:|
| `runners/tui_session.py` / `TUISession` | 4,631 lines; class 2,870 | 66 methods; module imports 70 internal modules; 45 broad excepts; `_build_session_context_impl` 682 lines | 122 commits | 65.2% |
| `runners/agent_turn.py` / `AgentTurnRunner` | 3,070; class 2,019 | `_stream` 657 lines, complexity 71 | 79 | 85.8% |
| `tui/workspace/background_manager.py` | 3,038; class 2,918 | 71 methods; `handle_key` complexity 141, `render` 98 | 17 | 81.3% |
| `background/worker.py` | 2,743 | `run_worker` is one 973-line function with 8 closures sharing state through `nonlocal` | 27 | 78.0% |
| `background/supervisor.py` / `BackgroundSupervisor` | 2,340; class 2,110 | 55 methods; 17 silent exception handlers | 23 | 74.5% |
| `config.py` | 2,844 | 17 settings dataclasses plus a 327-line hand-written TOML mapper | 52 | 91.6% |

`TUISession` combines turn routing, workflow lifecycle (dispatch, resume,
pause, fail, finalize, continuation), extension reloads and compaction. These
are separable services with distinct tests.

**A-03 (High) — The turn-execution API is private and parameter-exploded.**
`runners.agent_turn._run_agent_turn` takes **33 parameters** and is imported by
6 modules in 3 packages. Every runner rebuilds roughly 30 arguments from
`WorkflowConfig` (`CodePlanRunner._run_turn` 115 lines,
`CreateWorkflowRunner._run_turn` 144 lines). Because there is no injectable turn
executor, tests replace `runner._run_turn` by monkeypatching — **60 times** —
and production code reads the model through lauren-ai internals
(`agent_runner._transport._config.model`) in three runners.

**A-04 (High) — There is no runner framework base.** Six production runners
(`goal_flow`, `make_book`, `reconstruct_site`, `site_imitate`, `copy_website`,
`make_agenthicc_tool`) subclass the concrete `CodePlanRunner` only to reuse its
plumbing and `run_phase()`. Five of them override `run()`/`resume()` with
incompatible return types, which violates substitutability and accounts for 11
of the 16 `[override]` mypy errors (10 `run`/`resume`, 1 `_plan`). The other 5
narrow `build_params(source)` from the base's `Mapping[str, object]` to
`dict[str, object]`. `BaseWorkflowRunner` types `run` and `resume` as returning
`object`, so the contract carries no information. The `create_workflow`
authoring template (`inspection_tools.py`) teaches all three patterns —
`ReleaseCheckRunner(CodePlanRunner)`, shared memory typed `object`, and a
`dict[str, object]` `build_params` — so **every generated workflow inherits the
defects**. The template is a string and is never type-checked.

**A-05 (High) — Workflow lifecycle orchestration is implemented three times.**
`TUISession` (≈1,000 lines), `runners/headless.execute_workflow` (427 lines,
complexity 103) and `background/worker._handle_background_workflow_command`
(356 lines) each orchestrate run, resume, pause and failure;
`runners/workflow_handle.WorkflowRunHandle` is only partially adopted. The
defects cluster accordingly: **26 PRD titles since PRD-148 concern resume,
recovery, checkpoints or interrupts**, including two filed on 2026-10-20
(PRD-248, PRD-249).

**A-06 (Medium) — Domain workflows are bundled into the core package.** 34,332
non-blank lines (28% of `src/`) live in `workflows/`. Of those,
`reconstruct_site` alone is 9,258 lines and three workflows overlap in purpose
(`reconstruct_site`, `copy_website`, `site_imitate`). These modules contribute
**235 of the 240 mypy errors**. `reconstruct_site` carries a dual path:
`runner.py` defines `ReconstructSiteRunner(PhaseRunner)` where `PhaseRunner` is
an alias of the identically named class in `phase_impl.py`, plus
`LEGACY_RECONSTRUCT_PHASE_PLAN`. `name_that_ui.py` is a lone module at the
package root, unlike every other workflow.

**A-07 (Medium) — Durable storage is hand-rolled about twenty times.** Stores
include the conversation journal, workflow checkpoints, the background store,
terminals, runs, schedules, two inboxes, the notification outbox, the session
event store, the idempotency and usage ledgers, session ownership, the loop
store, worktree manifests and trust, the file cache and project memory. There
are five SQLite connection helpers with different busy timeouts (1, 5, 10 s)
and durability pragmas, at least five copies of the `fcntl` lock routine, and
no shared atomic-write or schema-version primitive.

**A-08 (Medium) — Configuration is a monolith with duplicated defaults.**
Defaults are written once on the dataclass and again in the TOML mapper — 61
times — so they can diverge. Validation policy varies by field (S-04).

**A-09 (Medium) — The runtime is coupled to lauren-ai private modules.** The
package imports 270 names through `lauren_ai._*` paths. **243 of them are
exported publicly** (`tool`, `ShortTermMemory`, `AgentRunnerBase`,
`AgentConfig`, …) and could be imported from `lauren_ai` mechanically. The
remaining 27 imports use 17 genuinely private names, including
underscore-private functions (`_build_transport`, `_complete_tool_results`,
`_add_to_tool_map`, `_NO_REPLACE`). With `lauren-ai>=1.6.0,<2`, any minor
release may break them without violating semantic versioning.

**A-10 (Medium) — The public API surface is undefined.** 165 modules declare
`__all__`, exporting 909 unique names. There is no public/internal module
convention, so every export is implicitly public, and `llms-full.txt` documents
35% of them while the checker enforces 22.

**A-11 (Low) — Compatibility paths contradict the stated policy.** The project
policy is to replace superseded code without shims, yet `legacy` appears on 223
lines in 67 files and `compat` on 189 lines in 67 files. The test-support
package `agenthicc.testing` and a verbatim reference script
(`workflows/make_book/build_book.py`, exempt from lint and mypy) ship in the
production wheel.

**A-12 (Low) — Input capabilities reach into session internals.**
`tui/input/capabilities.py` performs 104 private-attribute accesses on another
object (`session._buf`, `session._paste`, `session._push()`, …), so the
capability boundary exists in name only.

### 6.5 Complexity and size

| Measure | Count |
|---|---:|
| Functions and methods | 4,738 |
| Complexity > 15 / > 30 / > 50 | 276 / 57 / 21 |
| Functions longer than 100 / 200 lines | 158 / 38 |
| Functions with more than 8 parameters | 70 |
| Classes with more than 40 methods | 7 |
| Files longer than 1,000 / 2,000 lines | 29 / 10 |

The ten most complex functions are listed in Appendix B. Complexity
concentrates in the same hotspots as churn and low coverage — the combination
that predicts future defects.

### 6.6 Duplication

Exact clone detection finds little (192 redundant lines). Type-2 detection —
identical structure after renaming identifiers and literals — finds **29 clone
groups and 2,948 redundant lines**. Fifteen of those groups — **121 functions
and 2,570 redundant lines (87%)** — are hand-written phase-transition tools or
the per-phase factories that build them:

| Pattern | Copies | Lines each | Redundant lines |
|---|---:|---:|---:|
| Per-phase tool factory (`copy_website`, `reconstruct_site`, `goal_flow`) | 18 | 27 | 459 |
| Verification-gate factory (`reconstruct_site`) | 10 | 47 | 423 |
| Approve/verify transition tool (`reconstruct_site`, `make_agenthicc_tool`) | 23 | 17 | 374 |
| Submit/confirm transition tool (four workflows) | 19 | 15 | 270 |
| Submission factory (`reconstruct_site`) | 10 | 28 | 252 |
| Validation-pass factory (`reconstruct_site`) | 5 | 52 | 208 |
| Rejection tools (`reconstruct_site`) | 10 + 5 | 16 / 26 | 248 |
| `make_validation_tools` / `make_reviewer_tools` and their approve/reject tools | 2 + 4 | 75 / 26 | 153 |
| Other transition clones (`copy_website`, `reconstruct_site`, `make_book`, `site_imitate`) | 15 | 10–42 | 183 |
| Non-transition clones: browser tool factories (96 lines each), supervisor defer paths, `shell` / `run_bash`, store `_lock()`, plugin discovery, and 9 smaller groups | 2–3 per group | 10–96 | 378 |

The dominant cause is a missing abstraction: every phase transition is written
by hand as a `@tool()` closure that validates fields, writes a dict and sets an
`asyncio.Event`.

### 6.7 Type safety

- **The gate regressed.** PRD-140 recorded zero errors; there are now 240.
  Sixteen are `[override]` errors from A-04; most others are `object` values
  flowing out of untyped dictionaries (`arg-type` 148, `call-overload` 25).
- **The ratchet only moves up.** `docs/reference/type-safety-baseline.json`
  changed in 33 commits between 2026-07-23 and 2026-09-29, and 31 of them
  raised the `getattr` budget: `getattr` 214 → 777, `hasattr` 10 → 34,
  `type: ignore` 48 → 61. The baseline still records `"functions": 1477` and
  `"source_files": 172` (actual: 4,738 and 308).
- **Payloads are re-validated instead of parsed.** `dict[str, object]` appears
  1,127 times; the idiom `x if isinstance(x, T) else default` appears 148 times;
  there are 2,235 `isinstance` calls and 158 `cast()` calls, but only 12
  `TypedDict`s. Phase-tool data, tool results and checkpoint payloads should be
  parsed once at the boundary into typed models.
- **A production feature is exempt.** `[tool.mypy.overrides]` sets
  `ignore_errors = true` for `agenthicc.tools.mcp`, although MCP is a supported
  production integration (PRD-172), as well as for Outlook, S3 and Pyodide.
- `WorkflowPlugin.mode_bindings` and `phases` are mutable class defaults shared
  by every subclass that does not override them (67 `RUF012` findings
  overall).

### 6.8 Error handling and observability

There are 418 `except Exception`/`BaseException` handlers. Of these, 63
re-raise, 65 log, and **104 swallow the error silently** (`pass`, `continue`, or
a constant return). Counting every exception type, 290 handlers have an empty
or constant body. The worst placements are in infrastructure where silence hides
root causes: the reactive core (D-06), the kernel processor (D-08), the
background supervisor (17 silent handlers) and the workflow checkpoint store
(16). There is no project-wide policy on which failures must be logged,
surfaced to the user, or recorded as events.

### 6.9 Lint configuration

`[tool.ruff.lint]` selects nothing beyond the defaults, but the code is written
as if pylint- and bugbear-style rules were enforced:

- **All 1,305 `noqa` directives are unused** (`RUF100`). 1,285 of them — 1,013
  for `PLC0415` and 272 for `BLE001` — name rules that are not enabled.
- Read-only runs of stricter families show real, unenforced findings:

| Family | Findings | Notable rules |
|---|---:|---|
| `B` (bugbear) | 91 | `B023` ×5, `B904` ×5, `B006` ×1 |
| `ASYNC` | 55 | D-10 |
| `RUF` (excluding `RUF100`) | 215 | `RUF006` ×8 (D-02), `RUF012` ×67 |
| `S` (bandit) | 180 | `S110` ×71, `S602/S604` ×3, `S608` ×8 (S-06) |
| `BLE` | 95 | Blind excepts with no suppression at all |
| `C90` (default threshold 10) | 261 | Section 6.5 |
| `DTZ` | 6 | Naive datetimes; `utcnow()` deprecation warning in the suite |
| `PLW` | 29 | `PLW2901` ×11, `PLW1510` ×3 |

### 6.10 Test suite

**Strengths.** The suite is broad (4,438 passing tests: 3,946 unit, 317
integration, 172 E2E and 3 performance; 16 skipped) and fast where it should be
(unit 53 s). E2E tests drive real CLI subprocesses and PTY rendering. The full
suite is green.

**Findings.**

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| T-01 | High | Coverage is below the floor and under-measured | 84.5% statement coverage; no branch coverage; code exercised by subprocess E2E tests is not measured, so CLI modules appear barely tested (`cli/commands/schedules.py` 17.0%, `cli/commands/worktrees.py` 18.4%) |
| T-02 | High | The orchestration core is the least covered code | `runners/` 79.9%, `cli/` 61.7%, `commands/builtins.py` 65.5%, `runners/tui_session.py` 65.2% |
| T-03 | High | Tests are coupled to private implementation | 451 assignments to private attributes of objects under test (`_run_turn` ×60, `_run_phase` ×18, `_agent_task` ×16); 125 `monkeypatch.setattr` calls on private module names; 9 fakes of `agent_runner._transport._config` |
| T-04 | Medium | Duck-typed doubles replace contracts | 758 `SimpleNamespace` stubs, 594 `MagicMock`/`AsyncMock` instances, 181 `patch` calls |
| T-05 | Medium | Timing-based waits invite flakiness | 45 `time.sleep` and 52 non-zero `asyncio.sleep` calls in tests |
| T-06 | Medium | Marker-based selection is unreliable | 43 test files carry no `unit`/`integration`/`e2e` marker; no `--strict-markers`; the full `tests` Nox session runs with `-W ignore` |
| T-07 | Medium | Resource leaks are tolerated | About 105 `ResourceWarning`s, mostly from D-01, and one "coroutine was never awaited" warning per run; warnings are not errors |
| T-08 | Low | Helpers are duplicated across files | 11 local processor fixtures alongside `running_processor`; 14 local runner builders; 27 direct `WorkflowConfig(...)` constructions in 20 files |
| T-09 | Low | Tests self-skip on missing APIs | `test_config_prd21.py` skips with "parameter not yet added to load_config", so an API regression would skip rather than fail |
| T-10 | Low | Organization follows delivery history | 50 files are named after PRDs and 32 after coverage pushes (`test_coverage_boost.py`, `test_background_remaining_coverage.py`), which makes a module's tests hard to find |
| T-11 | Low | Property-based testing is barely used | 2 `@given` tests, although `hypothesis` is a development dependency and the reducer, codecs and config parser suit it |

### 6.11 Documentation and process

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| P-01 | High | Guides describe controls that do not exist | S-03; `docs/guides/plugins.md:243`, `quickstart.md:117`, `configuration.md:500-504`, `memory.md:241` |
| P-02 | Medium | Reference tables drift | `docs/reference/fact-base.md`: 11 of 24 resolvable symbol citations no longer point at the symbol; the docs contain 306 hand-maintained `file.py:NNN` references |
| P-03 | Medium | The PRD lifecycle is unmanaged | 248 PRD files; 70 have no machine-readable status; 11 different status words; only 111 are linked from `prds/README.md`; 88 are draft, proposed or proposal |
| P-04 | Medium | Releases and the changelog do not exist in practice | `CHANGELOG.md` last changed 2026-09-20 (PRD-193); PRD-194 through PRD-249 are absent; everything is `[Unreleased]` at version 0.1.0 |
| P-05 | Medium | Commits are hard to review, bisect and summarize | 441 of 468 commit subjects start with `feat`, including bug fixes; the 90th-percentile commit touches 24 files and about 3,000 lines; several bundle multiple PRDs |
| P-06 | Low | Generated artifacts are tracked | `coverage.json` (2.5 MB, stale since 2026-09-28) is committed while `coverage.xml` and `htmlcov/` are ignored |
| P-07 | Low | Repository identity is inconsistent | `CONTRIBUTING.md` clones `github.com/agenthicc/agenthicc`; the remote and PRD metadata use `jymchng/agenthicc` |

## 7. Root causes

1. **Gates are advisory.** Failing blocking jobs on `main` mean that no single
   change is ever forced to restore health, so debt accumulates by default.
2. **Baselines are editable without review.** A ratchet that a contributor can
   raise in the same commit that regresses it measures nothing.
3. **Delivery outpaces consolidation.** At about 25,000 lines a month, each PRD
   adds code beside existing code instead of refactoring a shared seam — hence
   transition-tool clones, `CodePlanRunner` subclassing and three lifecycle
   implementations.
4. **There are no architectural fitness functions.** Nothing measures or limits
   layering, complexity, file size or private access, so the 34-module cycle
   grew invisibly behind function-local imports.
5. **Private seams make refactoring expensive.** With hundreds of tests patching
   private attributes, restructuring a hotspot breaks tests that should not
   care, which discourages the restructuring.
6. **Fix-forward in hotspots.** Recovery and resume bugs are fixed by new PRDs
   that patch `TUISession`, `workflow_recovery` and runners locally (26 such PRD
   titles since PRD-148) rather than by consolidating the lifecycle.

## 8. Goals and non-goals

### Goals

1. Make every documented gate green and blocking, and keep it that way.
2. Fix every verified defect and close the unconsented-execution paths.
3. Introduce the missing seams — an injectable turn executor, a generic runner
   base, declarative transition tools and one workflow lifecycle — so hotspots
   can be decomposed safely.
4. Replace advisory baselines with enforced fitness functions that can only
   tighten.
5. Make documentation and configuration truthful: no documented control
   without enforcement.

### Non-goals

- Rewriting the TUI, kernel or workflow engine.
- Replacing lauren-ai, or adding product features.
- Reaching 100% coverage or zero complexity findings.
- Deleting historical PRDs; this PRD only labels and indexes them.

## 9. Improvement programme

Priority: **P0** — start immediately, finish within two weeks; **P1** — within
six weeks; **P2** — within the quarter. Each work item lists its acceptance
evidence.

### WS-1 — Restore and enforce the gates (P0)

| Item | Work | Acceptance |
|---|---|---|
| 1.1 | Format the 8 files; run `ruff format --check` on every tracked file regardless of permissions | `nox -s lint` passes in a clean checkout |
| 1.2 | Bring `mypy src/agenthicc` to zero; fix the 5 core errors and resolve the 235 workflow errors with WS-4.2 or a time-boxed per-module override that names an owner and expiry | `nox -s typecheck` green |
| 1.3 | Add branch protection on `main` that requires lint, typecheck, type_audit, unit, integration, E2E, docs and llms jobs | A failing job blocks merge |
| 1.4 | Make the release workflow depend on the full test and lint matrix for the tagged commit; derive the version from the tag (for example `setuptools-scm`) and the CLI version from package metadata | A tag on a red commit cannot publish; `agenthicc --version` equals the wheel version |
| 1.5 | Fix CodeQL (`--extra all` → declared extras) and run `mkdocs build --strict` in `docs.yml` | Both workflows green |
| 1.6 | Merge the two `dev` sets into one dependency group; declare `twine`; remove duplicated core/extra dependencies | `uv sync` once provides every tool a Nox session calls |
| 1.7 | Add Python 3.11 to the matrix, or raise `requires-python` to 3.12; add a Windows unit-test job for the terminal, lock and path code | The tested matrix equals the claimed matrix |
| 1.8 | Run `test_cassette_replay.py` in CI | The replay test runs on every pull request |
| 1.9 | Require code-owner review for `docs/reference/type-safety-baseline.json` and any quality baseline; `type_audit` fails if a baseline value *increases* | Baselines can only decrease without explicit owner approval |

### WS-2 — Fix the verified defects (P0)

| Item | Work | Acceptance |
|---|---|---|
| 2.1 | D-01: use `with closing(self._connect()) as conn, conn:` (or one shared helper) in `memory/layers.py` | Regression test asserts the connection is closed; the project-memory tests emit no `ResourceWarning`s |
| 2.2 | D-02: add a supervised `TaskGroup`/background-task registry that holds references, logs exceptions and is drained on shutdown; await the `WorkflowRunDiscarded` emit | `RUF006` enabled with zero findings |
| 2.3 | D-03: remove `EventProcessor._event_log` from production, or replace it with an opt-in bounded ring buffer for tests | Memory does not grow with event count; tests use a test sink |
| 2.4 | D-04: one tested `FileLock` primitive (`fcntl`/`msvcrt`, fail-closed with a typed error) used by every store | A lock that cannot be acquired raises; Windows CI covers it |
| 2.5 | D-05: read `DEPENDENCIES` from the AST; import each plugin exactly once | A plugin with a side-effect counter runs it once |
| 2.6 | D-06: log subscriber exceptions with the signal name; add `Computed.dispose()` | A failing subscriber is logged and the remaining subscribers still run |
| 2.7 | D-07/D-08: an event registry that maps every emitted type to a handler or an explicit "projection-only" marker; delete the 10 dead handlers; reducer failures produce an error event; subscriber drops are counted and logged; `drain()` uses `queue.join()`; file writes leave the loop | A test enumerates every `Event.create` literal and asserts it is registered |
| 2.8 | D-09/D-10: fix the `B023`, `B006`, `B904` and `ASYNC` findings, starting with the skill injection path | `B` and `ASYNC` enabled with zero findings |

### WS-3 — Consent and configuration truth (P0)

| Item | Work | Acceptance |
|---|---|---|
| 3.1 | S-01: never execute `` !`command` `` placeholders from untrusted sources; for trusted skills, run them asynchronously through the exec tool path so approvals, mode ceilings and the sandbox apply | A project skill in an untrusted repository cannot start a process; a test proves it |
| 3.2 | S-02: one trust service, digest-bound, for tools, commands, workflows, modes, agents, skills and CLI plugins; replace both existing mechanisms; untrusted extensions are listed and skipped, never imported | `plugins/trust.check_trust` is either the single implementation or deleted; opening an untrusted repository imports no project code |
| 3.3 | S-03: wire or delete each of the 15 inert settings; correct the guides | A test asserts every settings field has at least one reader or an explicit `runtime-only` marker |
| 3.4 | S-04: schema-driven configuration — derive the TOML mapping from the dataclass fields, reject unknown keys and sections, and report invalid values with their path; keep defaults in one place | A misspelled key or section fails `agenthicc config show` with the exact path |
| 3.5 | S-05: route every outbound request through the shared client and allow-list, or document the exempt loopback cases; standardize on one HTTP library | A test fails if `urllib.request`, `aiohttp.ClientSession` or a bare `httpx.AsyncClient` appears outside the allowed modules |

### WS-4 — Turn-execution and runner seams (P1)

| Item | Work | Acceptance |
|---|---|---|
| 4.1 | Introduce a public `AgentTurnRequest` dataclass and a `TurnExecutor` protocol; `WorkflowConfig` supplies the executor; `_run_agent_turn`'s 33 parameters become one request | Runners and the background worker no longer import `runners.agent_turn`; tests inject a fake executor instead of patching `_run_turn` |
| 4.2 | Add a generic `StateMachineRunner[CtxT]` base that owns turn execution, phase reporting, model selection, checkpoints and `run_phase`; `CodePlanRunner` and every bundled runner extend it, and none subclasses `CodePlanRunner` | Zero `[override]` errors; `BaseWorkflowRunner` is generic over its context |
| 4.3 | A declarative transition-tool builder — name, required fields, validation, target event — replacing hand-written closures | The 121 cloned transition tools and factories become specifications; Type-2 redundancy falls by at least 2,000 lines |
| 4.4 | Make the `create_workflow` runner example a real, type-checked and tested module that `show_example_workflow` reads with `inspect`, and point it at the new base class | Generated workflows type-check; the template can no longer drift |
| 4.5 | Expose the model id and transport configuration through a public accessor (upstream in lauren-ai, or one local adapter) | No production code reads `_transport._config` |

### WS-5 — Decompose the hotspots (P1–P2)

| Item | Work | Acceptance |
|---|---|---|
| 5.1 | Extract from `TUISession`: `WorkflowLifecycleService` (one implementation shared with headless and background), `ExtensionReloadService`, `SessionContextBuilder` and `TurnRouter` | `tui_session.py` under 1,500 lines; coverage above 85% |
| 5.2 | Replace `background/worker.run_worker` with a `BackgroundWorker` class whose closures become methods | No function over 150 lines in `background/` |
| 5.3 | Split `agent_turn._stream` into streaming, tool-transaction and compaction collaborators | No function in `runners/` above complexity 30 |
| 5.4 | Turn `BackgroundManager.handle_key` and `render` into per-view key maps and renderers | No function above complexity 30 in `tui/` |
| 5.5 | Split `config.py` into provider profiles, settings models and the loader | No module over 1,000 lines in the config package |

### WS-6 — Layering and dependency hygiene (P1)

| Item | Work | Acceptance |
|---|---|---|
| 6.1 | Declare layer contracts with `import-linter` or an equivalent test, counting function-local imports. Layers, lowest first: `kernel`; `config`, `memory`, `tools`; `workflows`; `runners`, `background`; `tui`, `cli` — a module may import only from its own layer or lower. Move the reactive presentation store (`tui/conversation_store.py`, `reactive.py`) below `runners`, or reach it through a port, because runners consume it today | CI fails on a violation; the largest cycle shrinks from 34 to 0 |
| 6.2 | Replace the 243 private-path imports of public lauren-ai names; move the 27 truly private uses into one adapter module and propose upstream APIs | A banned-import rule (`TID251`) allows `lauren_ai._*` only in the adapter |
| 6.3 | Define the public API: make internal modules private or list the supported modules; check `llms-full.txt` against that surface | The checker covers the declared surface at 100% |
| 6.4 | Give input capabilities a typed `InputSessionPort` instead of private access | `tui/input/capabilities.py` performs no private access |
| 6.5 | Delete compatibility and legacy paths whose callers are gone, starting with the `reconstruct_site` dual runner | `legacy`/`compat` identifiers fall by at least half; one `ReconstructSiteRunner` |

### WS-7 — Persistence primitives (P2)

Provide one SQLite helper (connection lifecycle, WAL, busy timeout, schema
version and migration hook), one atomic-write helper and the `FileLock` from
WS-2.4, and migrate the stores to them. **Acceptance:** every store uses the
shared primitives; a corruption and concurrent-writer test matrix runs against
each store; durability pragmas are uniform or documented per store.

### WS-8 — Bundled workflows (P2)

Move domain workflows (`reconstruct_site`, `copy_website`, `site_imitate`,
`make_book`, `ui_ux_pro_max`, `name_that_ui`, `make_agenthicc_tool`) into a
separate distribution or optional extra built on the public runner base, and
consolidate the three website-cloning workflows or document why each exists.
**Acceptance:** the core package contains only framework workflows
(`default`, `code_plan`, `create_workflow`, `goal_flow`, `parallel_code_plan`);
the extracted package passes mypy; `build_book.py` is a data file, not a
package module.

### WS-9 — Test architecture (P1)

| Item | Work | Acceptance |
|---|---|---|
| 9.1 | Enable branch coverage and subprocess coverage (`COVERAGE_PROCESS_START`); set the floor to the measured value and raise it monthly | The coverage session is green and measures CLI subprocesses |
| 9.2 | Shared fixtures for `WorkflowConfig`, processors and runners; delete local copies | No local `processor` fixture; one `make_workflow_config` factory |
| 9.3 | Replace private patching with the WS-4 seams | Private-attribute assignments in tests fall by at least half |
| 9.4 | `--strict-markers`, auto-apply markers by directory, `filterwarnings = error` for `ResourceWarning` and unawaited coroutines; remove `-W ignore` | No unmarked tests; warnings fail the run |
| 9.5 | Replace sleeps with events, `drain()` or fake clocks | `time.sleep` in tests only where a real clock is the subject |
| 9.6 | Property-based tests for `root_reducer`, the checkpoint codecs and the config parser | At least one Hypothesis suite per area |
| 9.7 | Re-home tests by module ownership; keep PRD numbers in docstrings rather than file names | New tests follow `tests/<layer>/<package>/` |

### WS-10 — Documentation, measurement and delivery process (P1)

| Item | Work | Acceptance |
|---|---|---|
| 10.1 | Commit `scripts/quality_report.py` (the analyzers in Appendix A) and publish its JSON as a CI artifact | Every number in Section 10 is produced by CI |
| 10.2 | Replace hand-maintained `file.py:NNN` references with symbol references resolved by a checker | The checker fails on an unresolvable symbol |
| 10.3 | Generate the PRD index from frontmatter with one status vocabulary (`proposed`, `accepted`, `implemented`, `superseded`, `rejected`); backfill the 70 missing statuses | `prds/README.md` lists all PRDs with status; CI validates frontmatter |
| 10.4 | Conventional commit types (`fix`, `feat`, `refactor`, `test`, `docs`, `ci`), one PRD per commit series, and a generated changelog | The changelog is generated at release time |
| 10.5 | Remove `coverage.json` from git and ignore it; correct the repository URL in `CONTRIBUTING.md` | No generated artifacts tracked |

## 10. Fitness functions and budgets

Baselines are from this study. A budget may only tighten; raising one requires
code-owner approval and a linked issue.

| Metric | Baseline | 30 days | 90 days | Enforced by |
|---|---:|---:|---:|---|
| `ruff format` violations | 8 | 0 | 0 | `nox -s lint` |
| mypy errors in `src/` | 240 | 0 (or owned, expiring overrides) | 0, no `ignore_errors` for production modules | `nox -s typecheck` |
| Unused `noqa` | 1,305 | 0 | 0 | `RUF100` |
| Enabled ruff families | defaults (59 rules) | + `B`, `ASYNC`, `RUF006`, `RUF012`, `BLE`, `DTZ`, `PLW` | + `C90`, `SIM`, `UP`, `PERF`, `S` (justified ignores) | `ruff check` |
| Functions with complexity > 30 | 57 | ≤ 57 | ≤ 20 | `C901` per-file ratchet |
| Functions > 200 lines | 38 | ≤ 38 | ≤ 10 | quality report |
| Files > 2,000 lines | 10 | ≤ 10 | ≤ 3 | quality report |
| Largest import cycle (modules) | 34 | ≤ 34 | 0 | layer contracts |
| Function-local imports | 1,303 | ≤ 1,303 | ≤ 400 | quality report |
| Statement / branch coverage | 84.5% / unmeasured | 86% / measured | 90% / 85% | `nox -s coverage` |
| `getattr` calls | 762 | ≤ 762 | ≤ 500 | `type_audit`, baseline lowered |
| Imports via `lauren_ai._*` | 270 names | ≤ 27 | ≤ 10, in one adapter | `TID251` |
| Silent broad `except` | 104 | ≤ 60 | ≤ 15 | `BLE` and review |
| Dangling tasks (`RUF006`) | 8 | 0 | 0 | `ruff check` |
| Inert configuration fields | 15 | 0 | 0 | field-reader test |
| Private-attribute writes in tests | 451 | no new | ≤ 200 | quality report |
| Type-2 clone redundancy (lines) | 2,948 | ≤ 2,948 | ≤ 900 | quality report |
| `ResourceWarning`s per suite run | about 105 | 0 | 0 | `filterwarnings = error` |
| `__all__` names documented | 35% | public surface declared | 100% of the declared surface | `llms_check` |

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Making gates blocking stalls delivery | WS-1 lands in small changes; time-boxed per-module overrides with owners keep the gate green while debt is paid |
| Seam refactors break behaviour | WS-4 introduces the new seams beside existing behaviour and migrates one runner at a time behind the existing E2E and cassette suites |
| The trust gate disrupts existing project extensions | First run lists the extensions that need trust and how to grant it; trust is digest-bound and auditable |
| Extracting bundled workflows breaks users of those workflows | Ship them as an extra with the same entry names and record the move in the changelog |
| Stricter lint produces large mechanical diffs | Apply automatic fixes in dedicated commits that change no behaviour, with no review burden mixed in |
| Metrics become targets | Budgets accompany findings rather than replace review; the quality report is a signal, not a substitute for design |

## 12. Definition of done

This PRD is complete when:

1. Every gate in Section 4 is green on `main` and required by branch protection.
2. All verified defects (D-01 through D-10) and security findings S-01 through
   S-05 are fixed with regression tests.
3. Workstreams WS-4 and WS-6 have merged: one turn executor, one generic runner
   base, one workflow lifecycle, layer contracts enforced, and no import cycle.
4. The 90-day column of Section 10 is met, or each miss has an owner and date.
5. `scripts/quality_report.py` runs in CI and its trend is published.

## 13. Relationship to existing PRDs

| PRD | Item | Status verified by this study |
|---|---|---|
| PRD-138 P0.4 | Security baseline and plugin trust | **Open** — superseded in scope by WS-3 |
| PRD-138 P0.5 | Release metadata and check gate | **Partly open** — version is still hard-coded; 3.11 untested; gates not blocking (WS-1) |
| PRD-138 P1.1 | Workflow single source of truth | **Partly open** — see A-04 and A-05 (WS-4, WS-5.1) |
| PRD-138 P1.2 | Event processor failure semantics | **Open** — D-07 and D-08 (WS-2.7) |
| PRD-138 P1.7 | Configuration validation | **Open** — S-04 (WS-3.4) |
| PRD-138 P2.6 | PRD governance | **Open** — P-03 (WS-10.3) |
| PRD-140 | Type-safety contract | Implemented on 2026-07-23, **since regressed** — 240 errors and an eroded ratchet; WS-1.2, WS-1.9 and WS-4.2 restore it |
| PRD-207 | Async-first runtime | Undermined by the D-10 findings; WS-2.8 |
| PRD-170, PRD-248, PRD-249 | Workflow recovery | Symptoms of A-05; WS-5.1 consolidates the lifecycle these PRDs patch |

## Appendix A — Reproduction

All commands run from the repository root on commit `2d66765` with
`uv sync --extra dev` and Python 3.13.

```bash
# Project gates
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run mkdocs build --strict -d /tmp/site
uv run nox -s llms_check
uv run pytest tests/ -q --durations=60 --cov=agenthicc --cov-report=json:/tmp/cov.json
uv build --out-dir /tmp/dist
uv sync --extra all --extra dev --dry-run          # reproduces F-04

# Latent lint findings (read-only)
uv run ruff check src/ --select RUF100 --statistics
for f in B ASYNC BLE S PERF SIM UP RUF PLW C90 DTZ; do
  uv run ruff check src/ --select "$f" --statistics --exit-zero
done

# Ratchet history
git log --format=%h -- docs/reference/type-safety-baseline.json

# Growth
for d in 2026-07-01 2026-07-23 2026-08-15 2026-09-15 2026-10-20; do
  git rev-list -1 --before="$d 23:59" HEAD
done
```

**AST analyzers** — to be committed as `scripts/quality_report.py` (WS-10.1):

| Metric | Definition |
|---|---|
| Complexity | 1 + count of `if`, loops, `except`, `with`, `assert`, comprehensions, conditional expressions, `match` cases, and (operands − 1) per boolean operator, excluding nested definitions |
| Function length | `end_lineno − lineno + 1`, decorators excluded |
| Type-2 clones | Functions of 10+ lines whose AST is identical after renaming every identifier, argument and literal and dropping docstrings, decorators and annotations |
| Import cycles | Tarjan's strongly connected components over module edges, with and without function-local imports; `TYPE_CHECKING` imports excluded |
| Broad-exception outcome | Handlers catching `Exception`/`BaseException`, classified as re-raise, log or warn, silent (`pass`, `continue`, constant return), or other |
| Inert configuration | Settings fields with no attribute read, `getattr` string, or `self.` use outside their definition and parsing |
| Private access | `x._name` where `x` is not `self` or `cls`; imports from `lauren_ai._*` compared with `lauren_ai.__all__` |

## Appendix B — Most complex functions

| Complexity | Lines | Function |
|---:|---:|---|
| 141 | 448 | `tui/workspace/background_manager.py` — `BackgroundManager.handle_key` |
| 106 | 355 | `workflows/goal_flow/runner.py` — `checkpoint_context_from_payload` |
| 103 | 427 | `runners/headless.py` — `execute_workflow` |
| 98 | 315 | `tui/workspace/background_manager.py` — `BackgroundManager.render` |
| 94 | 368 | `workflows/create_workflow/validation.py` — `validate_workflow_file` |
| 77 | 210 | `workflows/reconstruct_site/runner.py` — `_rehydrate_evidence` |
| 73 | 214 | `workflows/checkpoint.py` — `from_dict` |
| 71 | 657 | `runners/agent_turn.py` — `AgentTurnRunner._stream` |
| 67 | 682 | `runners/tui_session.py` — `_build_session_context_impl` |
| 63 | 397 | `workflows/default/runner.py` — `WorkflowRunner._run_phase` |

Longest functions not shown above: `background/worker.py` `run_worker`
(973 lines), `workflows/create_workflow/inspection_tools.py`
`make_inspection_tools` (749), `workflows/make_book/builder.py`
`build_book_script` (550).
