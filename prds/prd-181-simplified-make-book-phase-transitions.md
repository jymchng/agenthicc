---
title: "PRD-181: Summary-only make_book phase transitions"
status: Implemented
version: 2.0.0
created: 2026-08-30
scope: "make_book phase-transition schemas, filesystem gates, asset production, and resumable handoffs"
related_prds:
  - PRD-100 # code_plan architecture
  - PRD-163 # cache-stable workflow prompts
  - PRD-179 # phase annotations and boundary checkpoints
  - PRD-180 # subagent artifact writing and research handoffs
tags:
  - make-book
  - workflows
  - phase-transitions
  - artifacts
  - checkpoints
  - assets
  - llm-ergonomics
---

# PRD-181 — Summary-only `make_book` phase transitions

## 1. Summary

`make_book` is a file-producing workflow. The model should spend its turn
creating the book, research, assets, and build outputs—not serialising facts
that the runner can derive from the filesystem. The phase handoff tools are
therefore deliberately small:

```python
submit_toc(summary: str)
submit_research(summary: str)
confirm_assets_ready(summary: str)
confirm_chapter_complete(summary: str)
confirm_front_matter_ready(summary: str)
confirm_back_matter_ready(summary: str)
mark_book_complete(summary: str)
reject_book(summary: str)
```

The only exception is `create_build_book()`. It is a compile-phase utility,
not a phase transition, and intentionally takes no arguments because the
runner already knows the output directory and book metadata.

The agent creates the artifacts with its normal filesystem and integration
tools. A transition gate only checks existing artifacts, derives their facts,
records a bounded summary/receipt, and emits the state-transition event. A
failed gate never creates, repairs, or mutates an artifact and never advances
the workflow.

## 2. Problem

The previous contract forced an agent to make a large nested call while it was
also doing the actual work. It asked the model to repeat runner-owned values
such as chapter index, output paths, word counts, complete asset inventories,
research bodies, and PDF paths. This caused:

- malformed nested arguments and avoidable retry turns;
- wrong chapter indexes after a retry;
- estimated word counts and stale file lists being treated as facts;
- research notes being truncated in provider messages;
- duplicated context and lower cache effectiveness;
- false handoffs when a model claimed a file existed but had not written it.

## 3. Goals

1. Make every phase-transition tool require exactly one concise `summary`.
2. Make the filesystem and typed runner context the sources of truth for
   artifact facts.
3. Keep transitions tool-controlled: prose alone can never advance a phase.
4. Keep large research and generated-content bodies on disk rather than in
   transition payloads or checkpoint metadata.
5. Make asset production substantial and publication-ready: at least three
   supported assets per chapter and at least six overall, including free
   Unsplash photography with provenance.
6. Preserve receipts, checkpoint state, resume behavior, and the shared
   workflow conversation without duplicating provider memory.
7. Keep stable workflow policy in the cacheable prompt prefix and put phase
   state, summaries, paths, and retry feedback in dynamic context.

## 4. Non-goals

- Removing phase gates or allowing a phase to advance on assistant prose.
- Making a missing artifact valid by inference.
- Passing complete chapter/research bodies through tool arguments.
- Adding a legacy adapter, accepting old nested keyword shapes, or preserving
  old provider schemas. This is a clean-slate contract; old cassettes and
  programmatic callers must be regenerated.
- Changing the publication order or PDF compilation strategy.
- Requiring custom workflows to use `make_book`'s tool names. They may adopt
  the same summary-and-files pattern through `create_workflow`.

## 5. Canonical ownership model

| Fact | Canonical owner |
|---|---|
| Current phase and next state | `MakeBookRunner` / `MakeBookState` |
| Current chapter | `MakeBookContext.current_chapter_index` |
| Book title and chapter plan | Agent-authored `toc.json`, validated by `submit_toc` |
| Research content | Files under `<output_dir>/research/` |
| Research chapter coverage | Deterministic filename scan (`ch01-*`, `chapter-1-*`, etc.) |
| Chapter path | Runner-derived `chapters/NN-<slug>.md` |
| Chapter word count and references | Read and derived from that Markdown file |
| Asset inventory | Deterministic scan under `<output_dir>/assets/` |
| Front/back matter inventory | Deterministic scan under their canonical directories |
| Compiled PDF | Exactly one existing `%PDF-` file under `dist/` |
| Live provider conversation | Session-scoped conversation/memory, not checkpoint payload |
| Resumable workflow facts | Typed context plus compact transition receipts |

