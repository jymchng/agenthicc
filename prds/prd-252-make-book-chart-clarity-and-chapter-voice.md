---
title: "PRD-252: make_book Chart Clarity, Chapter Voice, and Punctuation"
status: Implemented
version: 1.1.0
date: 2026-10-25
scope: "make_book asset, chapter, front-matter, and back-matter instructions"
related_prds: [prd-181, prd-163]
tags: [make-book, charts, matplotlib, prompts, prose, punctuation]
---

# PRD-252 — `make_book` Chart Clarity, Chapter Voice, and Punctuation

## 1. Summary

Update `make_book`'s asset and chapter-generation prompts so Matplotlib charts
never contain annotations or legends, generated chapters use a natural
first-person author voice that addresses the reader directly with “I” and
“you” and varied transitions, and chapter, front-matter, and back-matter
prose never uses em dashes. Keep the change within the existing workflow and
prompt contracts; do not change phase transitions, artifact validation, chart
generation tools, or compilation behavior.

## 2. Evidence and problem

The current asset-phase instructions conflict with the requested chart layout:
they ask for “rounded callout panels” and “key annotations”, “clean legends”,
and a contrast-check helper for panel labels. These elements can cover bar
values, category labels, ticks, or other chart text. Chapter-generation
instructions also allow Matplotlib charts without the no-annotation/no-legend
rule, even though the chapter phase may generate additional graphs.

The chapter prompt requests a natural human authorial voice and varied prose,
but does not explicitly require sustained first-person narration or direct
address. A chapter can therefore satisfy the existing style guidance while
remaining impersonal and avoiding “I” and “you”.

## 3. Goals

1. Make the asset phase's Matplotlib instructions unambiguous and free of
   contradictory annotation, callout, or legend requirements.
2. Apply the same chart rule whenever the chapter phase creates a Matplotlib
   graph, not only to graphs made in the asset phase.
3. Retain readable chart titles, axis labels, tick/category labels, scales,
   and gridlines; the restriction concerns in-plot annotations and legends,
   not the chart's essential axes and labels.
4. Direct chapter agents to use first-person “I” for the author's reasoning
   and “you”/“your” to guide the reader throughout the chapter, naturally and
   deliberately rather than mechanically.
5. Require frequent, varied, natural connective language in chapters so ideas
   and paragraphs form a cohesive, flowing narrative.
6. Ban the em dash character in generated chapter, front-matter, and back-matter
   Markdown while allowing hyphens and en dashes where appropriate.
7. Preserve technical accuracy, evidence-backed citations, existing phase
   tools, cache behavior, and checkpoint/resume semantics.

## 4. Non-goals

- No automatic image/chart parsing or visual-quality gate.
- No new Python dependencies, Matplotlib tooling, transition arguments, or
  artifact checks.
- No requirement to remove legends or annotations from Mermaid diagrams,
  photographs, or other non-Matplotlib assets.
- No forced first-person or direct-reader voice in research, front matter, back
  matter, or the builder; that voice requirement applies only to chapter
  generation. The em-dash ban applies to chapter, front-matter, and back-matter
  generation, not research, layout review, or the builder.
- No fabricated personal anecdotes, credentials, or experiences to satisfy
  the voice rule.

## 5. Required behavior

### 5.1 Matplotlib chart contract

Both the `assets` and `chapter` phase instructions must say that Matplotlib
charts:

- must not contain legends, annotations, callout boxes, or floating in-plot
  text/data labels;
- must retain an appropriate chart title, axis labels, readable tick labels,
  and category labels where relevant;
- should use a single series or small multiples when multiple series would
  otherwise require a legend;
- should put explanatory values, caveats, and series descriptions in the
  surrounding figure caption or chapter prose rather than overlaying them on
  the plot; and
- should use layout controls (for example `constrained_layout=True` or
  `tight_layout`) and verify that axes and category labels are not clipped or
  overlapping.

Remove or rewrite any Matplotlib guidance that positively requests legends,
annotations, callouts, or panel-label contrast checks. Do not weaken the
existing palette, resolution, DPI, reproducibility, image-bounds, or source
requirements.

### 5.2 Chapter voice contract

Both the static chapter `PhaseSpec` prompt and the runtime chapter prompt must
instruct the agent to:

- write as one human author using first-person singular (“I”) to explain
  reasoning, interpretation, and editorial choices;
- address the reader directly with “you” and “your” while explaining
  procedures, trade-offs, and implications;
- sustain both forms throughout the chapter, including more than the opening
  paragraph, while varying sentence structure and avoiding repetitive,
  forced pronoun insertion;
