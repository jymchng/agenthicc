---
title: "TOOLR-03: Git tools that are non-interactive, bounded and exact"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "the eleven git_* tools: process handling, argument safety, parsing of git's output, and truthful result fields"
related_prds: [TOOLR-00, TOOLR-02, TOOLR-04]
tags: [tools, git, correctness]
---

# TOOLR-03: Git tool correctness

## 1. Problem

TOOLR-00 G1–G5. All git tools share `_run_git` (`tools/git/__init__.py:74`).

- **Wrong commit hash (verified).** `git_commit` parses `[branch hash]` with
  `\[[\w/]+ ([0-9a-f]+)\]`. Branch names containing `-` or `.` — the common
  case, such as `feature-x`, `release-1.2` or this repository's own
  `subagent-wave-5-…` — and root commits (`[main (root-commit) abc]`) yield
  `hash: ""` while `ok: true`.
- **Timeouts escape and leak.** `asyncio.wait_for(proc.communicate(), 30)`
  raises `TimeoutError` out of the tool and never kills git. A pre-commit hook
  slower than 30 s turns every `git_commit` into an exception, while the
  commit may still complete in the background.
- **Interactive git.** No `GIT_TERMINAL_PROMPT=0`, `GIT_EDITOR`, `GIT_PAGER`
  or `GIT_ASKPASS`, and stdin is inherited (TOOLR-02). A credential prompt, an
  editor (`git stash` with some configs, a merge) or a pager can block on the
  TUI's terminal.
- **Option injection.** Model-supplied refs and branch names go into argv
  unseparated. `git_checkout(branch="--orphan=x")`, `git_show(ref="--output=f")`
  and `git_diff(ref="--no-index …")` are parsed as options.
- **Untruthful fields.**
  - `git_diff` counts `"\n+"`/`"\n-"`, which includes the `+++`/`---` file
    headers.
  - Output is sliced to 64 K *characters*, with no `truncated` flag, for diff,
    show, blame and grep.
  - `git_log` declares a `format` parameter that nothing reads.
  - Locale-dependent messages are parsed (`git branch`, commit output).

## 2. Goals

1. **One hardened runner.** `_run_git` runs:

   ```
   git -c core.pager=cat -c color.ui=never -c core.quotepath=off -C <root> …
   ```

   with these settings:
   - `stdin=DEVNULL`;
   - `LC_ALL=C`, `GIT_TERMINAL_PROMPT=0`, `GIT_EDITOR=true`, `GIT_PAGER=cat`
     and `GIT_OPTIONAL_LOCKS=0` for read-only tools;
   - TOOLR-02's bounded head and tail capture.

   It returns a `CommandOutcome`-shaped result. On timeout it kills the
   process group and returns `error_code="timeout"`.
2. **Per-tool deadlines.** Read tools take 30 s. `git_commit` takes
   `git.commit_timeout_s` (default 300 s), so hooks can run. A commit that
   timed out reports whether `HEAD` moved.
3. **No option injection.** Refs and branches are validated with
   `git check-ref-format --allow-onelevel` (branches) or
   `git rev-parse --verify --end-of-options <ref>^{commit}` (refs) before use.
   Every path list is preceded by `--`. A value starting with `-` is refused
   with `error_code="invalid_argument"`.
4. **Exact results from plumbing.**
   - `git_commit` returns `git rev-parse HEAD` (full and short) after a
     successful commit.
   - `git_diff` counts from `--numstat` (per file: added, deleted, binary), and
     returns the patch separately, capped and marked `truncated`.
   - `git_status` uses `--porcelain=v2 --branch`.
   - `git_branch` uses `for-each-ref --format`.
   - `git_log` either implements `format` (`oneline`, `full`, or `patch` with a
     cap) or drops the parameter.
5. **Every result carries `ok`** and the TOOLR-04 error codes:
   `not_a_repository`, `invalid_argument`, `conflict`, `nothing_to_commit`,
   `hook_failed` and `timeout`.

## 3. Non-goals

- Network operations (`push`, `pull`, `fetch`) — not offered today, and adding
  them is a separate decision.
- Changing which paths git may touch.

## 4. Acceptance criteria

1. `git_commit` on branches `feature-x`, `release-1.2` and on a root commit
   returns the hash that `git rev-parse HEAD` prints.
2. A pre-commit hook sleeping 40 s commits successfully. With
   `commit_timeout_s=5` it returns `timeout`, leaves no git process running,
   and reports whether `HEAD` moved.
3. `git_checkout(branch="--orphan=x")` and `git_show(ref="--output=/tmp/x")`
   are refused with `invalid_argument`, and no file is written.
4. `git_diff` of a one-line change reports `additions=1, deletions=1`, even
   though the file headers contain `+++` and `---`.
5. A diff larger than the cap returns `truncated: true` and the full
   `--numstat` totals.
6. With a credential helper that would prompt, a read tool finishes without
   reading the terminal.
