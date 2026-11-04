---
title: "TOOLR-10: Search and read tools the model can use precisely"
status: Implemented with amendments (2026-11-04; tools wave 7, pending merge)
version: 1.0.0
date: 2026-11-04
scope: "the options and output of read_file, read_lines, grep_files and search_files: line numbers, binary detection, case and glob filters, context, ordering and a fast backend"
related_prds: [TOOLR-00, TOOLR-01, TOOLR-04, TOOLR-08, TUIR-01, TUIR-02, PRD-133]
tags: [tools, effectiveness, filesystem, search]
---

# TOOLR-10: Search and read tools the model can use precisely

## Implementation status (2026-11-04)

Delivered in tools wave 7 (branch `tools-wave-7-surface-and-search`).

- **Numbered reads (goal 1).**
  - `read_file` returns `<n>\t<line>`, with the numbers right-aligned to one
    width, by default; `line_numbers=false` returns the raw text.
  - `offset` and `limit` select a range. `read_lines` is deleted.
  - Results carry `total_lines`, `lines_returned`, `offset` and `version`.
  - A long read stops at a whole line, with `truncated` and `next_offset`.
- **Binary detection (goal 2).** Without `encoding`, a file with a NUL byte
  in its first 8 KB, or one that is not UTF-8, returns `binary: true`,
  `size_bytes` and `mime_type` and no content. An explicit `encoding` decodes
  with replacement and bypasses the cache, so a lossy decode is never later
  served as text.
- **A capable `grep_files` (goal 3).**
  - Options: `glob`, `exclude`, smart-case `case_sensitive`, `fixed_string`,
    `context_lines` (0–5), `max_results` and `output`
    (`matches` / `files` / `count`).
  - Every result carries `total_matches`, `files_matched`, `files_searched`
    and `backend`.
  - `truncated` names both causes: `max_results`, and the discovery cap.
  - An invalid regex is `invalid_argument`.
- **A fast backend, identical semantics (goal 4).** In
  `agenthicc.tools.fs.search`:
  - `search_with_ripgrep` runs `rg --json --crlf` over the files
    `discover()` returned, in batches of 2,000; `search_with_python` runs
    `re` in a thread.
  - Both split lines on `\n`, drop a trailing `\r`, replace undecodable
    bytes and skip files containing a NUL.
  - `rg`'s byte offsets are converted to the same character offsets.
  - A pattern `rg` cannot compile falls back to Python.
- **Recency (goal 5).** `search_files(sort="mtime")` lists the most recently
  modified files first.

Amendments:

- **`rg` searches the discovered file set.** `rg` is not used to walk the
  tree. The file set, with its ignore rules, caps and TUIR-01 authorization,
  is identical by construction, and only the regex work moves to `rg`.
  Symlinks are decided once, by `discover()`.
- **The 20,000-file cap stays.** On TUIR-00's 33,000-file project the search
  covers the first 20,000 non-ignored files in 0.53 s, and says so in its
  `note`.
- **Where the tests live.** The conformance suite is in
  `tests/unit/test_read_and_grep.py`: eight queries over files with CRLF,
  multi-byte, Latin-1 and binary content, and overlapping context, all
  required equal across the backends. The `rg` timing gate is
  `tests/performance/test_grep_backend.py`, with a 1 s budget.
- **Bugs the suite found.** It found two differences, both fixed: `rg`
  needs `--crlf` for `$` to match before `\r\n`, and its offsets for
  non-UTF-8 lines index the raw bytes.

## 1. Problem

TOOLR-00 F10 and F11. The read and search tools are correct, but blunt. The
model compensates by making more calls and burning more context.

- **No line numbers.** `read_file` returns one string, and `read_lines` returns
  a list without numbers. The model has to count lines to cite a location,
  build an `apply_diff` hunk, or choose a `read_lines` range, and it miscounts.
- **Binary as noise.** Both tools decode with `errors="replace"`. An image, a
  SQLite file or a wheel comes back as replacement characters, up to 100,000
  of them.
- **`grep_files` options.** It offers only `pattern`, `path` and
  `max_results`. Every search is case-sensitive, there is no file-glob or
  type filter, there are no context lines, and results are in path order with
  no count of how many files matched. A large match set is cut at
  `max_results` with no indication of how many matches exist.
- **Slow backend.** A pure-Python regex over every file. TUIR-00 measured
  5.9 s on 9,000 files, where `rg` on the same tree took 0.2–0.5 s.

## 2. Goals

1. **Numbered reads.**
   - `read_file` and `read_lines` take `line_numbers: bool = True`. When true,
     content is rendered as `<n>\t<line>` — the format the model already uses
     with other agents — with the number width fixed per call.
   - `read_file` also takes `offset` and `limit` lines, so `read_lines`
     becomes its alias and is removed under TOOLR-08's deletion rule.
   - Results carry `total_lines` and the TOOLR-01 `version`.
2. **Binary detection.** A file whose first 8 KB contains a NUL byte, or fails
   strict decoding, returns `ok: true, binary: true`, its size, its MIME guess,
   and no content. An explicit `encoding` argument overrides the check.
3. **A capable `grep_files`.**
   - Parameters: `pattern`, `path`, `glob` (include patterns), `exclude`,
     `case_sensitive` (default false when the pattern is all lowercase),
     `fixed_string`, `context_lines` (≤ 5), `max_results`,
     `output: "matches" | "files" | "count"`.
   - Results carry `total_matches` and `files_matched`, and `truncated` when
     `max_results` cut them.
   - An invalid regex returns `invalid_argument` (TOOLR-04).
   - It honours `.gitignore` and the TUIR-02 default ignore set.
4. **A fast backend, identical semantics.**
   - When `rg` is on `PATH`, `grep_files` and `search_files` run it, with `-0`
     NUL separators, `--json`, and bounded capture under TOOLR-02. They map
     its output to the same result shape.
   - Without `rg`, the TUIR-02 Python walker runs.
   - Both backends pass one shared conformance suite.
   - Authorization of reported paths follows TUIR-01: in-scope by
     construction, symlinks resolved and decided.
5. **Recency where it helps.** `search_files` takes `sort: "path" | "mtime"`.
   With `mtime`, the most recently changed files come first, which is the
   common "what did I just touch" query.

## 3. Non-goals

- Semantic or embedding search. `semantic_search` and the repo map
  (PRD-131/132) own that.
- Changing output caps (PRD-133), except that cuts are reported.

## 4. Acceptance criteria

1. `read_file` on a 3-line file returns `"1\ta\n2\tb\n3\tc"` by default, and
   the raw text with `line_numbers=false`.
2. `read_file` on a PNG returns `binary: true`, the size and no content.
3. `grep_files(pattern="todo")` matches `TODO`. With `glob=["*.py"]` it skips
   `.md` files. With `context_lines=2` each match carries two lines on each
   side.
4. With `rg` installed, `grep_files` over TUIR-00's 33,000-file project
   completes in under 1 s. It returns the same matches as the Python backend
   on the conformance fixture.
5. A search cut at `max_results=10` reports the real `total_matches`.
