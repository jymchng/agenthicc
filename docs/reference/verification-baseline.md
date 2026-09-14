# Documentation verification baseline

This page records the state of the two documentation gates **before** the
documentation overhaul, so that any regression introduced by later edits is
provable rather than arguable. Compare against it when the work is finished.

- Repository: `agenthicc`, branch `main`
- Commit: `332987a`
- Date of capture: 2026-09-14
- Working tree at capture: clean except the untracked
  [`reference/fact-base.md`](fact-base.md) added by the preceding step

---

## Summary

| Gate | Command | Exit code | Result |
|---|---|---|---|
| MkDocs strict build | `mkdocs build --strict` | **1** | **FAILS** — pre-existing |
| Public-symbol coverage | `nox -s llms_check` | **0** | passes |

The MkDocs gate was **already failing before this work started**. It is not a
regression, and it is not caused by the changes described on this page. It must
be fixed for the finished work to pass its own gate.

!!! warning "CI does not currently catch this"
    `.github/workflows/docs.yml:45` runs `uv run mkdocs build` **without**
    `--strict`. Warnings are therefore non-fatal in CI, and the broken link
    below has been shipping unnoticed. The strict build is the stronger gate,
    and it is the one this project holds itself to.

---

## Gate 1 — `mkdocs build --strict`

```console
$ mkdocs build --strict
EXIT=1
```

ANSI-stripped output, informational lines included:

```text
INFO    -  Cleaning site directory
INFO    -  Building documentation to directory: <repo>/site
INFO    -  The following pages exist in the docs directory, but are not included in the "nav" configuration:
  - guides/exploratory-tool-calls.md
  - guides/startup.md
  - guides/usage-accounting.md
  - reference/fact-base.md
  - reference/usage-ledger.md
WARNING -  Doc file 'guides/workflows.md' contains a link '../../prds/prd-178-reconstruct-site-ui-fidelity-research.md', but the target '../prds/prd-178-reconstruct-site-ui-fidelity-research.md' is not found among documentation files.
INFO    -  Doc file 'guides/workflows.md' contains a link '../reference/code-plan.md#cache-stable-workflow-turns', but the doc 'reference/code-plan.md' does not contain an anchor '#cache-stable-workflow-turns'.
Aborted with 1 warnings in strict mode!
```

Totals: **1 `WARNING`, 0 `ERROR`**. One warning is enough to abort a strict
build, so the count that matters is that the build reaches zero.

### The fatal warning: a link that escapes the docs tree

`docs/guides/workflows.md:1482`:

```markdown
[PRD-178](../../prds/prd-178-reconstruct-site-ui-fidelity-research.md). It
```

The target file does exist on disk (`prds/prd-178-reconstruct-site-ui-fidelity-research.md`),
but it lives **outside** the MkDocs documentation directory. MkDocs resolves
site links only between pages it publishes, so a relative path out of `docs/`
is an error in strict mode.

The repository already has a convention for this. Every other PRD reference in
`docs/` uses an absolute GitHub URL:

```markdown
[PRD-138](https://github.com/agenthicc/agenthicc/blob/main/prds/prd-138-repository-improvement-roadmap.md)
```

That absolute form appears as a rendered link **four** times in the
pre-change documentation: `docs/index.md:12`, `docs/index.md:56`,
`docs/guides/architecture.md:200`, and `docs/reference/workflow-review.md:5`.
The broken link at `docs/guides/workflows.md:1482` was the **only** relative
PRD path, so the fix is a one-line change to match the established convention.

!!! note "Quoted Markdown is inert"
    This page quotes the broken link verbatim inside a fenced `markdown` block
    so it can be compared against the fix. MkDocs does not resolve links inside
    fenced code, so the quotation adds no warning of its own — the build still
    reports exactly one.

### The non-fatal notices

These are `INFO`, not `WARNING`, so they do not fail the build today. They are
recorded because the finished work aims at a clean strict build and because
each is a real navigation gap.