No transition tool accepts a phase name, next state, chapter index, path,
word count, asset list, research body, or PDF path from the model.

## 6. Functional requirements

### FR-1 — Exact provider schemas

The eight transition tools must emit schemas equivalent to:

```json
{
  "type": "object",
  "properties": {"summary": {"type": "string"}},
  "required": ["summary"]
}
```

`summary` must be non-empty after trimming. The compile utility
`create_build_book()` must emit an object schema with no properties.

### FR-2 — TOC manifest handoff

The TOC prompt supplies a deterministic run-scoped `toc.json` path. The agent
must write that JSON file before calling `submit_toc(summary=...)`. The gate
must:

- reject a missing, unreadable, invalid, or non-object manifest;
- validate title, at least one valid chapter, technical level, and content
  types;
- apply runner defaults for author, level, content types, and output
  directory;
- derive chapter records and the assets/research directories; and
- record only a bounded summary and manifest metadata in the receipt.

The gate must never create the directory or manifest.

### FR-3 — Research handoff

The research agent must write durable files under `research/`, including one
chapter note file per planned chapter and the shared source/summary files. The
gate receives only `summary`, recursively inventories supported research
files, verifies every planned chapter has a matching note filename, and records
the relative file inventory. Research bodies are not copied into the context
or receipt. Chapter prompts explicitly tell the writer to read this file
inventory from disk.

### FR-4 — Chapter handoff

For chapter `i`, the gate derives
`chapters/{i+1:02d}-{slug(title)}.md`. It must verify that the existing file is
non-empty Markdown, calculate its word count, and inspect Markdown image
references. Each referenced asset must exist and resolve inside the configured
assets directory; relative references are resolved relative to the chapter
file and absolute references are still constrained to the book root. The gate
records the derived path, count, and asset references—not model-supplied
values.

### FR-5 — Many-asset production

The assets phase must instruct the agent to produce at least:

```text
max(6, 3 * number_of_chapters)
```

supported assets before transition. The set should be varied and useful, not
one repeated placeholder per chapter. The prompt requires Mermaid source and
rendered diagrams, reproducible matplotlib charts where data warrants them,
and print-suitable image dimensions/contrast.

The phase must include at least one raster image under
`assets/unsplash/`, obtained from the free Unsplash service. Unsplash+ and
paid assets are forbidden. The agent must write
`assets/unsplash/manifest.json` containing provenance entries and free
`unsplash.com` source URLs. The gate must:

- count only existing supported asset files;
- require the minimum count;
- require a raster under the `unsplash/` path;
- require a non-empty valid JSON provenance manifest;
- reject a manifest without an Unsplash source URL; and
- reject provenance containing Unsplash+ indicators (`unsplash+`,
  `unsplash.plus`, or `plus.unsplash.com`).

The gate does not download, generate, or modify any asset.

### FR-6 — Front/back matter handoffs

`confirm_front_matter_ready(summary=...)` scans existing Markdown files under
`front-matter/`; `confirm_back_matter_ready(summary=...)` does the same under
`back-matter/`. Neither accepts or creates a file list. Empty or missing
directories reject without an event.

### FR-7 — Compile verdict

`create_build_book()` may create the reusable builder at the output directory.
`mark_book_complete(summary=...)` may only verify existing state: the builder
must already exist, `dist/` must contain exactly one PDF, and its first five
bytes must be `%PDF-`. It must not create a builder, directory, PDF, or repair
an invalid output. `reject_book(summary=...)` records a bounded whole-book
diagnosis and routes back to correction; an empty summary is rejected.

### FR-8 — Receipts and checkpoints

After each accepted gate, the runner captures one compact receipt containing
the contract version, workflow/run identity, phase, bounded metadata, and
canonical relative paths. Receipts must not contain provider objects, tool
callables, chapter bodies, or research bodies. Checkpoint encoding must persist
the receipts and `toc_manifest_path`; restore must rehydrate both while
reattaching the existing session memory supplied by the caller.

Repeated successful calls must not append duplicate receipts.

### FR-9 — Retry and transition semantics

Each phase has a bounded inner retry loop. A failed tool result leaves state
unchanged and sends short repair feedback. Only a successful transition tool
sets the event. The outer runner loop then commits the next typed state. A
phase that exhausts retries fails through the existing workflow failure path.

## 7. Prompt and cache requirements

- The stable `CACHE_CONTRACT` is reused across all `make_book` phases and
  retries.
