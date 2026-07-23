---
title: "PRD-140: Type-Safety and Static Contract Hardening"
status: Proposed
version: 0.1.0
created: 2026-07-23
related_prds:
  - PRD-138  # Repository Improvement Roadmap
  - PRD-139  # OpenCode-Inspired Product Expansion and Privacy-First Advertisements
  - PRD-01   # AppState and Event System
  - PRD-04   # Tool Execution and Hooks
  - PRD-24   # Tool Plugin Discovery
  - PRD-51   # Filesystem Backend Protocol
  - PRD-81   # Workflow Revamp
  - PRD-103  # Workflow Extensibility Revamp
supersedes: []
tags:
  - typing
  - mypy
  - static-analysis
  - protocols
  - reliability
  - architecture
---

# PRD-140 — Type-Safety and Static Contract Hardening

Study date: 2026-07-23. This PRD turns the repository's current typing debt
into a staged engineering programme. It is intentionally about contracts and
verification, not about adding annotations mechanically or replacing the
runtime architecture.

## 1. Executive summary

agenthicc has meaningful type foundations: frozen kernel dataclasses, typed
workflow and command models, filesystem and trigger `Protocol`s, and a
documented rule against introducing avoidable `Any`. The repository does not
yet have a reproducible type-checking contract, however. The configured CI/Nox
path refers to mypy as an external executable that is absent from the declared
development dependencies, there is no `[tool.mypy]` or Pyright configuration,
and the pull-request type job is explicitly non-blocking.

The largest risk is not the number of explicit `Any` annotations alone. Runtime
contracts are frequently represented as `dict[str, object]`, bare `dict`/`list`
annotations, optional third-party objects, and unchecked `getattr()` calls.
Those choices move failures from the boundary where input is received to deep
inside reducers, workflow runners, tools, and the TUI. The first ephemeral
mypy baseline found 611 errors in 56 source files before any strict-mode
settings were enabled.

This PRD establishes mypy as the initial checker because the repository already
advertises and runs mypy in CI. It defines a typed vocabulary for JSON and tool
contracts, typed event payloads and configuration ingress, explicit protocols
for optional integrations, and a ratcheted path from a reproducible baseline
to strict source and test checking. Dynamic introspection remains allowed at
genuine plugin/provider boundaries, but it must be isolated, narrowed, and
tested rather than spread through core code.

## 2. Current-state study

### 2.1 Reproducibility and enforcement

The current state is:

| Surface | Evidence | Consequence |
|---|---|---|
| Declared checker dependency | `pyproject.toml` has no mypy or Pyright dependency in the project or dev groups | A clean `uv sync --extra dev` environment cannot run the documented type check |
| Checker configuration | No `[tool.mypy]`, `[tool.pyright]`, or checked-in type baseline exists | Defaults, import discovery, and strictness are implicit and unstable |
| Nox | `noxfile.py:typecheck` invokes `mypy` as an external command and describes it as optional | The local and clean-checkout commands disagree about whether type checking is available |
| CI | `.github/workflows/lint.yml` has a mypy job with `continue-on-error` for pull requests | Type regressions are visible but do not protect a merge |
| Contributor contract | `AGENTS.md`, `CLAUDE.md`, `README.md`, and `docs/contributing.md` advertise `uv run mypy src/agenthicc` | The advertised definition of done is currently not executable from the declared dev environment |

The command `uv run mypy src/agenthicc` currently fails before checking because
the executable is not installed. For this study only, an ephemeral checker was
run without changing project metadata:

```text
uv run --with mypy mypy src/agenthicc --no-pretty
Found 611 errors in 56 files
```

This is a default-mode baseline, not a strict-mode target. Enabling strict
options will initially expose more issues and must therefore happen through a
ratchet rather than an all-at-once flag change.

### 2.2 Repository inventory

The following counts were produced from the current tree on 2026-07-23. The
AST counts exclude `self` and `cls` when measuring missing function
annotations. They are inventory metrics, not a claim that every occurrence is
wrong.

