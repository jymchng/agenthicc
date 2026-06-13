---
title: "PRD-52: Expanded Filesystem Tools — New Tools + Batch CRUDs"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-51-fs-backend-protocol.md
---

# PRD-52: Expanded Filesystem Tools

## Executive Summary

The current 14 fs tools lack several primitives the agent frequently needs:
single-file grep, unified-diff application, checksum verification, bulk read/write,
and targeted line operations.  This PRD adds 10 new tools and refactors all 24 to
use the `FilesystemBackend` Protocol from PRD-51.

---

## New Tool Catalogue

| Tool | Key Parameters | Returns | Purpose |
|---|---|---|---|
| `grep_file` | `path, pattern, case_sensitive=True, context_lines=0` | `{matches: [{line_number, line, match_start, match_end}], total_matches}` | Grep a single file (no dir walk) |
| `apply_diff` | `path, diff, allow_partial=False` | `{ok, hunks_applied, hunks_failed, result}` | Apply a unified diff (`--- a/… +++ b/…`) to a file |
| `checksum_file` | `path, algorithm="sha256"` | `{path, algorithm, digest}` | SHA-256 / MD5 / blake2b digest |
| `truncate_file` | `path, size=0` | `{ok, new_size}` | Truncate file to N bytes (default: empty) |
| `touch_file` | `path, create=True` | `{ok, created}` | Create empty file or update mtime |
| `symlink` | `target, link_path` | `{ok}` | Create symlink (Linux backend only) |
| `batch_read` | `paths: list[str], encoding="utf-8"` | `{results: [{path, content, ok, error}]}` | Read multiple files in one call |
| `batch_write` | `files: [{path, content}], create_parents=True` | `{results: [{path, ok, error, bytes_written}]}` | Write multiple files atomically (best-effort) |
| `batch_delete` | `paths: list[str]` | `{results: [{path, ok, error}]}` | Delete multiple paths |
| `batch_move` | `moves: [{source, destination}]` | `{results: [{source, destination, ok, error}]}` | Move/rename multiple files |

---

## Tool Specifications

### `grep_file`

Single-file grep with optional context lines (like `grep -n -C N`).

```python
@tool()
async def grep_file(
    path: str,
    pattern: str,
    case_sensitive: bool = True,
    context_lines: int = 0,
) -> dict:
    """Search a single file for lines matching a regex pattern.

    Returns each match with its line number and optional surrounding context.
    Faster than grep_files when the agent already knows which file to search.
    """
    backend = _get_backend()
    try:
        text = backend.read_text(path)
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    except PermissionError:
        return {"ok": False, "error": "permission_denied"}

    import re
    flags = 0 if case_sensitive else re.IGNORECASE
    pat = re.compile(pattern, flags)
    lines = text.splitlines()
    matches = []
    for i, line in enumerate(lines, 1):
        m = pat.search(line)
        if not m:
            continue
        entry = {
            "line_number": i,
            "line": line,
            "match_start": m.start(),
            "match_end": m.end(),
        }
        if context_lines > 0:
            before = lines[max(0, i - 1 - context_lines): i - 1]
            after  = lines[i: min(len(lines), i + context_lines)]
            entry["context_before"] = before
            entry["context_after"]  = after
        matches.append(entry)

    return {"ok": True, "path": path, "matches": matches, "total_matches": len(matches)}
```

---

### `apply_diff`

Applies a well-formed unified diff (`--- a/file\n+++ b/file\n@@ … @@`) to an existing
file.  Each hunk is applied independently; `allow_partial=True` applies passing hunks
and reports failures without aborting.

```python
@tool()
async def apply_diff(
    path: str,
    diff: str,
    allow_partial: bool = False,
) -> dict:
    """Apply a unified diff to a file.

    The diff must use the standard unified format:
        --- a/path
        +++ b/path
        @@ -L,N +L,N @@
         context
        -removed line
        +added line

    Set allow_partial=True to apply clean hunks even if some fail.
    Returns the final file content in 'result' on success.
    """
    ...
```

Implementation sketch:

