# PRD-253 — Preserve Timestamped `make_book` Build Outputs

**Status:** Implemented
**Date:** 2026-10-25
**Scope:** `make_book` compile instructions, generated `build_book.py`, output validation

## Problem

The compile instructions named fixed PDF/EPUB paths and the completion gate
required exactly one file of each type in `dist/`. This encouraged agents to
remove prior builds to satisfy validation and made repeated runs liable to
overwrite useful deliverables. Prior outputs must remain available, and older
files must not be mistaken for the current compile's result.

## Requirements

1. The compile phase instructs the agent to use timestamped output names and
   never delete, prune, or overwrite pre-existing files in `dist/`.
2. Intermediate cleanup is explicitly restricted to build intermediates,
   never historical deliverables in `dist/`.
3. The generated builder's default PDF and matching EPUB names use a
   microsecond-resolution timestamp and avoid a filename collision.
4. The completion gate snapshots existing PDF/EPUB files before the builder
   runs, validates exactly one new matching pair, and fails if pre-existing
   files were removed or changed.
5. Existing phase transition schemas remain unchanged; completion still takes
   only `summary`.

## Non-goals

- Automatically deleting failed or historical builds.
- Changing the explicit `--out`/`-o` interface.
- Limiting how many historical deliverables `dist/` may retain.

## Acceptance criteria

- A previous PDF/EPUB pair remains untouched while a new timestamped pair is
  accepted.
- Old outputs alone cannot satisfy the current compile gate.
- Removing or modifying a pre-existing output causes a recoverable gate error.
- The default builder path is timestamped and collision-safe.
- Unit, integration, and prompt-contract tests cover these behaviors.

## Verification

See the make_book tests in `tests/unit/test_make_book_builder.py`,
`tests/unit/test_make_book_transition_contract.py`,
`tests/integration/test_make_book_builder.py`, and
`tests/integration/test_make_book_transition_gates.py`.
