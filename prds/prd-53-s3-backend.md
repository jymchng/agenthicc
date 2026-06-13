---
title: "PRD-53: S3 Filesystem Backend + Config Integration"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-51-fs-backend-protocol.md
---

# PRD-53: S3 Filesystem Backend

## Executive Summary

S3-compatible object storage (AWS S3, MinIO, R2, etc.) exposed as a
`FilesystemBackend`.  Credentials are configured in `agenthicc.toml` —
per-project (`.agenthicc/agenthicc.toml`) or globally (`~/.agenthicc/agenthicc.toml`).
The backend is auto-registered in `BackendRouter` on session startup when a
`[storage.s3]` section is present.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `S3FilesystemBackend` implements `FilesystemBackend` Protocol fully |
| G2 | All S3 paths use `s3://bucket/key` URI form; backend strips the scheme |
| G3 | `[storage.s3.*]` TOML section in project or user config; project wins |
| G4 | `access_key_id` / `secret_access_key` can be omitted to use IAM / env vars |
| G5 | `endpoint_url` allows S3-compatible backends (MinIO, R2, etc.) |
| G6 | Per-session bucket mounting: `s3://bucket/prefix` → `BackendRouter` |
| G7 | Missing `boto3` produces a clear `ImportError` with install instructions |
| G8 | Batch operations use `concurrent.futures.ThreadPoolExecutor` for parallelism |
| G9 | S3 paths with sensitive credentials are never logged or shown in transcript |

---

## Config Schema

### Per-project (`./agenthicc/agenthicc.toml`)

```toml
[storage.s3]
bucket           = "my-project-bucket"
region           = "us-east-1"
prefix           = "workspace/"        # optional: key prefix for all operations
access_key_id    = ""                  # leave empty to use env / IAM profile
secret_access_key = ""
endpoint_url     = ""                  # S3-compatible override (MinIO etc.)
profile          = ""                  # AWS profile name (~/.aws/credentials)
path_style       = false               # true for MinIO / some S3-compat servers

[storage.s3.mounts]
# Additional named buckets accessible as s3://alias/key
"archive"  = { bucket = "my-archive-bucket", prefix = "", region = "eu-west-1" }
"readonly" = { bucket = "shared-assets",     prefix = "assets/" }
```

### Global user config (`~/.agenthicc/agenthicc.toml`)

Same `[storage.s3]` section. Project config takes precedence on any key that
appears in both.  User config supplies defaults (region, profile, credentials)
that are shared across projects.

### Merge semantics

```python
# Shallow merge: project overwrites user on a per-key basis
merged = {**user_s3_cfg, **project_s3_cfg}
```

---

## `StorageS3Settings` Dataclass

```python
# src/agenthicc/config.py — added to AgenthiccConfig

@dataclass
class StorageS3Settings:
    bucket: str = ""
    region: str = "us-east-1"
    prefix: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    endpoint_url: str = ""
    profile: str = ""
    path_style: bool = False
    mounts: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.bucket)


@dataclass
class StorageSettings:
    s3: StorageS3Settings = field(default_factory=StorageS3Settings)
    default_backend: str = "linux"     # "linux" | "s3"


@dataclass
class AgenthiccConfig:
    ...
    storage: StorageSettings = field(default_factory=StorageSettings)
```

TOML loading in `load_config()`: map `[storage]` and `[storage.s3]` into
`StorageSettings` and `StorageS3Settings` using the existing `deep_merge()`.

---

## `S3FilesystemBackend`

