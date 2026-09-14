# Reference index

This area holds precise, source-anchored descriptions of surfaces that other
pages only summarize. It is organized by subsystem, not by reader task — for
task-oriented material start at the [usage chapters](../usage/index.md), and for
teaching material at the [guides](../guides/quickstart.md).

## Specification pages

These describe what the code does now. Each one is expected to stay in step with
the implementation.

| Area | Canonical source | Reference |
|---|---|---|
| CLI | `src/agenthicc/cli/` | [CLI reference](cli.md) |
| Kernel | `src/agenthicc/kernel/` | [Kernel reference](kernel.md) |
| Storage and retention | `memory/`, `tui/runtime/`, `runners/`, `background/` | [Storage reference](storage.md) |
| `code_plan` workflow | `src/agenthicc/workflows/code_plan/` | [`code_plan` structure](code-plan.md) |
| API status | (package absent) | [API status](api.md) |
| Repository state | Current checkout | [Current repository state](repository-state.md) |

## Registers and baselines

These are **operational records**, not tutorials. They are dated snapshots and
are expected to be read with their date in mind, or re-generated.

| Document | Purpose |
|---|---|
| [Documentation fact base](fact-base.md) | Provenance register: maps every surface claim to a `path:line` citation, and lists verified-absent symbols |
| [Verification baseline](verification-baseline.md) | Recorded pass/fail state of the release gates, captured before the documentation campaign |
| [Usage ledger reference](usage-ledger.md) | Usage-record schema, fold rules, and cost/token projections |
| [Workflow package review](workflow-review.md) | Historical audit of `src/agenthicc/workflows/`, with a current status table per finding |
| `type-safety-baseline.json` | Machine-readable type-check baseline consumed by tooling (not a rendered page) |

!!! warning "These five pages are absent from the site navigation"
    `fact-base.md`, `verification-baseline.md`, `usage-ledger.md`, and
    `workflow-review.md` are not listed in the `mkdocs.yml` nav, so MkDocs
    reports them as unreachable pages. They are still buildable and linkable by
    URL. Reaching them by navigation is tracked as a separate navigation task.

## Cross-links into the guides

The reference area deliberately does not restate the guides. Where a subsystem
needs explanation rather than specification, the reference page links out:

| Area | Guide |
|---|---|
| Configuration discovery and precedence | [Configuration guide](../guides/configuration.md) |
| Project bootstrap | [Project bootstrap guide](../guides/project-bootstrap.md) |
| Workflow authoring | [Workflows guide](../guides/workflows.md); [custom workflows and TOML](../guides/custom-workflows-and-config.md) |
| TUI layout and input | [TUI guide](../guides/tui.md) |
| Tools and capability policy | [Security guide](../guides/security.md) |
| Memory tiers and retrieval | [Memory guide](../guides/memory.md) |
| Testing layers and cassettes | [Testing guide](../guides/testing.md) |

## Exported-symbol inventory

For the complete exported-symbol inventory, see
[`llms-full.txt`](https://github.com/agenthicc/agenthicc/blob/main/llms-full.txt).
It is checked by the repository's LLM documentation session, which requires the
set of `### Symbol` headings to remain a superset of `agenthicc.__all__` and
`agenthicc.kernel.__all__`.

There is no REST API reference because `src/agenthicc/api/` is absent; see
[API status](api.md). That product decision is tracked in PRD-138.
