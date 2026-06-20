# PRD-109 — Case-Insensitive @Mention Matching

## Summary

The `@mention` system performs case-insensitive matching across all candidate
paths, files, directories, skills, and future mentionable resources.

Users do not need to remember filesystem capitalisation.  `@re` matches
`README.md`; `@DOC` matches `Documentation.md` and `docs/`.

Behaviour is consistent across Linux, macOS, and Windows regardless of
underlying filesystem case semantics.

---

## Motivation

Modern coding assistants allow users to reference files without memorising
exact capitalisation.  Requiring case-sensitive matching introduces friction
because repository naming conventions vary, mixed-case filenames are common,
and case sensitivity differs between operating systems.

---

## Functional Requirements

| FR | Requirement |
|---|---|
| FR-1 | Both query and candidate are normalised with `str.casefold()` before comparison. Original filename casing is preserved for display and insertion. |
| FR-2 | Matching evaluates the full path, the filename segment, and each intermediate path segment. |
| FR-3 | Filename prefix matches rank highest. |
| FR-4 | Path-segment prefix matching: `@read` matches `docs/README.md` because `README.md` is a path segment. |
| FR-5 | Substring matching: `@note` matches `release_notes.md`. |
| FR-6 | Fuzzy matching: `@rdm` matches `README.md` (sequential character containment). |
| FR-7 | Ranking order: exact → filename prefix → path-segment prefix → filename substring → path substring → fuzzy. Within a tier: alphabetical by casefolded display. |
| FR-8 | Display and insertion use the actual path casing, never the casefolded form. |
| FR-9 | Identical behaviour on Linux, macOS, and Windows. |
| FR-10 | Directories participate in matching exactly like files. |
| FR-11 | All mention providers use the same matching engine (`mentions/matcher.py`). |
| FR-12 | Future mention sources inherit case-insensitive matching automatically by calling `filter_and_rank()`. |

---

## Architecture

### New: `mentions/matcher.py`

Centralised, provider-agnostic matching engine:

```python
RANK_EXACT            = 0
RANK_FILENAME_PREFIX  = 1
RANK_SEGMENT_PREFIX   = 2
RANK_FILENAME_SUBSTR  = 3
RANK_PATH_SUBSTR      = 4
RANK_FUZZY            = 5

def rank_match(query: str, display: str) -> int | None: ...
def filter_and_rank(query: str, items: list[MatchItem]) -> list[MatchItem]: ...
```

### Changed: `tui/triggers/at_mention.py`

`get_matches()` now:
1. Collects all top-level entries **and** immediate children of all top-level
   directories into a flat candidate pool (enabling FR-4 path-segment matching
   without a recursive crawl).
2. Passes the pool to `filter_and_rank(fragment, candidates)`.
3. Preserves `display` and `value` with original filesystem casing.

The two previous `entry.name.startswith(fragment)` calls are replaced by the
centralised matcher.

---

## Performance

| Repository size | Measured latency |
|---|---|
| ≤ 10,000 candidates | < 10 ms |

Candidates are not cached between keystrokes (behaviour unchanged from before).
`casefold()` is O(n) and negligible versus the directory I/O already performed.

---

## Acceptance Criteria

| # | Input | Matches |
|---|---|---|
| 1 | `@re` | `README.md` |
| 2 | `@RE` | `README.md` |
| 3 | `@Read` | `README.md` |
| 4 | `@DOC` | `docs/`, `Documentation.md` |
| 5 | `@doc` | `docs/`, `docstrings.py` |
| 6 | `@rdm` | `README.md` (fuzzy) |
| 7 | `@read` | `docs/README.md` (path-segment) |
| 8 | Display always shows actual path casing | |
| 9 | Identical results on Linux, macOS, Windows | |
| 10 | `filter_and_rank()` is the single matching implementation | |

---

## Files Changed

| File | Change |
|---|---|
| `mentions/matcher.py` | New — `rank_match()`, `filter_and_rank()`, `_fuzzy_match()` |
| `tui/triggers/at_mention.py` | Use `filter_and_rank()`; expand all dirs before filtering |
