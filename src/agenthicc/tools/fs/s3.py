"""S3 filesystem backend — wraps boto3 for object-storage access."""
from __future__ import annotations

import concurrent.futures
import datetime
import fnmatch
import re
import time

from .backend import FileEntry, FileStat, GrepMatch

__all__ = ["S3FilesystemBackend"]

_WORKERS = 8


class S3FilesystemBackend:
    """Filesystem backend backed by an Amazon S3 (or S3-compatible) bucket.

    Satisfies the :class:`.backend.FilesystemBackend` Protocol.
    """

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
            import boto3  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "S3 backend requires boto3. Install: pip install boto3"
            ) from exc

        self._bucket = bucket
        self._prefix = prefix.lstrip("/")

        client_kwargs: dict[str, object] = {"region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if path_style and endpoint_url:
            client_kwargs["config"] = _make_path_style_config()

        if profile:
            session = boto3.Session(profile_name=profile)
            self._s3 = session.client("s3", **client_kwargs)
        else:
            if access_key_id and secret_access_key:
                client_kwargs["aws_access_key_id"] = access_key_id
                client_kwargs["aws_secret_access_key"] = secret_access_key
            self._s3 = boto3.client("s3", **client_kwargs)

    # ------------------------------------------------------------------
    # Identity (Protocol properties)
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "s3"

    @property
    def root(self) -> str:
        return f"s3://{self._bucket}/{self._prefix}"

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _key(self, path: str) -> str:
        """Normalise *path* into an S3 object key.

        Strips an ``s3://<bucket>/`` prefix when present, then prepends
        ``self._prefix``.
        """
        p = path
        s3_prefix = f"s3://{self._bucket}/"
        if p.startswith(s3_prefix):
            p = p[len(s3_prefix):]
        elif p.startswith("s3://"):
            slash = p.find("/", 5)
            p = p[slash + 1:] if slash != -1 else ""
        p = p.lstrip("/")
        if self._prefix:
            return f"{self._prefix}/{p}" if p else self._prefix
        return p

    def _check_escape(self, path: str) -> None:
        """Raise PermissionError if the path contains a ``..`` segment."""
        if ".." in path.split("/"):
            raise PermissionError(f"path traversal detected: {path!r}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_404(self, exc: Exception) -> bool:
        try:
            import botocore.exceptions  # noqa: PLC0415
        except ImportError:
            return False
        if isinstance(exc, botocore.exceptions.ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            return code in ("404", "NoSuchKey")
        return False

    def _dt_to_timestamp(self, dt: datetime.datetime) -> float:
        try:
            return dt.timestamp()
        except Exception:
            return time.time()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read_bytes(self, path: str) -> bytes:
        self._check_escape(path)
        key = self._key(path)
        resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def read_lines(
        self,
        path: str,
        start: int = 1,
        end: int | None = None,
    ) -> tuple[list[str], int]:
        """Return ``(lines[start-1:end], total_line_count)`` for *path*."""
        all_lines = self.read_text(path).splitlines()
        total = len(all_lines)
        s = max(0, start - 1)
        e = end if end is not None else total
        return all_lines[s:e], total

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def write_bytes(
        self,
        path: str,
        data: bytes,
        create_parents: bool = True,
    ) -> int:
        self._check_escape(path)
        key = self._key(path)
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return len(data)

    def write_text(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        create_parents: bool = True,
    ) -> int:
        data = content.encode(encoding)
        return self.write_bytes(path, data, create_parents=create_parents)

    def append_text(self, path: str, content: str) -> int:
        try:
            existing = self.read_text(path)
        except Exception as exc:
            if self._is_404(exc):
                existing = ""
            else:
                raise
        appended = content.encode()
        self.write_text(path, existing + content)
        return len(appended)

    def truncate(self, path: str, size: int = 0) -> None:
        if size == 0:
            self.write_bytes(path, b"")
            return
        data = self.read_bytes(path)
        self.write_bytes(path, data[:size])

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def delete(self, path: str) -> None:
        self._check_escape(path)
        key = self._key(path)
        # Detect directory-like prefix — list with trailing "/"
        dir_prefix = key.rstrip("/") + "/"
        paginator = self._s3.get_paginator("list_objects_v2")
        keys_to_delete: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=dir_prefix):
            for obj in page.get("Contents", []):
                keys_to_delete.append(obj["Key"])

        if keys_to_delete:
            for i in range(0, len(keys_to_delete), 1000):
                chunk = keys_to_delete[i : i + 1000]
                self._s3.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
                )
        else:
            self._s3.delete_object(Bucket=self._bucket, Key=key)

    def move(self, src: str, dst: str) -> None:
        self.copy(src, dst)
        self.delete(src)

    def copy(self, src: str, dst: str) -> None:
        self._check_escape(src)
        self._check_escape(dst)
        src_key = self._key(src)
        dst_key = self._key(dst)
        self._s3.copy_object(
            Bucket=self._bucket,
            CopySource={"Bucket": self._bucket, "Key": src_key},
            Key=dst_key,
        )

    def make_directory(self, path: str, parents: bool = True) -> None:
        self._check_escape(path)
        key = self._key(path).rstrip("/") + "/"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=b"")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        import botocore.exceptions  # noqa: PLC0415

        self._check_escape(path)
        key = self._key(path)
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except botocore.exceptions.ClientError as exc:
            if self._is_404(exc):
                return False
            raise

    def stat(self, path: str) -> FileStat:
        self._check_escape(path)
        key = self._key(path)
        resp = self._s3.head_object(Bucket=self._bucket, Key=key)
        last_modified: datetime.datetime = resp.get(
            "LastModified", datetime.datetime.utcnow()
        )
        etag = resp.get("ETag", "").strip('"')
        ts = self._dt_to_timestamp(last_modified)
        return FileStat(
            path=path,
            size=resp.get("ContentLength", 0),
            is_dir=False,
            is_file=True,
            modified_at=ts,
            created_at=ts,
            etag=etag,
            backend="s3",
        )

    def list_dir(
        self,
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        include_hidden: bool = False,
    ) -> list[FileEntry]:
        if path not in (".", ""):
            self._check_escape(path)
        prefix = self._key(path) if path not in (".", "") else self._prefix
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        paginator = self._s3.get_paginator("list_objects_v2")
        kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
        if not recursive:
            kwargs["Delimiter"] = "/"

        entries: list[FileEntry] = []
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key == prefix:
                    continue
                rel = key[len(prefix):]
                if not rel:
                    continue
                name = rel.rstrip("/").rsplit("/", 1)[-1]
                if not include_hidden and name.startswith("."):
                    continue
                if not fnmatch.fnmatch(name, pattern) and not fnmatch.fnmatch(rel, pattern):
                    continue
                entries.append(
                    FileEntry(
                        name=name,
                        path=key,
                        is_dir=False,
                        size=obj.get("Size", 0),
                    )
                )
            for cp in page.get("CommonPrefixes", []):
                cp_key: str = cp["Prefix"]
                dir_name = cp_key.rstrip("/").rsplit("/", 1)[-1]
                if not include_hidden and dir_name.startswith("."):
                    continue
                entries.append(
                    FileEntry(
                        name=dir_name,
                        path=cp_key,
                        is_dir=True,
                    )
                )
        return entries

    def glob(
        self,
        pattern: str,
        path: str = ".",
        recursive: bool = True,
    ) -> list[str]:
        if path not in (".", ""):
            self._check_escape(path)
        prefix = self._key(path) if path not in (".", "") else self._prefix
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        paginator = self._s3.get_paginator("list_objects_v2")
        matches: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(prefix):]
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(key, pattern):
                    matches.append(key)
        return matches

    def grep(
        self,
        regex: str,
        path: str = ".",
        recursive: bool = True,
        max_results: int = 100,
        case_sensitive: bool = True,
    ) -> list[GrepMatch]:
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(regex, flags)
        files = self.glob("*", path=path, recursive=recursive)
        results: list[GrepMatch] = []
        for file_key in files:
            try:
                text = self.read_text(file_key)
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                m = compiled.search(line)
                if m:
                    results.append(
                        GrepMatch(
                            path=file_key,
                            line_number=i,
                            line=line.rstrip(),
                            match_start=m.start(),
                            match_end=m.end(),
                        )
                    )
                    if len(results) >= max_results:
                        return results
        return results

    # ------------------------------------------------------------------
    # Batch helpers
    # ------------------------------------------------------------------

    def _read_one(self, path: str, encoding: str) -> dict[str, object]:
        try:
            content = self.read_text(path, encoding=encoding)
            return {"path": path, "content": content, "ok": True, "error": None}
        except Exception as exc:
            return {"path": path, "content": None, "ok": False, "error": str(exc)}

    def _write_one(self, f: dict[str, object], create_parents: bool) -> dict[str, object]:
        path: str = f["path"]
        content: str = f.get("content", "")
        encoding: str = f.get("encoding", "utf-8")
        try:
            written = self.write_text(path, content, encoding=encoding, create_parents=create_parents)
            return {"path": path, "ok": True, "error": None, "bytes_written": written}
        except Exception as exc:
            return {"path": path, "ok": False, "error": str(exc), "bytes_written": 0}

    def batch_read(
        self,
        paths: list[str],
        encoding: str = "utf-8",
    ) -> list[dict]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futures = {pool.submit(self._read_one, p, encoding): p for p in paths}
            results: list[dict] = []
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())
        order = {p: i for i, p in enumerate(paths)}
        results.sort(key=lambda r: order.get(r["path"], 0))
        return results

    def batch_write(
        self,
        files: list[dict],
        create_parents: bool = True,
    ) -> list[dict]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futures = {
                pool.submit(self._write_one, f, create_parents): f["path"] for f in files
            }
            results: list[dict] = []
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())
        order = {f["path"]: i for i, f in enumerate(files)}
        results.sort(key=lambda r: order.get(r["path"], 0))
        return results

    def batch_delete(self, paths: list[str]) -> list[dict]:
        """Delete multiple objects in bulk using S3's native delete_objects API."""
        if not paths:
            return []

        keys = [self._key(p) for p in paths]
        key_to_path = dict(zip(keys, paths))

        results: list[dict] = []
        for i in range(0, len(keys), 1000):
            chunk_keys = keys[i : i + 1000]
            resp = self._s3.delete_objects(
                Bucket=self._bucket,
                Delete={
                    "Objects": [{"Key": k} for k in chunk_keys],
                    "Quiet": False,
                },
            )
            for deleted in resp.get("Deleted", []):
                k = deleted["Key"]
                results.append({"path": key_to_path.get(k, k), "ok": True, "error": None})
            for err in resp.get("Errors", []):
                k = err.get("Key", "")
                results.append(
                    {
                        "path": key_to_path.get(k, k),
                        "ok": False,
                        "error": f"{err.get('Code', 'UnknownError')}: {err.get('Message', '')}",
                    }
                )
        order = {p: idx for idx, p in enumerate(paths)}
        results.sort(key=lambda r: order.get(r["path"], 0))
        return results


# ------------------------------------------------------------------
# Private helper — avoids importing botocore at module load time
# ------------------------------------------------------------------

def _make_path_style_config() -> object:
    from botocore.config import Config  # noqa: PLC0415

    return Config(s3={"addressing_style": "path"})