| Metric | Baseline | Interpretation |
|---|---:|---|
| Python files under `src/agenthicc/` | 172 | Production surface in scope for the first checker gate |
| Production functions/methods | 1,477 | Scope of signature coverage measurement |
| Functions missing at least one non-`self`/`cls` annotation | 21 | 14 have missing parameter annotations; 17 have missing return annotations; categories overlap |
| Explicit `Any` in production annotations | 14 | Concentrated in `testing/recording_approval.py`, `testing/recording_transport.py`, and `workflows/plugin.py` |
| `getattr(...)` calls in production | 214 | Concentrated in agent turns, Outlook, TUI session wiring, executor, MCP, and plugin adapters |
| `hasattr(...)` calls in production | 10 | Mostly capability probing and compatibility paths |
| Bare `list` annotations in production | 16 | Most concentrated in tool wrappers and workflow phase helpers |
| Bare `dict` annotations in production | 125 | Most concentrated in filesystem, Git, Outlook, and tool wrappers |
| `# type: ignore` comments in production | 48 | Existing errors are suppressed or compatibility gaps are undocumented |
| Default mypy errors | 611 in 56 files | 283 `arg-type`, 143 `attr-defined`, 67 `override`, 34 `call-overload`, 20 `union-attr`, and smaller categories |

The explicit `Any` count is comparatively small because the code often uses
`object` or unparameterized containers instead. Those forms still erase useful
information: mypy reports `object` attribute errors in TUI/workflow paths and
`object` argument errors when event payloads and tool results are consumed.

### 2.3 Highest-leverage error clusters

The 611-error baseline is distributed unevenly:

| Area | Baseline errors | Root contract problem | First design response |
|---|---:|---|---|
| `workflows/default/runner.py` | 91 | Untyped kwargs dictionaries, plugin-tool collections, and an override whose base context is `object` | Type workflow context and turn invocation parameters; make runner base types generic or use a shared protocol |
| `config.py` | 70 | Raw TOML mappings are narrowed incompletely and dynamic fields are accessed through permissive fallbacks | Add typed raw-config mappings, field coercion helpers, and a typed effective-config model |
| `tools/fs/__init__.py` | 57 | Legacy class tools and backend results use bare dictionaries and untyped nested closures | Introduce JSON/tool result aliases and typed backend result records while preserving the tool envelope |
| `tools/git/__init__.py` | 49 | Tool arguments and parameter schemas are bare mappings; values from `dict[str, object]` are passed to subprocess helpers without narrowing | Define typed argument/result records and explicit scalar extraction |
| `kernel/reducer.py` | 38 | Every event payload access returns `object` because the event model has one generic payload mapping | Add event-specific payload types and validation before reducer dispatch |
| `tui/workspace/overlays/trigger_picker.py` and `help.py` | 34 combined | Reactive/UI fields are inferred as `object` at ownership boundaries | Type trigger, registry, and overlay host protocols directly |
| `tools/outlook/` | 35 combined | Optional COM objects have no stable local protocol and no installed stubs | Isolate COM behind a typed adapter and optional dependency/stub policy |

The most frequent error codes confirm that this is a contract problem rather
than an annotation-format problem:

- `arg-type` (283): boundary values are not narrowed before construction or
  invocation.
- `attr-defined` (143): values are typed as `object`, or a protocol is missing
  the capability that callers assume.
- `override` (67): base classes use `object`/broad signatures while concrete
  runners use narrower types, violating substitutability.
- `call-overload` (34): generic dictionaries are passed to APIs that require
  known iterables, keys, or scalar values.
- `union-attr` (20): optional resources are probed but not narrowed once.

### 2.4 Representative boundary findings

#### Kernel events and reducers