- Phase-specific metadata, current chapter data, artifact inventories,
  summaries, and retry feedback are dynamic text.
- Prompts must say that files are authoritative and the short transition call
  must be made only after writing them.
- Research and chapter prompts must not inline full research bodies. Chapter
  prompts list the durable research files and tell the agent to read them.
- Asset prompts must explicitly say “many assets,” the formula for the
  minimum, free Unsplash, no Unsplash+, and the manifest path.

## 8. Data flow

```text
user intent
  -> TOC agent writes run-scoped toc.json
  -> submit_toc(summary)
       -> validate/read toc.json
       -> MakeBookContext(title, chapters, roots)
  -> research agent writes research/*
  -> submit_research(summary)
       -> scan files + verify chapter coverage
  -> assets agent writes assets/* + unsplash/* + manifest.json
  -> confirm_assets_ready(summary)
       -> scan/count/provenance-check assets
  -> chapter agent writes chapters/NN-slug.md
  -> confirm_chapter_complete(summary)
       -> derive/read/count/check references
  -> front/back agents write canonical Markdown
  -> confirm_*_ready(summary)
       -> scan existing files
  -> compile agent calls create_build_book(), runs builder, writes dist/*.pdf
  -> mark_book_complete(summary)
       -> verify builder + exactly one valid PDF
  -> receipt after each accepted boundary
  -> checkpoint(context + receipts + manifest path, no provider memory)
  -> resume with the same session conversation and durable files
```

## 9. Acceptance criteria

### AC-1 — Schema ergonomics

All eight transition tools require exactly `summary: str`; no transition
schema exposes the old nested object or runner-owned path/index/count fields.
The builder utility is the only zero-argument tool.

### AC-2 — Verification-only gates

Missing artifacts, malformed manifests, missing chapter coverage, invalid
references, too few assets, absent/invalid Unsplash provenance, invalid PDFs,
and blank summaries reject without setting the transition event or mutating
handoff data. Gates never create artifacts.

### AC-3 — Asset quality floor

For `N` planned chapters, a successful assets gate inventories at least
`max(6, 3*N)` supported files and a free-Unsplash raster with a valid
provenance manifest. A manifest naming Unsplash+ is rejected.

### AC-4 — Derived facts

Chapter path, word count, referenced assets, asset count, research file list,
front/back file lists, and PDF path in accepted state are derived from files,
not trusted from model arguments.

### AC-5 — Durable resume

The checkpoint round trip preserves context, manifest path, and receipts while
excluding live session memory. A resumed runner starts from the saved typed
state and can continue using the existing artifacts.

### AC-6 — Full journey

The deterministic E2E test drives TOC → research → assets → chapter → front
matter → back matter → compile, verifies all receipts, and restores the
checkpoint successfully.

## 10. Test plan

- **Unit:** emitted schemas; blank summaries; malformed/missing TOC; research
  chapter coverage; canonical chapter path/count/reference validation; asset
  minimum and Unsplash provenance rules; front/back scans; builder/PDF checks;
  rejection behavior; path/symlink containment; checkpoint codec.
- **Integration:** execute every gate against a temporary output tree, assert
  no gate creates missing artifacts, assert deterministic inventories and
  receipt ordering, then serialize/restore the complete context.
- **E2E:** replace model turns with a deterministic filesystem-producing
  driver, run the complete state machine, verify dynamic chapter flow and all
  seven boundary receipts, then resume from the checkpoint.
- **Regression:** preserve tests for truncated research handoffs, wrong
  chapter paths/counts, missing Unsplash provenance, Unsplash+ provenance,
  stale PDFs, and duplicate receipt insertion.

## 11. Clean-slate migration and assumptions

- The simplified schema is authoritative immediately. There is no legacy
  normalizer or old-signature compatibility path.
- Existing provider cassettes using old arguments are invalid test fixtures
  and must be regenerated.
- The agent's normal filesystem tool is responsible for creating artifacts;
  this PRD does not grant a transition gate write access.
- “Many” is made deterministic as at least three assets per chapter and six
  overall. This is a floor, not a target; the prompt encourages additional
  varied assets when the content needs them.
- File-name based chapter coverage is intentionally lightweight and robust to
  notes containing different prose. The note contents remain authoritative to
  the chapter writer, while the gate verifies existence and coverage.
- The free-Unsplash rule is enforced through the required path, raster file,
  and provenance manifest. The manifest is an auditable source declaration;
  external image licensing verification remains outside this workflow.