```python
import re as _re

_HUNK_HEADER = _re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

def _parse_hunks(diff: str) -> list[dict]:
    hunks = []
    current = None
    for line in diff.splitlines():
        m = _HUNK_HEADER.match(line)
        if m:
            if current:
                hunks.append(current)
            old_start = int(m.group(1))
            old_count = int(m.group(2) or 1)
            new_start = int(m.group(3))
            new_count = int(m.group(4) or 1)
            current = {
                "old_start": old_start, "old_count": old_count,
                "new_start": new_start, "new_count": new_count,
                "lines": [],
            }
        elif current is not None and line.startswith((" ", "+", "-")):
            current["lines"].append(line)
    if current:
        hunks.append(current)
    return hunks

def _apply_hunk(file_lines: list[str], hunk: dict) -> list[str] | None:
    """Return updated lines or None if the hunk doesn't match."""
    old_start = hunk["old_start"] - 1   # 0-indexed
    old_lines = [l[1:] for l in hunk["lines"] if l.startswith((" ", "-"))]
    # Verify context
    if file_lines[old_start: old_start + len(old_lines)] != old_lines:
        return None
    new_chunk = [l[1:] for l in hunk["lines"] if l.startswith((" ", "+"))]
    return file_lines[:old_start] + new_chunk + file_lines[old_start + len(old_lines):]
```

Return shape: `{"ok": bool, "hunks_applied": int, "hunks_failed": int, "result": str | None, "error": str | None}`

---

### `checksum_file`

```python
@tool()
async def checksum_file(
    path: str,
    algorithm: str = "sha256",
) -> dict:
    """Compute a cryptographic digest of a file.

    Supported algorithms: sha256 (default), sha1, md5, blake2b.
    Returns the hex digest.
    """
    import hashlib
    backend = _get_backend()
    data = backend.read_bytes(path)
    h = hashlib.new(algorithm, data)
    return {"ok": True, "path": path, "algorithm": algorithm, "digest": h.hexdigest()}
```

---

### `truncate_file`

```python
@tool()
async def truncate_file(path: str, size: int = 0) -> dict:
    """Truncate a file to *size* bytes. Defaults to empty (0 bytes)."""
    backend = _get_backend()
    backend.truncate(path, size)
    new_size = backend.stat(path).size
    return {"ok": True, "path": path, "new_size": new_size}
```

---

### `touch_file`

```python
@tool()
async def touch_file(path: str, create: bool = True) -> dict:
    """Create an empty file or update its modification time.

    If the file does not exist and create=True, it is created empty.
    If the file does not exist and create=False, returns ok=False.
    """
    backend = _get_backend()
    existed = backend.exists(path)
    if not existed and not create:
        return {"ok": False, "error": f"file not found: {path}", "created": False}
    if not existed:
        backend.write_text(path, "")
    else:
        # Update mtime via append+truncate
        backend.append_text(path, "")
    return {"ok": True, "path": path, "created": not existed}
```

---

### `batch_read`

```python
@tool()
async def batch_read(
    paths: list[str],
    encoding: str = "utf-8",
) -> dict:
    """Read multiple files in a single call.

    Avoids repeated round-trips when the agent needs several files at once.
    Each entry in 'results' has: path, content (str or null), ok, error.
    """
    backend = _get_backend()
    results = backend.batch_read(paths, encoding)
    total_ok = sum(1 for r in results if r["ok"])
    return {
        "ok": total_ok == len(paths),
        "results": results,
        "total": len(paths),
        "succeeded": total_ok,
        "failed": len(paths) - total_ok,
    }
```

---

### `batch_write`

```python
@tool()
async def batch_write(
    files: list[dict],
    create_parents: bool = True,
) -> dict:
    """Write multiple files in a single call.

    Each item in *files* must have "path" (str) and "content" (str).
    Files are written independently; a failure on one does not stop the others.
    Set create_parents=True (default) to auto-create missing directories.
    """
    backend = _get_backend()
    results = backend.batch_write(files, create_parents)
    total_ok = sum(1 for r in results if r["ok"])
    return {
        "ok": total_ok == len(files),
        "results": results,
        "total": len(files),
        "succeeded": total_ok,
        "failed": len(files) - total_ok,
    }
```

---

### `batch_delete`

```python
@tool()
async def batch_delete(paths: list[str]) -> dict:
    """Delete multiple files or empty directories.

    Each entry reports ok/error independently.
    """
    backend = _get_backend()
    results = backend.batch_delete(paths)
    total_ok = sum(1 for r in results if r["ok"])
    return {
        "ok": total_ok == len(paths),
        "results": results,
        "total": len(paths),
        "succeeded": total_ok,
        "failed": len(paths) - total_ok,
    }
```

---

### `batch_move`