`kernel.events.Event` stores `payload: dict[str, object]`, then
`kernel.reducer` indexes that mapping as if every event had a known schema.
`Event.from_dict()` currently passes `object` values directly into fields typed
as `str`, `float`, enums, and nested mappings. This accounts for many reducer
errors and means malformed persisted data is diagnosed late. The durable JSONL
format is valuable and should remain, but decoding should produce a validated
typed payload or a structured invalid-record result before reduction.

#### Tool and JSON boundaries

The legacy `Tool` contract and many wrappers use `parameters: dict`,
`execute(args: dict, context: dict)`, and `-> dict`. Filesystem, Git, exec, and
Outlook wrappers repeat this pattern. Tool schemas are JSON-shaped and can be
open-ended, so they should not be forced into a false closed dataclass. They do
need a recursive JSON type, typed common envelopes, and per-tool argument
`TypedDict`s or validated argument models where keys are known.

The lauren-ai `@tool` decorator inspects annotations at runtime in some
modules. A migration must therefore preserve real runtime annotations for tool
wrappers and test generated schemas; adding postponed annotations or aliases
blindly can change registration behaviour.

#### Workflows and agent turns

Workflow runners pass `_turn_kwargs: dict[str, object]` into a strongly typed
agent-turn function, and the base runner accepts `object` where concrete
implementations require `WorkflowContext`. `WorkflowPlugin.build_params()` also
uses `dict[str, Any]`. This hides missing fields until runtime and creates
override errors. A shared typed context protocol, a typed turn-options record,
and generic or protocol-based resume methods are preferable to repeated casts.

#### Configuration

TOML, environment, and CLI values are correctly untrusted at ingress, but
their types are not consistently narrowed after parsing. The configuration
module already contains useful coercion and `from_dict` helpers; the next step
is to make those helpers the only path from `Mapping[str, object]` to settings
dataclasses. Internal code should read typed settings fields directly rather
than use `getattr` fallbacks for fields that the current model owns.

#### Dynamic integrations and optional platforms

`getattr` is legitimate when reading an optional provider signal, COM object,
or plugin-defined capability. It is not a substitute for typing a known
`SessionContext`, `ModeManager`, `WorkflowPlugin`, `MCP` registry, or transport.
The largest concentrations are `runners/agent_turn.py`,
`tools/outlook/win32_backend.py`, `runners/tui_session.py`, and
`tools/executor.py`. Optional imports for Outlook, S3, Pyodide, and YAML also
need a deliberate package/stub policy instead of silently turning modules into
`object`-typed islands.

## 3. Problem statement

The project advertises a typed, state-driven runtime, but its static contract
has four gaps:

1. There is no reproducible checker configuration or dependency, so a type
   check cannot be trusted as a clean-checkout gate.
2. Generic mappings are used across durable, security-sensitive, and
   cross-component boundaries without a validated schema or narrowing helper.
3. Dynamic access is distributed through core code, making provider/API drift
   appear as late runtime failures and making ownership boundaries difficult to
   inspect.
4. CI reports type failures without enforcing a ratchet, so new debt can be
   added while old debt remains unmeasured.

This weakens the safety properties already sought by PRD-138: frozen kernel
state can still be structurally typed while its event inputs are not; a tool
can have capability metadata while its arguments and result shape remain
untyped; and a workflow can preserve state while its runner contract is
inconsistent across implementations.

## 4. Goals

1. Make the production type check reproducible from a clean checkout with the
   documented `uv` workflow.
2. Establish one documented type policy for `Any`, `object`, bare containers,
   dynamic access, casts, ignores, JSON, plugins, and optional dependencies.
3. Replace avoidable `Any`, bare `list`/`dict`, and broad `object` annotations
   with concrete types, `TypedDict`s, dataclasses, enums, `Protocol`s, or
   validated JSON aliases.
4. Type the highest-value boundaries: event deserialization/reduction,
   configuration ingress, tool invocation/results, workflow contexts and
   resume, session/TUI protocols, and external integrations.
