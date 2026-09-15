# Current repository state

This is the maintainer-facing state audit for the checkout. It records what is
implemented, what is only a compatibility boundary, and what remains roadmap
work. It is intentionally separate from historical PRDs: a PRD can describe a
proposed design without being a description of the running package.

!!! warning "Snapshot date"
    The audit below was re-derived from commit `332987a` (14 September 2026).
    The **counts** and **commit reference** are the fastest-moving parts of this
    page, so re-run the commands in
    [Re-deriving the evidence snapshot](#re-deriving-the-evidence-snapshot)
    before quoting them. The architectural statements are far more stable.

## Evidence snapshot

The audit used the current source tree, package metadata, and the test layout as
its evidence. The checkout contains:

| Area | Current evidence | Meaning |
|---|---:|---|
| Python source files | 261 | Broad runtime and integration surface under `src/agenthicc/` |
| Python test files | 315 | Unit, integration, and E2E coverage |
| Markdown docs | 51 | User, contributor, architecture, guide, and reference docs |
| PRD/research docs | 192 | Historical and proposed product/design records |
| Package version | `0.1.0` | Declared in `pyproject.toml:7` |
| Supported Python | `>=3.11` | `pyproject.toml:10` |
| Declared extras | `cloud`, `book`, `cloakbrowser`, `playwright`, `mcp`, `dev` | There is no `tui`, `api`, or `all` extra |

Passing local test counts are deliberately **not** recorded here: they change
with every commit, they depend on how the suite is selected, and a stale number
invites the reader to trust this page instead of running the suite. Run
`uv run nox` (or `pytest tests/ -q`) for the authoritative figure.

### Re-deriving the evidence snapshot

```bash
git rev-parse --short HEAD
find src -name '*.py'   | wc -l
find tests -name '*.py' | wc -l
find docs -name '*.md'  | wc -l
find prds -name '*.md'  | wc -l
PYTHONPATH=src python -c \
  "from agenthicc.tui.runtime.mode_manager import SELECTABLE_MODE_NAMES, MODE_ALIASES; \
   print(SELECTABLE_MODE_NAMES, MODE_ALIASES)"
python -c "import tomllib; print(list(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']))"
```

## Supported runtime surfaces

The supported product path is:

```text
agenthicc CLI
   ├─ interactive TUISession
   │    └─ Rich Live Workspace + reactive presentation state
   ├─ --headless stdin runner → JSON-lines
   └─ session commands → client-neutral SessionService
                         └─ optional loopback HTTP/SSE adapter
```

The event-sourced kernel, workflow runners, tool/capability path, memory
layers, and durable journals sit behind those entry points. The loopback
session transport is an adapter over the in-process service; it is not the
historical `agenthicc.api` server and it does not start automatically.

### Authoritative ownership boundaries

| Concern | Authoritative implementation | Durable or ephemeral |
|---|---|---|
| Domain state | `kernel/state.py` frozen `kernel.AppState` | Durable/replayable through events |
| Domain transitions | `kernel/events.py` and `kernel/reducer.py` | Pure reduction |
| Event processing | `kernel/processor.py` | Queue, persistence, effects, subscribers |
| Session construction | `runners/session_context.py` | Owns runtime resources |
| Interactive orchestration | `runners/tui_session.py` | Session lifecycle and UI bridge |
| Headless input | `runners/headless.py` | Stdin/kernel smoke and workflow entry points |
| Reactive presentation | `tui/conversation_store.py` | Ephemeral UI/input state |
| Rich rendering | `tui/workspace/` | Ephemeral terminal presentation |
| Workflow execution | `workflows/` | Phase state and handoff context |
| Tool policy and approvals | `tools/capabilities.py`, `tools/capability_gate.py`, `tools/approval.py` | Runtime authorization |
| Tool adapter | `tools/executor.py`, `tools/hooks.py` | Lauren-ai compatibility boundary |
| Memory and journals | `memory/`, `tui/runtime/`, `tools/fs/file_cache.py` | Tiered and session durability |
| Client-neutral projection | `session_service/` | Durable event projection plus bounded subscriptions |

There are two `AppState` types by design. Kernel state must change through
events and the pure reducer. Reactive TUI state owns terminal-only concerns.
When a feature crosses that boundary, the bridge belongs in the session/runner
layer and needs both event and presentation tests.

## Workflow reality

`src/agenthicc/workflows/` contains **nine** built-in packages:

| Package | Runner shape |
|---|---|
| `default` | Generic `WorkflowRunner` over a declarative `PhaseSpec` graph |
| `code_plan` | `CodePlanRunner` with a typed `CodePlanState` loop |
| `create_workflow` | Typed authoring state, direct source generation, deterministic validation, resume/retry |
| `copy_website` | Playwright study phase, then implementation, responsive, and parity validation |
| `reconstruct_site` | Deeper reference-site reconstruction: research, architecture, infrastructure, implementation, validation |
| `site_imitate` | Visual imitation path (complementary to `copy_website`; not an alias) |
| `goal_flow` | Dynamic goal list with `append_goal`/`insert_goal` mutation tools and stable-ID records |
| `make_book` | Long-form document authoring |
| `make_agenthicc_tool` | Scaffolds a project-local agenthicc tool |

Reliable detail per package:

- `code_plan` uses `CodePlanRunner` and a typed `CodePlanState` loop for
  `plan → execute → review → summarize`; see [`code_plan` structure](code-plan.md).
- `create_workflow` uses its own typed authoring state, phase artifacts, direct
  source generation, deterministic validation, and resume/retry rules.
- `copy_website` studies a target with Playwright before rebuilding it through
  explicit implementation, responsive, and parity-validation phases.
- `reconstruct_site` performs a deeper reference-site reconstruction with
  research, architecture, infrastructure, implementation, and validation
  phases.
- `goal_flow` maintains a canonical stable-ID `GoalRecord` list; the historical
  string/index arrays remain derived compatibility projections.
- Generic `WorkflowRunner` executes declarative `PhaseSpec` graphs and supports
  model overrides, command gates, human phases, parallel phases, and resume.

Enumerate the current set from source rather than from this page:

```bash
ls -d src/agenthicc/workflows/*/ | sed 's|.*/workflows/||; s|/$||'
```

See [the workflow comparison guide](../guides/workflows.md#website-reconstruction-workflows-choosing-the-right-one)
for the intended boundary between `site_imitate`, `copy_website`, and
`reconstruct_site`; they are complementary built-ins rather than aliases.

`PhaseSpec` is not the sole source of truth for the specialized `code_plan` and
`create_workflow` runners. Their class-level phase metadata is used for
registry/UI/configuration surfaces, while the specialized runner owns prompts,
phase loops, and transition tools. This remains an architectural improvement
item: either reconcile the two representations or document the split wherever
phase metadata is consumed.

All workflow transitions that matter to correctness are tool-controlled. The
runner checks an event and structured handoff data after the agent turn rather
than inferring a transition from prose. Approval, rejection, retry, command
failure, and resume behavior are distinct contracts and should not be collapsed
into a single `approved` truth value.

## Extension and trust model

The current extension surfaces are separate registries/loaders for workflows,
agents, tools, commands, skills, modes, plugins, and MCP servers. Project-local
Python extensions remain executable code. Discovery, trust, dependency
installation, shadowing, and headless behavior therefore remain security
boundaries; new documentation must not imply that a discovered plugin is safe
merely because it was found.

`tools/hooks.py` and `tools/executor.py` are real modules, but they are thin
adapters over lauren-ai's canonical `ToolHook`, decision objects, and executor.
They are not the removed standalone lifecycle engine described by older PRDs.
Use `docs/guides/hooks.md` for the supported boundary.

## Persistence and recovery

Persistence is split by owner rather than stored as one universal event log:

- kernel events and snapshots record domain state;
- conversation events and the journal support UI history and interrupted-turn
  recovery;
- project/global memory stores routed values and artifacts;
- the workspace file cache stores freshness-checked file content;
- cassettes record transport and approval interactions for deterministic replay;
- the session service stores client-neutral snapshots, durable event cursors,
  idempotency records, and bounded subscription state.

See [the storage reference](storage.md) before adding a file, retention policy,
or resume format. A new durable field needs corruption/restart coverage and a
documented owner.

## Documentation and release-gate findings

The maintained README and guides describe the Rich TUI, the headless stdin
interface, the session service, three selectable modes (`Safe`, `Plan`, `Yolo`
— plus the aliases `auto`→`Yolo`, `guard`/`ask`→`Safe`, `review`→`Plan`), the
current workflow authoring path, and the absent historical API explicitly.

These findings were re-checked by execution at commit `332987a`, and the
`mkdocs build --strict` and navigation findings below were re-checked again
after the documentation campaign's navigation fixes:

1. `mkdocs build --strict` **now exits 0 with zero warnings**. It previously
   aborted on one `WARNING`: `docs/guides/workflows.md` linked to
   `../../prds/prd-178-reconstruct-site-ui-fidelity-research.md`, a target
   outside the MkDocs docs directory. The link now uses the repository's
   absolute GitHub-URL convention for PRDs, matching `docs/index.md`,
   `docs/guides/architecture.md`, and `docs/reference/workflow-review.md`.
   Reproduce with `python -m mkdocs build --strict`.
2. **No page is absent from the `mkdocs.yml` nav**, and no nav target lacks a
   file. Nine pages were previously unreachable by navigation: `glossary.md`,
   `guides/architecture-diagram.md`, `guides/exploratory-tool-calls.md`,
   `guides/startup.md`, `guides/usage-accounting.md`, `reference/fact-base.md`,
   `reference/troubleshooting-index.md`, `reference/usage-ledger.md`, and
   `reference/verification-baseline.md`. All nine are now wired in, so MkDocs no
   longer prints its "not included in the `nav` configuration" notice. The first
   three were created by this campaign and were themselves orphans until this
   fix.
3. The one stale anchor is **repaired**: `guides/workflows.md` cited
   `../reference/code-plan.md#cache-stable-workflow-turns`, but the
   `Cache-stable workflow turns` heading lives in `guides/workflows.md` itself,
   not in `reference/code-plan.md`. The citation is now the in-page anchor
   `#cache-stable-workflow-turns`. MkDocs reported this at `INFO` level, so it
   never failed the build, but the anchor was genuinely broken.
4. `llms-full.txt` is checked for headings by an embedded Nox script
   (`nox -s llms_check`), but there is no source-to-reference generator and no
   complete stale-section verifier.
5. There is no default Nox docs build/link-check session, so a clean checkout
   cannot yet claim a reproducible docs release gate. The related claim that
   MkDocs is undeclared is **false**: `mkdocs>=1.6` and `mkdocs-material>=9.5`
   are declared in the `dev` extra (`pyproject.toml:37-39`). The missing piece is
   the session, not the dependency.
6. The package version and the CLI's `--version` string are maintained
   independently; release metadata can drift from `pyproject.toml`.
7. The workflow findings in [`workflow-review.md`](workflow-review.md) were
   mechanically re-checked: 21 of 33 remain open, 7 are resolved, and 2 need
   targeted revalidation. Treat that page's prose as the original record and its
   status table as current.
8. PRD-138 P0.2 still owns the decision to implement a supported server API or
   to remove compatibility-only API configuration and historical references.

## How to use this document

Use this page to choose the ownership boundary before changing code. Use the
user guides for supported behavior, `llms-full.txt` for AI-facing public
symbols, and PRDs for proposals or historical decisions. If those sources
disagree, verify the source tree and update this audit plus the affected
maintained documentation in the same change.
