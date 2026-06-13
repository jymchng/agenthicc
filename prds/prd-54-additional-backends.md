---
title: "PRD-54: Additional Filesystem Backends — Windows, Pyodide, Future"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-51-fs-backend-protocol.md
---

# PRD-54: Additional Filesystem Backends

## Executive Summary

With `FilesystemBackend` as the Protocol (PRD-51) and `LinuxFilesystemBackend`
as the reference (PRD-51), this PRD specifies the remaining concrete backends
and the extension mechanism for third-party backends.

---

## Backend Inventory

| Backend | Class | Platform | Key Dependency |
|---|---|---|---|
| `LinuxFilesystemBackend` | `linux.py` | POSIX | stdlib only |
| `WindowsFilesystemBackend` | `windows.py` | Win32 | stdlib + `pathlib` |
| `PyodideFilesystemBackend` | `pyodide.py` | Browser (WASM) | `pyodide.FS` |
| `S3FilesystemBackend` | `s3.py` | Any | `boto3` (PRD-53) |
| `GCSFilesystemBackend` | `gcs.py` | Any (future) | `google-cloud-storage` |
| `AzureBlobFilesystemBackend` | `azure.py` | Any (future) | `azure-storage-blob` |
| `SFTPFilesystemBackend` | `sftp.py` | Any (future) | `paramiko` |

---

## `WindowsFilesystemBackend`

Thin adapter over `LinuxFilesystemBackend` — Windows paths use `\` separators
and have drive letters (`C:\`), but Python's `pathlib.Path` abstracts both.

```python
# src/agenthicc/tools/fs/windows.py
from __future__ import annotations
from pathlib import Path, PureWindowsPath
from .linux import LinuxFilesystemBackend

class WindowsFilesystemBackend(LinuxFilesystemBackend):
    """Windows filesystem backend.

    Inherits all POSIX logic from LinuxFilesystemBackend.  Overrides:
    - name property → "windows"
    - stat() → no executable permission bit (always "")
    - symlink() → raises NotImplementedError (use shortcuts instead)
    - Path normalisation: converts forward slashes to backslashes in display
    """

    name = "windows"

    def stat(self, path: str):
        s = super().stat(path)
        # Windows has no Unix permission bits
        from dataclasses import replace
        return replace(s, permissions="", backend="windows")

    def symlink(self, target: str, link_path: str) -> None:
        raise NotImplementedError(
            "Symlinks on Windows require elevated privileges. "
            "Use a Windows shortcut (.lnk) instead."
        )
```

**Differences from Linux**:
- Max path length: 260 chars by default (32767 with long path support)
- Reserved names: `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`
- Case-insensitive filesystem (enforce via `path.lower()` comparisons in sandbox)

`WindowsFilesystemBackend` adds a `_check_reserved` helper:

```python
_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
})

def _check_reserved(self, path: str) -> None:
    name = Path(path).stem.upper()
    if name in _RESERVED:
        raise PermissionError(f"reserved Windows filename: {name!r}")
```

---

## `PyodideFilesystemBackend`

Pyodide runs Python in a browser via WASM.  It exposes `pyodide.FS` (Emscripten
MEMFS) — an in-memory filesystem that resets on page reload.

```python
# src/agenthicc/tools/fs/pyodide.py
from __future__ import annotations
from .backend import FileStat, FileEntry, GrepMatch