5. Isolate legitimate dynamic behaviour behind small adapters with explicit
   protocols, runtime validation, and focused tests.
6. Ratchet from the measured 611-error baseline to zero source errors under a
   documented strictness profile, then extend coverage to tests and plugins.
7. Keep the existing runtime ownership boundaries, serialized formats,
   security defaults, lauren-ai dispatch semantics, and Python 3.11 support.

## 5. Non-goals

- Replacing Python, lauren-ai, mypy, or the existing kernel/TUI/workflow
  architecture.
- Eliminating all uses of `object` from genuinely open JSON, plugin, or
  third-party boundaries. Unknown input must be narrowed, not disguised as a
  false closed type.
- Requiring every third-party optional package to be imported on every platform.
- Making a type checker a substitute for runtime validation, authorization,
  path/network policy, or approval checks.
- Rewriting all tests to avoid `MagicMock` in the first phase.
- Adding an HTTP/API surface or moving ownership to historical paths such as
  `src/agenthicc/api/`.

## 6. Type policy

### 6.1 Allowed and discouraged forms

| Form | Policy |
|---|---|
| Concrete dataclass, enum, `TypedDict`, or `Protocol` | Preferred for closed domain and component contracts |
| `Mapping[str, object]` / `object` | Allowed only at an ingress or genuinely open boundary; narrow immediately with a named helper |
| Recursive JSON alias | Preferred for JSON-serializable open data and schemas |
| `Any` | No new uses. Existing uses require a boundary waiver naming the external contract, why a narrower type is impossible, and the runtime/test guard |
| `cast(...)` | Only after an explicit runtime check or a trusted typed adapter; never to silence a mypy error at a normal internal call site |
| `getattr`/`hasattr` | Confined to optional/plugin/provider adapters. Known internal objects use direct typed access; capability probing returns a typed result |
| Bare `list`/`dict` | Forbidden in production annotations; use concrete type parameters |
| `# type: ignore` | Must include a specific error code and explanation; broad ignores and unused ignores are removed |

The shared vocabulary should be introduced in a new canonical typing module
only after checking import ownership. It should provide a Python-3.11-compatible
recursive JSON value/object alias and common aliases for tool arguments,
structured tool results, event payload mappings, and callback/unsubscribe
functions. The module must not become a grab bag of domain models owned by the
kernel, workflows, or TUI.

### 6.2 Runtime narrowing rules

- Decode JSON/TOML/plugin data with named validators or constructors that return
  typed records or structured errors.
- Prefer `isinstance`, `match`, `TypeGuard`, and typed helper functions over
  repeated inline `getattr` and unchecked indexing.
- Use `Protocol` for structural integrations owned by agenthicc; use a concrete
  adapter for third-party objects whose shape is not controlled by the project.
- Keep `TYPE_CHECKING` imports and quoted annotations where cycles or runtime
  decorator inspection require them. Add tests that call `typing.get_type_hints`
  or inspect generated tool schemas where annotations are runtime inputs.
- Treat external plugin data and persisted event data as untrusted even when a
  type annotation describes the expected shape.

## 7. Proposed architecture changes

### 7.1 Typed JSON and tool contracts (`P0`/`P1`)

Define a recursive JSON type compatible with Python 3.11 and use it for
serializable payloads. Replace repeated bare tool mappings with a stable
contract such as:

- typed common `ToolArgs`/`ToolResult` or `JsonObject` aliases;
- per-tool `TypedDict` argument records where keys are stable;
- a structured result envelope for success, error, warnings, and metadata;
- typed capability/approval context protocols;
- compatibility adapters for legacy `Tool` subclasses and lauren-ai callable
  tools.

The result contract must remain JSON serializable and must not expose secrets.
Schema dictionaries may remain open JSON objects, but their values must use the
JSON alias and schema validation must happen at registration/invocation where
the contract requires it.

### 7.2 Typed event payloads and reducer ingress (`P0`/`P1`)