- use a rich variety of connective words and transitional phrases throughout
  every chapter to connect ideas and link paragraphs into a cohesive, flowing
  narrative. Examples include “although,” “however,” “moreover,” “furthermore,”
  “nevertheless,” “consequently,” “therefore,” “meanwhile,” “in contrast,”
  “for instance,” “in addition,” “despite this,” and “as a result.” Use them
  frequently and naturally, without repetitive or forced phrasing; and
- avoid inventing personal history, experiments, authority, or experience.

The voice must not weaken technical precision, research-grounded claims,
citations, or the existing human-prose requirements.

### 5.3 Em-dash punctuation contract

The static phase instructions and live prompts for `chapter`, `front_matter`,
and `back_matter` must prohibit the em dash character (U+2014) in all generated
Markdown, including headings. Direct agents to replace it with commas,
parentheses, colons, semicolons, or separate sentences. This rule is specific
to em dashes; do not prohibit ordinary hyphens or en dashes where they are
appropriate. This is a prompt-level authoring rule, not a new artifact gate.

## 6. Implementation boundaries

- Keep the edits in `src/agenthicc/workflows/make_book/runner.py` and its
  existing tests unless repository conventions require otherwise.
- Preserve the stable system-prompt/cache contract. These are phase-specific
  instructions and must remain consistent between static phase metadata and
  the runtime prompt used by `MakeBookRunner`.
- Do not modify the summary-only transition tools or the filesystem gates
  defined by PRD-181.

## 7. Acceptance criteria

1. The `assets` phase's static and runtime instructions explicitly prohibit
   Matplotlib legends, annotations, callouts, and floating in-plot labels.
2. The `chapter` phase's static and runtime instructions carry the same
   Matplotlib restriction for any graphs created during chapter writing.
3. No remaining Matplotlib prompt text requires legends, annotation panels,
   callouts, or annotation contrast-check code.
4. Both static and runtime chapter prompts require natural, sustained use of
   first-person “I” and direct-address “you”/“your”, plus a frequent, varied,
   natural use of connective words and transitional phrases throughout the
   chapter, without repetitive phrasing or fabricated personal experience.
5. Static and runtime prompts for chapter, front matter, and back matter all
   prohibit em dashes in generated Markdown and suggest acceptable alternatives;
   ordinary hyphens and en dashes remain allowed.
6. Existing chapter content, asset DPI, Unsplash policy, image/table bounds,
   tool schemas, and phase transitions remain unchanged.
7. Deterministic tests fail if these prompt rules are removed or contradictory
   chart instructions reappear.
8. Relevant make_book unit, integration, and E2E tests pass, along with lint,
   formatting, and type checks for touched files.

## 8. Verification

Run the focused make_book suites and source checks:

```bash
uv run pytest tests/unit/test_make_book_transition_contract.py tests/integration/test_make_book_transition_gates.py tests/e2e/test_make_book_transition_e2e.py -q
uv run ruff check src/agenthicc/workflows/make_book/runner.py tests/unit/test_make_book_transition_contract.py
uv run ruff format --check src/agenthicc/workflows/make_book/runner.py tests/unit/test_make_book_transition_contract.py
uv run mypy src/agenthicc
uv run mkdocs build --strict
```

Implementation verification completed on 2026-10-25: the combined make_book
suite passed (20 tests), Ruff lint/format passed for the changed source and
test files, mypy passed for all 336 source files, and the strict docs build
passed. MkDocs reported its existing, non-fatal navigation warning for
`guides/question-timeouts.md`. After the v1.1 em-dash prompt addition, the
focused make_book unit/integration/E2E suites passed again (20 tests), and
Ruff, mypy, and the strict docs build passed. The full repository suite also passed
(`4,972 passed, 16 skipped`); it was rerun with `RuntimeWarning` promoted to an
error after correcting an immediate-timeout test double that leaked the
`Queue.get()` coroutine it received. The warning-as-error run passed without
warnings.

## 9. Rollout and compatibility

This is a prompt-only behavior refinement. Existing checkpoints, saved chapter
artifacts, workflow phase names, tool schemas, and builder inputs remain
compatible. A resumed run receives the revised instructions on its next
chapter or asset turn; already-written content is not rewritten
automatically.

## 10. Security and data integrity

No new execution capability is introduced. The workflow continues to use its
existing approved filesystem, chart-generation, network, and asset tools.
First-person language is stylistic: it must not be used to fabricate source
evidence or claims of real personal experience.