```python
# src/agenthicc/tools/fs/s3.py
from __future__ import annotations

import io
import os
import re
import concurrent.futures
from dataclasses import dataclass
from typing import Any

from .backend import FilesystemBackend, FileStat, FileEntry, GrepMatch

_WORKERS = 8   # thread pool for batch ops


class S3FilesystemBackend:
    """S3-compatible filesystem backend using boto3.

    Paths are plain key strings (no leading slash).
    s3://bucket/key URIs are accepted; the s3://bucket/ prefix is stripped.
    """

    name = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str = "us-east-1",
        access_key_id: str = "",
        secret_access_key: str = "",
        endpoint_url: str = "",
        profile: str = "",
        path_style: bool = False,
    ) -> None:
        try:
            import boto3
            import botocore.config
        except ImportError:
            raise ImportError(
                "S3 backend requires boto3. Install it with: pip install boto3"
            )
        self._bucket = bucket
        self._prefix = prefix.lstrip("/")
        self._uri_prefix = f"s3://{bucket}/{self._prefix}"

        kwargs: dict[str, Any] = {"region_name": region}
        if access_key_id and secret_access_key:
            kwargs["aws_access_key_id"]     = access_key_id
            kwargs["aws_secret_access_key"] = secret_access_key
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if profile:
            session = boto3.Session(profile_name=profile)
            self._s3 = session.client("s3", **kwargs)
        else:
            self._s3 = boto3.client("s3", **kwargs)

        if path_style:
            self._s3._endpoint.host = endpoint_url  # force path-style

    @property
    def root(self) -> str:
        return self._uri_prefix

    # ── key helpers ────────────────────────────────────────────────────────

    def _key(self, path: str) -> str:
        """Normalise a path to a full S3 key."""
        # Strip s3://bucket/ prefix if present
        if path.startswith("s3://"):
            path = path[len(f"s3://{self._bucket}/"):]
        return (self._prefix + path.lstrip("/")).lstrip("/")

    def _check_escape(self, path: str) -> None:
        """Raise PermissionError for .. traversal attempts."""
        if ".." in path.split("/"):
            raise PermissionError(f"path escape rejected: {path!r}")

    # ── reads ──────────────────────────────────────────────────────────────

    def read_bytes(self, path: str) -> bytes:
        self._check_escape(path)
        obj = self._s3.get_object(Bucket=self._bucket, Key=self._key(path))
        return obj["Body"].read()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def read_lines(
        self, path: str, start: int = 1, end: int | None = None
    ) -> tuple[list[str], int]:
        lines = self.read_text(path).splitlines()
        total = len(lines)
        s = max(0, start - 1)
        e = end if end is None else min(end, total)
        return lines[s:e], total

    # ── writes ─────────────────────────────────────────────────────────────

    def write_bytes(
        self, path: str, data: bytes, create_parents: bool = True
    ) -> int:
        self._check_escape(path)
        self._s3.put_object(Bucket=self._bucket, Key=self._key(path), Body=data)
        return len(data)

    def write_text(
        self, path: str, content: str, encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int:
        data = content.encode(encoding)
        return self.write_bytes(path, data, create_parents)

    def append_text(self, path: str, content: str) -> int:
        # S3 has no native append — download + concatenate + re-upload
        try:
            existing = self.read_text(path)
        except self._s3.exceptions.NoSuchKey:
            existing = ""
        new = existing + content
        return self.write_text(path, new)

    def truncate(self, path: str, size: int = 0) -> None:
        if size == 0:
            self.write_bytes(path, b"")
        else:
            data = self.read_bytes(path)
            self.write_bytes(path, data[:size])

    # ── CRUD ───────────────────────────────────────────────────────────────

    def delete(self, path: str) -> None:
        self._check_escape(path)
        key = self._key(path)
        # Check if it's a "directory" (prefix) by listing
        resp = self._s3.list_objects_v2(Bucket=self._bucket, Prefix=key + "/", MaxKeys=1)
        if resp.get("Contents"):
            # Delete all objects under the prefix
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=key + "/"):
                for obj in page.get("Contents", []):
                    self._s3.delete_object(Bucket=self._bucket, Key=obj["Key"])
        else:
            self._s3.delete_object(Bucket=self._bucket, Key=key)

    def move(self, source: str, destination: str) -> None:
        self.copy(source, destination)
        self.delete(source)

    def copy(self, source: str, destination: str) -> None:
        self._check_escape(source); self._check_escape(destination)
        self._s3.copy_object(
            Bucket=self._bucket,
            CopySource={"Bucket": self._bucket, "Key": self._key(source)},
            Key=self._key(destination),
        )

    def make_directory(self, path: str, parents: bool = True) -> None:
        # S3 has no real directories; create a zero-byte marker object
        key = self._key(path).rstrip("/") + "/"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=b"")

    # ── queries ────────────────────────────────────────────────────────────

    def exists(self, path: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._key(path))
            return True
        except Exception:
            return False

    def stat(self, path: str) -> FileStat:
        self._check_escape(path)
        resp = self._s3.head_object(Bucket=self._bucket, Key=self._key(path))
        import datetime
        mtime = resp.get("LastModified", datetime.datetime.utcnow()).timestamp()
        return FileStat(
            path=path,
            size=resp.get("ContentLength", -1),
            is_dir=path.endswith("/"),
            is_file=not path.endswith("/"),
            modified_at=mtime,
            created_at=mtime,
            etag=resp.get("ETag", "").strip('"'),
            backend="s3",
        )

    def list_dir(
        self, path: str = ".", pattern: str = "*",
        recursive: bool = False, include_hidden: bool = False,
    ) -> list[FileEntry]:
        import fnmatch
        prefix = (self._prefix + path.lstrip("./")).lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        delimiter = "" if recursive else "/"
        paginator = self._s3.get_paginator("list_objects_v2")
        entries: list[FileEntry] = []
        for page in paginator.paginate(
            Bucket=self._bucket, Prefix=prefix, Delimiter=delimiter
        ):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix):]
                if not rel:
                    continue
                name = rel.rstrip("/").split("/")[-1]
                if not include_hidden and name.startswith("."):
                    continue
                if not fnmatch.fnmatch(name, pattern):
                    continue
                entries.append(FileEntry(
                    name=name, path=obj["Key"][len(self._prefix):],
                    is_dir=rel.endswith("/"),
                    size=obj.get("Size", -1),
                ))
            for cp in page.get("CommonPrefixes", []):
                rel = cp["Prefix"][len(prefix):].rstrip("/")
                name = rel.split("/")[-1]
                if not include_hidden and name.startswith("."):
                    continue
                entries.append(FileEntry(
                    name=name, path=cp["Prefix"][len(self._prefix):].rstrip("/"),
                    is_dir=True, size=-1,
                ))
        return entries

    def glob(
        self, pattern: str, path: str = ".", recursive: bool = True
    ) -> list[str]:
        import fnmatch
        prefix = (self._prefix + path.lstrip("./")).lstrip("/")
        paginator = self._s3.get_paginator("list_objects_v2")
        results = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(self._prefix):]
                if fnmatch.fnmatch(rel, pattern):
                    results.append(rel)
        return results

    def grep(
        self, regex: str, path: str = ".",
        recursive: bool = True, max_results: int = 100,
        case_sensitive: bool = True,
    ) -> list[GrepMatch]:
        import re
        flags = 0 if case_sensitive else re.IGNORECASE
        pat = re.compile(regex, flags)
        files = self.glob("*", path, recursive)
        results: list[GrepMatch] = []
        for key in files:
            try:
                text = self.read_text(key)
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                m = pat.search(line)
                if m:
                    results.append(GrepMatch(
                        path=key, line_number=i, line=line,
                        match_start=m.start(), match_end=m.end(),
                    ))
                    if len(results) >= max_results:
                        return results
        return results

    # ── batch (parallel using thread pool) ────────────────────────────────

    def batch_read(
        self, paths: list[str], encoding: str = "utf-8"
    ) -> list[dict]:
        def _read_one(path):
            try:
                content = self.read_text(path, encoding)
                return {"path": path, "content": content, "ok": True, "error": None}
            except Exception as e:
                return {"path": path, "content": None, "ok": False, "error": str(e)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            return list(pool.map(_read_one, paths))

    def batch_write(
        self, files: list[dict[str, str]], create_parents: bool = True
    ) -> list[dict]:
        def _write_one(f):
            path, content = f["path"], f["content"]
            try:
                n = self.write_text(path, content)
                return {"path": path, "ok": True, "error": None, "bytes_written": n}
            except Exception as e:
                return {"path": path, "ok": False, "error": str(e), "bytes_written": 0}
        with concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            return list(pool.map(_write_one, files))

    def batch_delete(self, paths: list[str]) -> list[dict]:
        # Use S3 multi-object delete (up to 1000 keys per request) for efficiency
        keys = [{"Key": self._key(p)} for p in paths]
        resp = self._s3.delete_objects(
            Bucket=self._bucket,
            Delete={"Objects": keys, "Quiet": False},
        )
        deleted = {d["Key"] for d in resp.get("Deleted", [])}
        errors  = {e["Key"]: e.get("Message", "unknown") for e in resp.get("Errors", [])}
        return [
            {"path": p, "ok": self._key(p) in deleted,
             "error": errors.get(self._key(p))}
            for p in paths
        ]
```