Keep the existing event names and JSONL representation, but introduce one of
the following equivalent typed designs after a focused prototype:

- event-specific `TypedDict` payloads with a discriminated event mapping;
- event-specific constructors that validate into dataclasses before reduction;
- a small `EventPayload` protocol plus per-event narrowing helpers.

`Event.from_dict()` must validate required keys, scalar types, enum values, and
nested JSON shapes. Reducers should receive a narrowed event type or a helper
that cannot return an unvalidated `object`. Invalid persisted records must have
an explicit policy (reject, quarantine, or report) and tests for replay and
forward compatibility.

### 7.3 Typed configuration ingress (`P1`)

Keep TOML/environment/CLI data as `Mapping[str, object]` only at the parser
boundary. Add typed section mappings and coercion helpers, then construct the
existing settings dataclasses. Remove internal `getattr` fallbacks for owned
settings fields. Unknown keys, invalid unions, ranges, and provider/model
combinations remain runtime validation concerns and must retain their current
security defaults.

### 7.4 Workflow and agent-turn contracts (`P1`)

Replace untyped turn kwargs and `object` resume contexts with a shared typed
record/protocol owned by the runner boundary. Align base and concrete runner
method signatures so overrides are substitutable. Type plugin parameter input
as a read-only mapping and require each workflow to validate its own closed
parameter model before use. Preserve phase outputs, approval state, retries,
history, and resume semantics.

### 7.5 TUI, session, and provider protocols (`P1`/`P2`)

Type the existing `SessionContext`, `CommandContext`, `TriggerManager`, overlay
host, conversation store, processor, transport, approval service, and MCP
interfaces at their current ownership boundaries. Optional capabilities should
be represented by protocols or explicit nullable fields rather than repeated
attribute probing. The kernel `AppState` and reactive TUI state remain distinct
types; a stronger type system must make the bridge clearer, not merge them.

### 7.6 Optional integrations and plugins (`P2`)

Create explicit adapter boundaries for COM/Outlook, S3/boto3, Pyodide, YAML,
and lauren-ai provider objects. Choose one policy per integration:

- add a maintained stub package to the relevant optional dev extra;
- provide a small local `Protocol` and isolate the import in the adapter;
- or mark the module as an intentional optional boundary with a narrow,
  documented mypy override and runtime tests.

Do not use a repository-wide `ignore_missing_imports` setting, because it would
hide contract failures in core code.

## 8. Delivery plan

### Phase 0 — Reproducible checker and baseline (`P0`)

1. Add mypy to the declared development dependency group and lock it.
2. Add a checked-in `[tool.mypy]` configuration for Python 3.11, `src` layout,
   explicit error codes, unused-ignore detection, no implicit optional values,
   generic-parameter enforcement, and checked untyped bodies.
3. Decide and document optional-package stubs/overrides individually.
4. Make `nox -s typecheck` use the declared environment rather than an
   undeclared external executable.
5. Add a machine-readable or stable text baseline report with the error count,
   code distribution, and excluded modules. The report is temporary migration
   evidence, not a permanent waiver for unresolved errors.
6. Add a no-regression check for new source errors while the baseline is being
   reduced. Coordinate the final blocking-gate change with PRD-138 P0.5.

**Acceptance:** a clean checkout can run the exact documented type command after
`uv sync --extra dev`; the command has a stable configuration and the initial
baseline is reproducible. No CI job silently skips type checking because the
tool is absent.

### Phase 1 — Core vocabulary and high-value boundaries (`P0`)

1. Introduce the JSON and structured result vocabulary.
2. Parameterize all touched bare containers and eliminate avoidable explicit
   `Any` from kernel, configuration, workflow, TUI, and tool contracts.
3. Type event decoding and reducer payload access, beginning with the 38
   reducer errors.
4. Type `Tool`/filesystem/Git/exec argument and result boundaries without
   changing the external JSON tool protocol.
