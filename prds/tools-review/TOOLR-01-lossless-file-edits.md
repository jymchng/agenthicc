---
title: "TOOLR-01: Lossless, exact and atomic file edits"
status: Implemented (2026-11-04; wave 1, pending merge)
version: 1.0.0
date: 2026-11-04
scope: "write_file, append_file, patch_file, apply_diff, touch_file, truncate_file and batch_write: preserve every byte the edit does not change, fail on ambiguity, write atomically, and refuse to overwrite a file that changed since it was read"
related_prds: [TOOLR-00, TOOLR-04, TOOLR-09, PRD-132]
tags: [tools, filesystem, correctness, data-integrity]
---

# TOOLR-01: Lossless, exact and atomic file edits


## Implementation status (2026-11-04)

Delivered in wave 1 (branch `tools-wave-1-gates-and-edits`).

- `agenthicc.tools.fs.edits` provides strict decoding with BOM and newline
  detection, `atomic_write` (keeps the mode, cleans up the temporary file),
  `FileVersions`, `diff_preview` and `changed_line_ranges`.
- `patch_file` accepts `expected_replacements`, `replace_all`,
  `expected_version`, `force` and `encoding`. `write_file` writes atomically
  and is stale-safe. `apply_diff` searches for drifted hunks within ±50 lines
  and handles insertion-only hunks, and its result no longer contains the file.
  `touch_file` uses `os.utime`. `truncate_file` is atomic. Backend
  `write_text`/`write_bytes` are atomic.
- Reads, appends and moves record versions; deletes forget them.
- **Amended (goal 5):** a version is `<size>:<mtime_ns>`, without a hash.
  It is cheap enough to compute on every read, including cache hits. Hash
  checking for coarse-mtime filesystems is TOOLR-09 goal 4.
- Tests: `tests/unit/test_fs_tools_edits.py` (criteria 1–8).

## 1. Problem

TOOLR-00 F1–F6. The edit tools are the ones an agent uses most, and three of
them can damage a file the model never meant to touch.

- **Lossy round trip (verified).** `patch_file` (`tools/fs/__init__.py:862`) and
  `apply_diff` (`tools/fs/agent_tools.py:385`) read with
  `read_text(encoding="utf-8", errors="replace")` and write back with
  `write_text`. Universal newlines turn CRLF into LF across the whole file,
  and every byte that is not valid UTF-8 becomes U+FFFD. Patching one
  character in `b'a\r\nb\xff\r\n'` wrote `b'A\nb\xef\xbf\xbd\n'`. Windows
  projects, Latin-1 sources and files with a stray byte are corrupted by any
  edit.
- **Ambiguous edits succeed.** `patch_file` replaces every occurrence of
  `old_content`. An anchor such as `return None` silently changes every
  function. The model is told only `replacements: N` after the fact.
- **`apply_diff`** trusts each hunk's line number exactly, with no search for
  the hunk nearby. That fails on any drift. It also returns the whole new file
  as `result`, which is an unbounded tool result.
- **`touch_file` (verified)** opens an existing file for append and closes it
  without writing, which does not update mtime.
- **Non-atomic writes.** `write_text` truncates the file, then writes it. A
  crash, a cancellation or a full disk leaves a partial file. Nothing
  preserves the file's mode.
- **Lost updates.** Nothing checks that the file is the one the model read. If
  the user, a formatter or another subagent changed it in between, the write
  silently discards their change.

## 2. Goals

1. **Byte-exact edits.** Edit tools work on bytes:
   - read the file as bytes;
   - detect its encoding (UTF-8 or UTF-8 with a BOM, else the encoding argument);
   - detect its newline convention (LF, CRLF, or mixed);
   - apply the change to the decoded text;
   - re-encode with the same encoding, BOM and newline convention;
   - leave every byte outside the edited span unchanged.

   A file that does not decode strictly is refused with
   `error_code="undecodable"`, naming the first bad offset, instead of being
   rewritten with replacement characters.
2. **Exact replacement by default.**
   - `patch_file(path, old_content, new_content, expected_replacements=1)`
     fails with `error_code="ambiguous_match"` when `old_content` occurs more
     or fewer times than expected. That error reports the count and the first
     three line numbers.
   - `expected_replacements=0`, or `replace_all=true`, keeps the old
     "replace every occurrence" behaviour as an explicit choice.
   - Newline differences between `old_content` and the file (the model sends
     LF, the file is CRLF) are normalized for matching only.
3. **`apply_diff` that tolerates drift.** Each hunk is applied at its stated
   line, and otherwise at the nearest exact match of its context within ±50
   lines. The result reports each hunk's applied line and offset. It returns a
   bounded summary — hunks applied and failed, and the changed line ranges —
   never the file.
4. **Atomic writes, mode preserved.** Every write goes through one helper,
   `atomic_write(path, data, *, mode_from=path)`. It writes a sibling temporary
   file in the same directory, flushes and fsyncs it, copies the original
   file's mode, and `os.replace`s it into place. `write_file`, `patch_file`,
   `apply_diff`, `truncate_file` and `batch_write` all use it. `append_file`
   opens with `O_APPEND`, which is atomic for a single write.
5. **Optimistic concurrency.**
   - `read_file`, `read_lines` and every edit result carry a `version`: the
     file's size, mtime_ns and a SHA-256 prefix.
   - Edit tools accept `expected_version`. On a mismatch they refuse with
     `error_code="stale_file"` and the current version.
   - When `expected_version` is omitted, a write still compares against the
     session's last read of that path, if there was one. A file that changed
     since then is refused unless `force=true`.
   - Creating a new file needs no version.
6. **`touch_file` touches.** It calls `os.utime(path)` for an existing file
   and creates the file only when `create=true`.
7. **Edits report what changed.** Each edit returns:
   - `bytes_written`;
   - the new `version`;
   - the changed line ranges;
   - a unified diff of the change, capped at 4,000 characters with
     `diff_truncated` when cut.

   This lets the model check its own edit without re-reading the file.

## 3. Non-goals

- A general multi-file transaction. `batch_write` stays per-file atomic and
  reports per-file outcomes.
- Merging concurrent edits. A stale write is refused, never merged.
- Changing which paths may be written; authorization is unchanged.

## 4. Acceptance criteria

1. Patching one character in each of these files changes only the bytes of
   that character, as a byte-for-byte comparison of the rest shows:
   - a CRLF file;
   - a mixed-newline file;
   - a UTF-8-with-BOM file;
   - a Latin-1 file opened with `encoding="latin-1"`.

   Tested in `tests/unit/test_fs_tools_edits.py`.
2. Patching a file with an invalid UTF-8 byte (opened as UTF-8) is refused
   with `undecodable` and the file is unchanged.
3. `patch_file` with an `old_content` that occurs twice fails with
   `ambiguous_match`, count 2 and both line numbers, and leaves the file
   unchanged; `replace_all=true` replaces both.
4. `apply_diff` applies a hunk whose header is 7 lines off, reports offset 7,
   and its result contains no copy of the file.
5. A write interrupted after the temporary file is written (simulated with
   `os.replace` raising) leaves the original file byte-identical, and no
   temporary file remains.
6. A file modified on disk after `read_file` is refused with `stale_file` by a
   write that omits `force`, and accepted with `force=true`.
7. `touch_file` on an existing file advances its mtime.
8. An executable file stays executable after `patch_file`.