---

## Session Startup Integration

```python
# In InlineRenderer.run() or __main__.py, after config is loaded:

from agenthicc.tools.fs.router import BackendRouter  # noqa: PLC0415
from agenthicc.tools.fs.linux import LinuxFilesystemBackend  # noqa: PLC0415

_backend_router = BackendRouter(LinuxFilesystemBackend(cwd))

s3_cfg = getattr(cfg.storage, "s3", None)
if s3_cfg and s3_cfg.configured:
    try:
        from agenthicc.tools.fs.s3 import S3FilesystemBackend  # noqa: PLC0415
        _s3 = S3FilesystemBackend(
            bucket=s3_cfg.bucket,
            prefix=s3_cfg.prefix,
            region=s3_cfg.region,
            access_key_id=s3_cfg.access_key_id,
            secret_access_key=s3_cfg.secret_access_key,
            endpoint_url=s3_cfg.endpoint_url,
            profile=s3_cfg.profile,
        )
        _backend_router.register("s3://", _s3)
        # Also register named mounts
        for alias, mount_cfg in s3_cfg.mounts.items():
            _mount_s3 = S3FilesystemBackend(**mount_cfg)
            _backend_router.register(f"s3://{alias}/", _mount_s3)
        console.print(f"[dim]S3 backend: s3://{s3_cfg.bucket}/{s3_cfg.prefix}[/dim]")
    except ImportError as e:
        console.print(f"[yellow]S3 backend disabled: {e}[/yellow]")
```