5. Add runtime validation and regression tests for malformed payloads, unknown
   fields, serialization round trips, and tool result shape.

**Acceptance:** no new `Any`, bare `list`, or bare `dict` annotations in
production; core modules have zero baseline errors; all type ignores in changed
files have codes and explanations; existing reducer/tool/security tests pass.

### Phase 2 — Workflow, configuration, and session contracts (`P1`)

1. Type configuration parsing and effective settings construction.
2. Align workflow base/concrete signatures and replace untyped turn kwargs.
3. Type session/TUI protocols and remove internal `getattr` from known-owned
   objects.
4. Reduce the `arg-type`, `attr-defined`, `override`, and `union-attr` clusters
   in the top baseline modules before enabling stricter per-module options.
5. Add workflow transition, rejection, retry, parallel, and resume typing
   tests in the same ownership boundaries as the runtime tests.

**Acceptance:** `config.py`, `kernel/`, `workflows/`, `runners/`, and the
canonical tool contracts pass the configured checker; dynamic access in those
areas is limited to documented adapters; workflow behavior and resume tests
remain green.

### Phase 3 — Optional integrations, plugins, and test fixtures (`P1`/`P2`)

1. Add typed adapters/stubs for optional platform and provider modules.
2. Type plugin discovery results, dependency diagnostics, command/workflow
   plugin protocols, and trust boundaries.
3. Replace `Any` in recording approval/transport wrappers with generic or
   protocol-based decorators where the external lauren-ai contract permits;
   document the residual adapter waivers where it does not.
4. Type shared test fixtures and run mypy over `tests/` after production is
   clean. Keep test doubles explicit rather than making `MagicMock` appear to
   satisfy every protocol implicitly.

**Acceptance:** optional modules have explicit platform policies, plugin data
  is narrowed at load time, and source plus the selected test tree pass without
  blanket missing-import or `Any` suppression.

### Phase 4 — Strict ratchet and blocking gate (`P2`)

1. Enable `disallow_untyped_defs`, `disallow_incomplete_defs`,
   `disallow_untyped_decorators`, `warn_return_any`, and redundant-cast checks
   for source, then expand to tests and supported plugin examples.
2. Remove the temporary baseline and no-regression comparison once source and
   test targets are clean.
3. Make type checking blocking on protected branches, subject only to the
   explicit optional-platform policy agreed in Phase 0.
4. Add a small audit command that reports counts for explicit `Any`, bare
   containers, unscoped ignores, and dynamic access in core paths so future
   drift is visible even when mypy does not report an error.

**Acceptance:** the clean-checkout release gate runs type checking with no
  undeclared tools, zero source/test errors under the agreed profile, no
  unexplained type ignores, and a documented waiver list containing only
  genuine external/dynamic boundaries.

## 9. Verification matrix

| Requirement | Verification |
|---|---|
| Reproducible checker | `uv sync --extra dev && uv run mypy src/agenthicc` from a clean checkout |
| Static baseline/ratchet | Checked-in baseline/audit command and CI comparison |
| Kernel safety | Reducer unit tests, event serialization/replay tests, malformed-payload tests |
| Tool contract | Filesystem, Git, exec, Outlook, MCP, plugin registration, capability, approval, timeout, and result-shape tests |
| Workflow contracts | Normal, reject, retry, parallel, approval, and resume tests; type-check workflow modules |
| TUI/session contracts | Interactive and non-interactive startup, picker/dispatch, terminal fallback, and session lifecycle tests |
| Optional integrations | Per-platform import/type policy plus focused tests or explicit unavailable-platform tests |
| Runtime annotation compatibility | Tool decorator/schema inspection and `typing.get_type_hints` checks where annotations are inspected at runtime |
| Regression safety | `uv run ruff check src/ tests/`, `uv run ruff format --check src/ tests/`, `uv run pytest tests/ -q`, docs and public-symbol checks |

## 10. Security, compatibility, and migration