```python
@tool()
async def batch_move(moves: list[dict]) -> dict:
    """Move or rename multiple files.

    Each item in *moves* must have "source" (str) and "destination" (str).
    """
    backend = _get_backend()
    results = []
    for m in moves:
        src, dst = m["source"], m["destination"]
        try:
            backend.move(src, dst)
            results.append({"source": src, "destination": dst, "ok": True, "error": None})
        except Exception as e:
            results.append({"source": src, "destination": dst, "ok": False, "error": str(e)})
    total_ok = sum(1 for r in results if r["ok"])
    return {"ok": total_ok == len(moves), "results": results,
            "succeeded": total_ok, "failed": len(moves) - total_ok}
```

---

## Full Updated `FS_AGENT_TOOLS` list (24 tools)

```python
FS_AGENT_TOOLS = [
    # Original 14
    read_file, write_file, append_file, delete_file, move_file, copy_file,
    list_directory, make_directory, file_exists, search_files, grep_files,
    get_file_info, read_lines, patch_file,
    # New 10
    grep_file, apply_diff, checksum_file, truncate_file, touch_file,
    batch_read, batch_write, batch_delete, batch_move,
    # symlink intentionally excluded from default list (Linux-only, not in Protocol)
]
```

---

## Tests

```python
# tests/unit/test_expanded_fs_tools.py  (pytestmark = pytest.mark.unit)

def test_grep_file_basic(tmp_path):
    from agenthicc.tools.fs.agent_tools import grep_file
    (tmp_path / "f.py").write_text("def foo():\n    return 1\n")
    result = asyncio.run(grep_file.__wrapped__("f.py", "def foo"))
    assert result["ok"] and result["total_matches"] == 1

def test_grep_file_context_lines(tmp_path): ...

def test_grep_file_case_insensitive(tmp_path): ...

def test_apply_diff_adds_line(tmp_path): ...
    # diff adds a line, verify result

def test_apply_diff_removes_line(tmp_path): ...

def test_apply_diff_bad_context_fails(tmp_path): ...
    # hunk context doesn't match file; allow_partial=False → ok=False

def test_apply_diff_partial_allowed(tmp_path): ...
    # two hunks: one valid, one not; allow_partial=True → hunks_applied=1

def test_checksum_sha256(tmp_path):
    (tmp_path / "f.txt").write_text("hello")
    result = asyncio.run(checksum_file.__wrapped__("f.txt"))
    assert result["algorithm"] == "sha256"
    assert len(result["digest"]) == 64

def test_truncate_file(tmp_path):
    (tmp_path / "f.txt").write_text("hello world")
    asyncio.run(truncate_file.__wrapped__("f.txt", 5))
    assert (tmp_path / "f.txt").read_text() == "hello"

def test_touch_creates_file(tmp_path):
    result = asyncio.run(touch_file.__wrapped__("new.txt"))
    assert result["created"]

def test_batch_read(tmp_path):
    for i in range(3):
        (tmp_path / f"{i}.txt").write_text(str(i))
    result = asyncio.run(batch_read.__wrapped__(["0.txt", "1.txt", "2.txt"]))
    assert result["ok"] and result["succeeded"] == 3

def test_batch_read_partial_failure(tmp_path):
    (tmp_path / "exists.txt").write_text("data")
    result = asyncio.run(batch_read.__wrapped__(["exists.txt", "missing.txt"]))
    assert not result["ok"]
    assert result["succeeded"] == 1 and result["failed"] == 1

def test_batch_write(tmp_path):
    files = [{"path": "a.txt", "content": "aaa"}, {"path": "b.txt", "content": "bbb"}]
    result = asyncio.run(batch_write.__wrapped__(files))
    assert result["ok"] and (tmp_path / "a.txt").read_text() == "aaa"

def test_batch_delete(tmp_path):
    for name in ["x.txt", "y.txt"]:
        (tmp_path / name).write_text("data")
    result = asyncio.run(batch_delete.__wrapped__(["x.txt", "y.txt"]))
    assert result["ok"] and not (tmp_path / "x.txt").exists()

def test_batch_move(tmp_path):
    (tmp_path / "src.txt").write_text("data")
    result = asyncio.run(batch_move.__wrapped__([{"source": "src.txt", "destination": "dst.txt"}]))
    assert result["ok"] and (tmp_path / "dst.txt").exists()

# Integration tests
# tests/integration/test_expanded_fs_integration.py
def test_batch_write_then_batch_read_roundtrip(tmp_path): ...
def test_apply_diff_multiline_edit(tmp_path): ...
def test_grep_file_vs_grep_files_consistency(tmp_path): ...
```