class PyodideFilesystemBackend:
    """Browser in-memory filesystem via pyodide.FS (Emscripten MEMFS).

    All operations are synchronous (pyodide.FS is not async).
    Data does not persist across page reloads unless explicitly synced.
    Max total memory is bounded by browser WASM heap (typically 256–512 MB).
    """

    name = "pyodide"

    def __init__(self, root: str = "/workspace") -> None:
        try:
            import pyodide  # noqa: F401
            import js        # noqa: F401
        except ImportError:
            raise ImportError("PyodideFilesystemBackend requires a Pyodide environment.")
        import pyodide.FS as _FS
        self._fs = _FS
        self._root = root
        try:
            _FS.mkdir(root)
        except Exception:
            pass  # already exists

    @property
    def root(self) -> str:
        return self._root

    def _resolve(self, path: str) -> str:
        """Resolve a relative path to an absolute MEMFS path inside root."""
        if path.startswith("/"):
            abs_path = path
        else:
            abs_path = f"{self._root}/{path.lstrip('/')}"
        # Prevent escape
        import posixpath
        normed = posixpath.normpath(abs_path)
        if not normed.startswith(self._root):
            raise PermissionError(f"path escape rejected: {path!r}")
        return normed

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._fs.readFile(self._resolve(path), {"encoding": "utf8"})

    def write_text(
        self, path: str, content: str, encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int:
        abs_path = self._resolve(path)
        if create_parents:
            parent = abs_path.rsplit("/", 1)[0]
            self._ensure_dir(parent)
        self._fs.writeFile(abs_path, content, {"encoding": "utf8"})
        return len(content.encode(encoding))

    def _ensure_dir(self, path: str) -> None:
        parts = path.split("/")
        cumulative = ""
        for part in parts:
            if not part:
                continue
            cumulative += "/" + part
            try:
                self._fs.mkdir(cumulative)
            except Exception:
                pass

    def exists(self, path: str) -> bool:
        try:
            self._fs.stat(self._resolve(path))
            return True
        except Exception:
            return False

    def delete(self, path: str) -> None:
        self._fs.unlink(self._resolve(path))

    # … remaining Protocol methods follow the same _fs.* pattern
    # read_bytes, append_text, truncate, move, copy, make_directory,
    # stat, list_dir, glob, grep, batch_* all implemented analogously.
```

**Limitations**:
- No `asyncio.to_thread` — Pyodide MEMFS is synchronous
- No filesystem persistence without explicit `FS.syncfs()`
- `grep` must load entire files into WASM memory

---

## Backend Auto-Detection

`BackendRouter` can optionally auto-detect the environment:

```python
# src/agenthicc/tools/fs/router.py

def _detect_default_backend(root: str = ".") -> FilesystemBackend:
    """Return the best backend for the current runtime environment."""
    import sys, os
    # Pyodide check
    if hasattr(sys, "_emscripten_info") or "pyodide" in sys.modules:
        from .pyodide import PyodideFilesystemBackend
        return PyodideFilesystemBackend(root)
    # Windows check
    if os.name == "nt":
        from .windows import WindowsFilesystemBackend
        return WindowsFilesystemBackend(root)
    # POSIX default
    from .linux import LinuxFilesystemBackend
    return LinuxFilesystemBackend(root)
```

`BackendRouter.__init__` uses `_detect_default_backend()` when no explicit default is provided.

---

## Third-Party Backend Plugin Convention

User-defined backends live in `.agenthicc/backends/<name>.py`:

```python
# .agenthicc/backends/sftp.py
from agenthicc.tools.fs.backend import FilesystemBackend, FileStat, FileEntry, GrepMatch

class SFTPBackend:
    name = "sftp"

    def __init__(self, host, port=22, username=None, key_path=None):
        import paramiko
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.connect(host, port, username=username, key_filename=key_path)
        self._sftp = self._client.open_sftp()

    # ... implement all FilesystemBackend Protocol methods ...

BACKEND = SFTPBackend  # export convention

# MOUNT_PREFIX = "sftp://hostname/"  # optional: prefix for BackendRouter
```

The loader pattern mirrors command/tool/mode plugins:

```python
# In session startup:
from agenthicc.tools.fs.plugin_loader import discover_backend_plugins
_plugins = discover_backend_plugins(project_dir=Path(".agenthicc"))
for result in _plugins.ok_results:
    instance = result.backend_class(**result.config)
    _backend_router.register(result.mount_prefix or "", instance)
```

---

## Future Backends (planned)

### `GCSFilesystemBackend`

```toml
[storage.gcs]
bucket         = "my-gcs-bucket"
project        = "my-project"
credentials    = ""          # path to service account JSON; empty = ADC
prefix         = ""
```

Maps to `google.cloud.storage.Client` + `Bucket.blob(key).download_as_text()` etc.

### `AzureBlobFilesystemBackend`

```toml
[storage.azure]
container        = "my-container"
account_name     = ""
account_key      = ""
connection_string = ""       # alternative to name+key
prefix           = ""
```

Maps to `azure.storage.blob.BlobServiceClient`.

### `SFTPFilesystemBackend`

```toml
[storage.sftp]
host      = "files.example.com"
port      = 22
username  = "deploy"
key_path  = "~/.ssh/id_ed25519"
prefix    = "/var/www/"
```

### `GitFilesystemBackend` (read-only, any commit)

```toml
[storage.git]
repo   = "."
ref    = "HEAD"    # branch, tag, or commit SHA
prefix = ""
```

Reads blobs from any git ref without checking out.  No write operations.

---

## Tests

```python
# tests/unit/test_windows_backend.py  (pytestmark = pytest.mark.unit)

def test_windows_backend_inherits_linux(tmp_path):
    from agenthicc.tools.fs.windows import WindowsFilesystemBackend
    b = WindowsFilesystemBackend(tmp_path)
    b.write_text("hello.txt", "world")
    assert b.read_text("hello.txt") == "world"
    assert b.name == "windows"

def test_windows_reserved_name_rejected(tmp_path):
    from agenthicc.tools.fs.windows import WindowsFilesystemBackend
    b = WindowsFilesystemBackend(tmp_path)
    with pytest.raises(PermissionError):
        b.write_text("CON.txt", "data")

def test_windows_stat_has_no_permissions(tmp_path):
    from agenthicc.tools.fs.windows import WindowsFilesystemBackend
    b = WindowsFilesystemBackend(tmp_path)
    b.write_text("f.txt", "x")
    assert b.stat("f.txt").permissions == ""

def test_backend_auto_detect_returns_linux():
    from agenthicc.tools.fs.router import _detect_default_backend
    import sys, os
    if os.name == "nt" or hasattr(sys, "_emscripten_info"):
        pytest.skip("not a POSIX environment")
    b = _detect_default_backend(".")
    assert b.name == "linux"

def test_backend_router_detects_env(tmp_path):
    from agenthicc.tools.fs.router import BackendRouter
    r = BackendRouter()
    assert r.default.name in ("linux", "windows", "pyodide")
```
