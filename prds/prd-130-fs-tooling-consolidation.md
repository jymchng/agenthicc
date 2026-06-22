---
title: "PRD-130: Filesystem Tooling Architecture — Consolidate the Two Implementations"
status: proposal
version: 0.1.0
created: 2026-06-22
supersedes-decision-for: [prd-14, prd-51, prd-52, prd-53, prd-54]
---

# PRD-130 — Filesystem Tooling Consolidation

## Executive summary

agenthicc has **two parallel filesystem implementations** behind its agent fs
tools, and a **pluggable multi-backend system that production never plugs into**:

1. The original **14 tools** (PRD-14) read/write through `WorkspaceView` with
   direct `os`/`pathlib` calls.
2. The later **expanded tools** (PRD-52: `batch_*`, `apply_diff`, `checksum_file`,
   `truncate_file`, `touch_file`, `grep_file`) go through a `FilesystemBackend`
   abstraction (PRD-51) — but only ever reach `LinuxFilesystemBackend`.
3. The `BackendRouter` + `S3`/`Windows`/`Pyodide` backends (PRD-53/54) are
   **real, tested implementations that are completely unreachable at runtime**.

PRD-51 explicitly intended to migrate the 14 tools onto the backend (goal **G2**:
"`LinuxFilesystemBackend` is the drop-in replacement for current POSIX I/O";
"the tools … swap `WorkspaceView` calls for backend calls") and to route by path
(**G4**).  **That migration was never finished and the router was never wired.**
The result is duplicated I/O logic, a dead abstraction layer, and developer
confusion (this PRD was triggered by exactly that: *"I think the agent tools
don't use any of the backends?"*).

This PRD documents the findings and recommends **finishing the consolidation onto
a single path** — with a clear decision gate on whether multi-backend (cloud /
browser / Windows) support is actually wanted.

---

## 1. Findings

### 1.1 Two families of fs tools

`FS_AGENT_TOOLS` (`tools/fs/agent_tools.py:572`) registers both families:

| Family | Tools | Path |
|---|---|---|
| **A — WorkspaceView-direct** | `read_file`, `write_file`, `append_file`, `delete_file`, `file_exists`, `search_files`, `grep_files`, `get_file_info` | delegate to `*Tool` classes in `fs/__init__.py` → `WorkspaceView` + direct `os`/`pathlib` |
| **B — backend** | `grep_file`, `apply_diff`, `checksum_file`, `truncate_file`, `touch_file`, `batch_read/write/delete/move/copy` | `_get_backend()` → `LinuxFilesystemBackend` |

So the original observation is **half right**: Family A uses no backend; Family B
does — but only ever the Linux one.

### 1.2 The PRD-51 migration was never finished

PRD-51's goals G2 (migrate the 14 tools) and G4 (route by path) are **incomplete**.
PRD-52 added *new* tools on the backend instead of moving the existing ones, so
the two implementations now coexist with duplicated logic:

- `ReadFileTool.execute` (`fs/__init__.py:45`) and
  `LinuxFilesystemBackend.read_text` (`linux.py:57`) both implement reads.
- Same duplication for write / append / delete / list / exists / stat / grep.

### 1.3 The router is dead — and there are *two* of it

There are **two separate `configure_router` / `_router` globals**:

- `router.py` — `_router`, `configure_router`, `get_router`, plus
  `_detect_default_backend` (picks Pyodide on WASM, Windows on `nt`, else Linux).
- `agent_tools.py:48` — a **different** `_router` + a **different**
  `configure_router` (`:58`), which is what `_get_backend()` (`:51`) actually
  reads.

`agent_tools._get_backend()` checks `agent_tools._router` (never set) and falls
back to a **hardcoded `LinuxFilesystemBackend(os.getcwd())`** (`:54-55`) — it
**never calls `router.get_router()` or `_detect_default_backend()`**.  Net effect:

- `configure_router()` is **never called in `src/`** — only in tests.
- The OS auto-detection in `router.py` is **doubly unreachable**: the router
  isn't wired, and even if it were, `_get_backend()` hardcodes Linux.
- **Even on Windows or in a browser, the agent tools would use
  `LinuxFilesystemBackend`.**

### 1.4 Real, tested, unreachable cloud backends

`S3FilesystemBackend` (`s3.py`, 27 methods, real `boto3`), `PyodideFilesystemBackend`
(`pyodide.py`, 25 methods, real Emscripten FS), and `WindowsFilesystemBackend`
(`windows.py`, subclasses Linux) are **complete implementations** (PRD-53/54) with
**unit tests** (`test_s3_backend.py`, `test_windows_backend.py`,
`test_fs_backend_protocol.py`, `test_fs_backend_integration.py`).  In production
they are **100 % dead** — reachable only through the unwired router.

Additional gap: **`boto3` is not a declared dependency** anywhere (not in
`[project.optional-dependencies] cloud`, which only lists `aiohttp` + `keyring`),
so `S3FilesystemBackend` cannot even be instantiated in a real install.

### 1.5 Good news — no security divergence

Both paths enforce the **same** sandbox.  `LinuxFilesystemBackend` wraps
`WorkspaceView` internally (`linux.py:28`, `_resolve` → `view.resolve`), and the
WorkspaceView check (`sandbox.py:28-40`, `realpath` + root-prefix) blocks `..`,
absolute-path, and symlink escapes in **both** families.  So this is *not* a
security bug — it is duplication + dead code, not a sandbox hole.

### 1.6 Other loose ends

- `FsToolKit` (`fs/__init__.py:438`) accepts a `backend` and stores it in
  `self._backend` — which is **never read**; the class is **never instantiated**
  in production (PRD-51 G5 dead).
- Several Family-A tools are **defined but not registered**: `move_file`,
  `copy_file`, `list_directory`, `make_directory`, `read_lines`, `patch_file`
  exist in `agent_tools.py` but are absent from `FS_AGENT_TOOLS`.
- `LinuxFilesystemBackend` runs an import-time `assert isinstance(... )`
  (`linux.py:294`) that instantiates a backend on the cwd as a side effect.

### 1.7 Evidence index

| Claim | Location |
|---|---|
| Family A delegates to WorkspaceView `*Tool` classes | `tools/fs/agent_tools.py:64-261`, `tools/fs/__init__.py:45-57` |
| Family B uses `_get_backend()` | `tools/fs/agent_tools.py:264-573` |
| `_get_backend` hardcodes Linux; ignores router detection | `tools/fs/agent_tools.py:51-55` |
| Two separate `configure_router`/`_router` | `agent_tools.py:48,58` vs `router.py:21,123,134` |
| `configure_router` never called in `src/` | grep: only `tests/` |
| Backends enforce the same sandbox via WorkspaceView | `linux.py:28,46-48`, `sandbox.py:28-40` |
| Cloud backends real but unreachable | `s3.py`, `pyodide.py`, `windows.py` |
| `boto3` undeclared | `pyproject.toml` (`cloud` = aiohttp + keyring only) |
| `FsToolKit._backend` write-only; class unused | `fs/__init__.py:438-442` |
| Registration gaps | `agent_tools.py:572` (`FS_AGENT_TOOLS`) |

---

## 2. Options

| # | Option | Net effect | Effort | Keeps cloud/browser? |
|---|---|---|---|---|
| **A** | **Complete PRD-51** — migrate the 14 tools onto the backend, wire the router (one `_router`, `_get_backend` → `get_router()`), declare `boto3`, expose config to mount backends | One path (backend), multi-backend actually works | High | **Yes** |
| **B** | **Unify on one backend, drop multi** — migrate the 14 tools onto `LinuxFilesystemBackend`, delete `router.py` / `s3.py` / `windows.py` / `pyodide.py` / `FsToolKit`, remove the duplicate WorkspaceView tool logic | One path (single backend), no routing | Medium | No |
| **C** | **Revert to WorkspaceView** — move Family-B tools back onto `WorkspaceView`, delete the entire backend layer (`backend.py` + all backends + router + FsToolKit) | One path (WorkspaceView), simplest model | Medium | No |
| **D** | **Document only** — leave both, add a note | Status quo + a warning | Trivial | (dead) |

Notes:

- **A** realizes the original design and unlocks running agents against S3 mounts
  / in a Pyodide browser / on Windows — *if that is a product direction*.  It is
  the most code but deletes the duplication and the second `_router`.
- **B** keeps the clean `FilesystemBackend` Protocol + structured
  `FileStat`/`FileEntry`/`GrepMatch` returns and one concrete backend, but drops
  the (currently dead) cloud/browser/Windows code.  Easy to grow back toward A
  later by re-adding a backend + router.
- **C** is the smallest mental model but throws away the most work, including the
  structured return types and the Protocol seam.
- **D** leaves the exact confusion that triggered this PRD.

---

## 3. Recommendation

**Make a decision and commit — do not leave the migration half-finished.**

The deciding question is a product one: **is running agents against a non-local
filesystem (S3 mount, browser/WASM, first-class Windows) on the roadmap?**

- **If yes → Option A.** Finish what PRD-51/53/54 started: it's mostly wiring, the
  hard backend code already exists and is tested, and it removes the duplication
  as a side effect.  The work is wiring + a 14-tool migration, not new backends.
- **If no (the realistic default today) → Option B.** Consolidate every fs tool
  onto a single `LinuxFilesystemBackend`, delete the router and the three
  unreachable cloud backends + `FsToolKit`, and remove the duplicated WorkspaceView
  tool bodies.  This deletes ~4 modules of dead-in-production code and the
  duplicate I/O, keeps the clean Protocol seam and structured returns, and stays
  trivially re-openable toward A if cloud/browser ever lands.

**My lean: Option B**, unless someone confirms a concrete near-term need for
S3/browser/Windows.  Rationale: the multi-backend system has been 100 % dead and
is the source of the confusion; B removes the most dead code and the duplication
while preserving the one genuinely-useful abstraction (the Protocol + one
backend) and is reversible.  A is correct *only* if the cloud/browser capability
is actually going to ship — otherwise it's polishing code no one runs.

Either way, **also fix the registration gaps** (§1.6) and remove the dead
`FsToolKit` / second `_router`, regardless of A vs B.

---

## 4. Migration plan

### Common to A and B (do first)
1. Collapse to **one** router/backend accessor — delete the `agent_tools._router`
   + `agent_tools.configure_router` duplicate.
2. Resolve the **registration gaps**: either register `move_file`/`copy_file`/
   `list_directory`/`make_directory`/`read_lines`/`patch_file` or delete them.
3. Remove the import-time `assert isinstance(...)` side effect in `linux.py`.

### Option B (recommended default)
4. Point every Family-A tool at `LinuxFilesystemBackend` (one shared instance per
   workspace root) instead of the `WorkspaceView` `*Tool` classes.  Return shapes
   stay identical (PRD-51 G7).
5. Delete the now-duplicate `*Tool` classes from `fs/__init__.py` (or thin them to
   call the backend).
6. Delete `router.py`, `s3.py`, `windows.py`, `pyodide.py`, `FsToolKit`, and their
   tests (`test_s3_backend.py`, `test_windows_backend.py`, the router parts of
   `test_fs_backend_integration.py`).  Keep `backend.py` (Protocol + dataclasses)
   and `linux.py`.
7. Update CLAUDE.md / AGENTS.md / docs to describe one path.

### Option A (if cloud/browser is wanted)
4. Migrate the 14 tools onto the backend (per PRD-51 G2); delete the duplicate
   `*Tool` bodies.
5. Wire **one** router: `_get_backend()` → `router.get_router()`; call
   `configure_router()` once at session startup; let `_detect_default_backend()`
   choose Linux/Windows/Pyodide.
6. Declare `boto3` under a `cloud`/`s3` extra; add config (`[tools.fs]`) for
   mounting backends at path prefixes (e.g. `s3://bucket` → `/mnt/s3`).
7. Add an integration test that actually exercises a non-Linux backend through the
   *production* wiring (not a hand-injected router).

---

## 5. Testing strategy

- **Parity gate (both options):** before deleting anything, assert each Family-A
  tool returns byte-identical results to its backend equivalent for a fixture
  tree (read/write/append/delete/list/exists/stat/grep). This guards PRD-51 G7.
- **Sandbox tests stay green:** `..`, absolute-path, and symlink escapes still
  raise `PermissionError` on every fs tool (both families today; the single path
  after).
- **Option B:** delete the dead-backend suites; keep `test_linux_backend.py`,
  `test_fs_tools.py`, `test_new_fs_tools.py`, `test_fs_tools_e2e.py` (re-point any
  that injected a router).
- **Option A:** add a production-wiring test proving `_get_backend()` resolves the
  OS-appropriate backend, and a prefix-routing test for a mounted backend.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| Behavioural drift when migrating tools across paths | Parity gate (§5) before deleting either implementation |
| Deleting backends someone planned to use (Option B) | Decision gate in §3; B is reversible — the Protocol seam stays |
| Hidden external importer of a deleted backend | grep confirms zero non-test `src/` importers of `s3`/`windows`/`pyodide`/`router`/`FsToolKit` today |
| Registration changes alter the agent's tool set | Intentional; surface in CHANGELOG; the unregistered tools are currently unreachable anyway |

---

## 7. Acceptance criteria (for whichever option is chosen)

| # | Criterion |
|---|---|
| 130.1 | Exactly **one** fs implementation path remains; no duplicated read/write/etc. logic |
| 130.2 | Exactly **one** router/backend accessor; the second `_router`/`configure_router` is gone |
| 130.3 | No fs module is dead-in-production: every shipped module is reachable from `FS_AGENT_TOOLS` (or removed) |
| 130.4 | Registration gaps resolved (the six defined-but-unregistered tools are registered or deleted) |
| 130.5 | Sandbox escape tests pass for every fs tool on the surviving path |
| 130.6 | Parity gate green before any deletion (no behaviour change to tool return shapes) |
| 130.7 | (Option A only) a non-Linux backend is reachable through production wiring + has a test; `boto3` is a declared extra |
| 130.8 | CLAUDE.md / AGENTS.md / docs describe a single fs architecture |
