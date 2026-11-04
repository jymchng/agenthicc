---
title: "TUIR-01: Discovered-path authorization — one cheap decision per kept result, off the event loop"
status: Proposed
version: 1.0.0
date: 2026-11-03
scope: "how search_files, grep_files, list_directory and every other discovery tool authorize the paths they find: once, cheaply, in a worker thread, for the results they keep — with the full policy kept for any path that resolves outside the scope"
related_prds:
  - TUIR-00
  - TUIR-02
  - TUIR-04
tags:
  - performance
  - security
  - filesystem
  - event-loop
---

# TUIR-01: Discovered-path authorization off the event loop

## 1. Problem

TUIR-00 R1: `_authorize_discovered_paths` (`src/agenthicc/tools/fs/__init__.py:159-191`)
awaits `WorkspaceAccessPolicy.authorize` once per discovered candidate. Each
call:

1. preflights the candidate through `authorize_tool` → `WorkspaceScope.resolve`;
2. resolves it again in `authorize`;
3. resolves it a third time in `revalidate`.

Each `resolve` (`src/agenthicc/tools/workspace_access.py:138-188`) runs
`os.path.realpath` (one `lstat` per component), tests containment with
`root in candidate.parents` (allocating every parent `Path`), computes a
`relative_to` display string and a SHA-256 root id, and `stat`s the result.
None of it suspends, so the whole batch runs on the event loop in one go. One
`search_files("*.py")` over 33,000 files holds the loop for **14.3 s**; six
workers produce a **93.7 s** freeze (TUIR-00 §3).

The per-candidate design is right — a symlink inside the workspace can point
outside it, so a discovered path needs its own canonical decision. Its cost is
not: every candidate pays for the general case, three times, on the loop.

## 2. Goals

1. **Known-in-scope candidates are decided without the policy.** Discovery
   starts from a root the tool has already authorized
   (`_resolve_path(context, path, ...)`), and that root is canonical. A
   descendant reached without traversing a symlink is in scope by
   construction: its canonical path is the root's canonical path plus the
   relative names. Such a candidate needs no `realpath`, no containment test,
   no approval and no revalidation. Only candidates reached through a symlink
   (file or directory) are canonicalised and classified.
2. **One canonicalisation per symlinked candidate.** A symlinked candidate is
   resolved once (`os.path.realpath`) and classified by component-wise prefix
   against `scope.allowed_roots` (`os.path.commonpath` or a precomputed
   `str(root) + os.sep` prefix — never `Path.parents`). If it is inside a root,
   it is authorized. If it is outside, it goes to the full
   `policy.authorize(...)` path unchanged, so Safe mode still asks, Plan mode
   still denies, Yolo still bypasses, and `revalidate` still catches a target
   that changed after approval.
3. **The work runs off the loop.** Walking and classifying run in one
   `asyncio.to_thread` call per tool invocation (TUIR-02 bounds how many run at
   once). Only outside-scope candidates — normally none — return to the loop
   for the async policy, which may prompt.
4. **Authorize what is kept, not what is found.** The decision is made after
   TUIR-02's filters (ignored paths, type, glob match, result cap), so a search
   capped at 1,000 results authorizes at most the candidates it examines
   until it has 1,000 — not every path under the root.
5. **One implementation.** `search_files`, `grep_files`, `list_directory`,
   `batch_*` discovery and any tool that enumerates paths call one helper,
   `discover_authorized(context, root, *, recursive, include, operation,
   tool_name, limit)`, that yields `(display_path, canonical_path)` pairs. The
   per-tool copies of "rglob, then authorize, then filter" are deleted.
6. **Decisions are observable.** The helper reports `examined`, `kept`,
   `symlinked`, `outside_scope` and `denied` counts in the tool result's
   diagnostics, so a denied symlink is visible rather than silently missing.

## 3. Non-goals

- Changing what the policy allows. Every decision that today reaches the
  policy for an outside-scope path still reaches it.
- Caching decisions across tool calls. A symlink can be retargeted between
  calls; each call decides afresh.
- Replacing `WorkspaceScope.resolve` for explicit, model-supplied paths — one
  call per tool invocation is not a cost problem.

## 4. Design

```text
tool.execute
  root  = await _resolve_path(context, path, operation, tool_name)   # full policy, once
  found = await asyncio.to_thread(_walk_and_classify, root, filters, limit)
          # os.scandir walk; DirEntry.is_symlink() is free (d_type)
          # not a symlink           -> (rel, root / rel)          in scope, no syscalls
          # symlink -> realpath once -> prefix test vs allowed_roots
          #     inside              -> (rel, real)
          #     outside             -> deferred
  for rel in found.deferred:                                          # normally empty
      canonical = await policy.authorize(rel, operation, tool_name)   # full policy
```

A directory reached through a symlink is classified before the walk descends
into it: inside the scope, its subtree is walked relative to the real
directory (and loops are broken by a visited set of `(st_dev, st_ino)`); outside,
the directory is deferred to the policy and its subtree is walked only if the
policy allows it.

`ResolvedWorkspacePath.display` and the root id are computed only for paths
that are reported to the user (approvals, diagnostics), not for every
candidate.

## 5. Acceptance criteria

1. Over TUIR-04's 33,000-file project, `search_files("*.py")` and
   `grep_files(...)` on the main loop stall it < 50 ms (today 14.3 s and 4.7 s).
2. The same calls return byte-identical `matches`, `count` and `truncated`
   values to today for a project with no symlinks.
3. A symlink inside the workspace to a file outside it is: omitted with an
   `outside_scope` count in Plan mode; offered for approval in Safe mode (one
   request per call, as today); returned in Yolo mode. Pinned per mode in
   `tests/integration/test_workspace_access_integration.py`.
4. A symlinked directory pointing back to an ancestor does not loop.
5. A symlink retargeted between authorization and read is refused
   (`target_changed`), as `revalidate` does today.
6. No call to `WorkspaceScope.resolve` is made for a non-symlink candidate
   (asserted by a counting spy in a unit test).
7. `_authorize_discovered_paths` and every per-tool "rglob then authorize"
   copy are deleted; a repository grep finds no remaining caller.