- Type annotations do not replace `WorkspaceView`, `NetworkGuard`, approval,
  capability gates, trust prompts, or runtime validation. The migration must
  never weaken those defaults to satisfy the checker.
- Persisted event logs and exported sessions are compatibility surfaces. Typed
  decoding must preserve valid historical records and classify invalid records
  without silently dropping them.
- Project/user plugins remain executable and untrusted. Typed plugin protocols
  describe expected interfaces; they do not grant capabilities or suppress
  review.
- Python 3.11 remains supported. Type-alias syntax and standard-library typing
  features must work on the declared minimum version.
- Third-party stubs may lag runtime packages. Pin or constrain compatible
  versions in optional extras and keep adapter code small enough to review.
- `@tool` runtime annotation inspection is a compatibility hazard. Every
  decorator-facing signature change requires registration/schema tests.
- The migration should be incremental and reviewable by ownership boundary.
  Avoid a broad mechanical `Any` replacement that changes serialization,
  plugin loading, model-provider behavior, or UI rendering.

## 11. Success metrics

Track these metrics at each phase against the 2026-07-23 baseline:

- mypy error count and count by error code/module;
- number of source annotations using `Any`, bare `list`, or bare `dict`;
- `getattr`/`hasattr` count in core modules versus isolated adapters;
- number of broad or code-less `# type: ignore` comments;
- number of typed event payloads, tool contracts, workflow contexts, and
  integration protocols;
- time for a clean-checkout type check and full test suite;
- unchanged behavior for reducer replay, workflow resume, tool approval, and
  plugin discovery.

The first milestone is not “zero `getattr` everywhere.” It is a smaller,
reviewable surface in which dynamic access is intentional and typed at the
boundary, while core code receives concrete values. The final milestone is a
blocking, reproducible checker with no unresolved source/test errors under the
agreed profile.

## 12. Open decisions

| ID | Decision | Owner | Needed by |
|---|---|---|---|
| TS-01 | Mypy only, or mypy plus a second checker such as Pyright? | Maintainers | Phase 0; mypy is the initial default |
| TS-02 | Where should the shared JSON/tool aliases live without becoming a domain-model dumping ground? | Runtime maintainers | Phase 1 |
| TS-03 | Should event payloads use per-event `TypedDict`s, validated dataclasses, or a hybrid? | Kernel maintainers | Phase 1 |
| TS-04 | Which optional integrations receive maintained stubs versus local protocols? | Integration maintainers | Phase 0/3 |
| TS-05 | What is the accepted temporary error-baseline format and no-new-error policy? | Release maintainers | Phase 0 |
| TS-06 | How much of `tests/` is required to pass strict checking before the type gate becomes blocking? | Test/release maintainers | Phase 3/4 |

## 13. Definition of done

This PRD is complete when:

1. A fresh checkout installs and runs the configured type checker using the
   documented dev command.
2. The baseline has been retired: source and selected tests pass with the
   agreed strictness profile, and CI blocks regressions.
3. Event, configuration, tool, workflow, session, and plugin boundaries use
   named typed contracts with runtime validation where data is untrusted.
4. Explicit `Any`, bare containers, `getattr`/`hasattr`, casts, and ignores are
   either removed from core code or listed as reviewed adapter exceptions.
5. Existing runtime ownership, security defaults, persistence formats,
   lauren-ai tool behavior, and Python 3.11 compatibility remain covered by
   tests and documentation.

## 14. References

- [PRD-138 — Repository Improvement Roadmap](prd-138-repository-improvement-roadmap.md)
- [PRD-139 — OpenCode-Inspired Product Expansion](prd-139-opencode-inspired-features-and-privacy-first-ads.md)
- [Contributor guide](../docs/contributing.md)
- [Workflow review](../docs/reference/workflow-review.md)
- [Architecture guide](../docs/guides/architecture.md)
- `AGENTS.md` and `CLAUDE.md` typing rules and runtime ownership map