**Orphan pages** — present in `docs/` but absent from the `mkdocs.yml` nav, so
no reader can reach them by clicking:

| Page | Title | Note |
|---|---|---|
| `docs/guides/exploratory-tool-calls.md` | Exploratory tool-call presentation | |
| `docs/guides/startup.md` | Startup and readiness | linked from `README.md`, so the README points at a page the site does not offer |
| `docs/guides/usage-accounting.md` | Usage accounting and `/usage` | |
| `docs/reference/usage-ledger.md` | Usage ledger reference | |
| `docs/reference/fact-base.md` | Documentation fact base | added by the preceding step |
| `docs/reference/verification-baseline.md` | Documentation verification baseline | this page; added by the current step |

Five pages were orphaned before this work began; the last two are new pages
added by it. All six must be reachable from the nav before the strict build is
clean, which is why the repair is tracked as its own goal.

**Stale anchor** — `docs/guides/workflows.md:1158` links to
`../reference/code-plan.md#cache-stable-workflow-turns`, but `code-plan.md` has
no heading with that anchor. Its headings are: State and context; Memory
lifecycle and memory tools; The runner loop; Phase functions and turn budgets;
Phase prompt ownership; Phase-local transition tools; Planning; Execution;
Review; Tool result and failure contract; Summary; Adding a phase safely;
Definition metadata versus runtime behavior.

---

## Gate 2 — public symbol coverage

Run through Nox exactly as CI does:

```console
$ nox -s llms_check
llms_check OK — all 22 public symbols are documented in llms-full.txt.
nox > Session llms_check was successful.
NOX_EXIT=0
```

The `noxfile.py:244` session collects every name in `agenthicc.__all__` **and**
`agenthicc.kernel.__all__`, then requires each to have a matching
`### Symbol` heading in `llms-full.txt`.

### The numbers to compare against

| Measurement | Baseline value |
|---|---|
| `agenthicc.__all__` | **`None`** — `src/agenthicc/__init__.py` is a 0-byte file |
| Symbols actually enforced | **22** (`agenthicc.kernel.__all__`) |
| `### Symbol` headings in `llms-full.txt` | **206** |
| Headroom | 206 headings vs 22 required |

Because the top-level `__all__` is `None`, the enforced set is exactly the 22
kernel symbols. The gate has very wide margin, which means it will not catch a
deleted unrelated heading — only a missing *kernel* heading. Treat the heading
count as the sensitive number and keep it at or above 206.

The 22 enforced symbols, for reference:

```text
AgentInstance  AgentStatus  AppState  Effect  EffectExecutor  EffectType
Event  EventProcessor  Intent  IntentStatus  NodeStatus  NoOpEffectExecutor
PermissionRule  ReducerFn  SecurityPolicy  SystemSettings  Task
ToolRegistration  Workflow  WorkflowNode  restore_from_log  root_reducer
```

---

## How to re-run both gates

```bash
# Gate 1 — must exit 0 with no WARNING lines for the finished work
mkdocs build --strict 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -E '^(INFO|WARNING|ERROR)|Aborted'

# Gate 2 — must exit 0
nox -s llms_check

# The two numbers, without invoking Nox
PYTHONPATH=src python -c "
import agenthicc, agenthicc.kernel
print('top-level __all__:', getattr(agenthicc, '__all__', None))
print('enforced symbols:', len(agenthicc.kernel.__all__))"
grep -c '^### ' llms-full.txt
```

Run these from the repository root, and read
[Stale trees](fact-base.md#stale-trees-scope-every-search-before-trusting-it)
first: `build/`, `dist/`, `site/`, and `.venv/` contain outdated copies of the
tree that searches will otherwise pick up.

## Related

- [Documentation fact base](fact-base.md) — where each documented claim comes from
- [Current repository state](repository-state.md) — architecture boundaries and known risks