---

## Credential Security

- `access_key_id` and `secret_access_key` are **never** logged, printed, or
  included in transcript entries.
- The security policy in `build_policy_from_config()` redacts any tool argument
  whose key matches `/(key|secret|password|token|credential)/i` before logging.
- Recommend using IAM roles / environment variables (`AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`) instead of config file storage.
- The config file should be in `.gitignore`; the project template adds it.

---

## Tests

```python
# tests/unit/test_s3_backend.py  (pytestmark = pytest.mark.unit)
# Uses moto for S3 mocking — no real AWS calls

@pytest.fixture
def s3_backend():
    import boto3, moto
    with moto.mock_s3():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        from agenthicc.tools.fs.s3 import S3FilesystemBackend
        yield S3FilesystemBackend(bucket="test-bucket", region="us-east-1")

def test_s3_write_read(s3_backend):
    s3_backend.write_text("hello.txt", "world")
    assert s3_backend.read_text("hello.txt") == "world"

def test_s3_path_escape_rejected(s3_backend):
    with pytest.raises(PermissionError):
        s3_backend.read_text("../../secret")

def test_s3_batch_write_parallel(s3_backend):
    files = [{"path": f"{i}.txt", "content": str(i)} for i in range(10)]
    results = s3_backend.batch_write(files)
    assert all(r["ok"] for r in results)

def test_s3_list_dir(s3_backend):
    for name in ["a.txt", "b.txt"]:
        s3_backend.write_text(name, "x")
    entries = s3_backend.list_dir(".")
    names = [e.name for e in entries]
    assert "a.txt" in names and "b.txt" in names

def test_s3_missing_boto3_raises_import_error(monkeypatch):
    import builtins, importlib
    real_import = builtins.__import__
    def mock_import(name, *a, **kw):
        if name == "boto3":
            raise ImportError("no module named boto3")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", mock_import)
    with pytest.raises(ImportError, match="boto3"):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend
        S3FilesystemBackend(bucket="x")

def test_storage_settings_loaded_from_toml(tmp_path):
    cfg_file = tmp_path / "agenthicc.toml"
    cfg_file.write_text(
        '[storage.s3]\nbucket = "my-bucket"\nregion = "eu-west-1"\n'
    )
    from agenthicc.config import load_config
    cfg = load_config(project_path=cfg_file)
    assert cfg.storage.s3.bucket == "my-bucket"
    assert cfg.storage.s3.region == "eu-west-1"
    assert cfg.storage.s3.configured

def test_storage_s3_not_configured_by_default():
    from agenthicc.config import AgenthiccConfig
    cfg = AgenthiccConfig()
    assert not cfg.storage.s3.configured
```
