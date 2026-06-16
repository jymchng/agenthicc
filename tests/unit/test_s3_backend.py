"""Unit tests for S3FilesystemBackend.

boto3 / moto are NOT installed in this environment; all tests use
unittest.mock to inject a fake boto3 module at import time.
The HAS_MOTO / skip_no_moto scaffolding is kept so tests become live
moto tests if the package is later added.
"""
from __future__ import annotations

import datetime
import sys
import types
from unittest.mock import MagicMock

import pytest

try:
    import moto  # noqa: F401

    HAS_MOTO = True
except ImportError:
    HAS_MOTO = False

pytestmark = pytest.mark.unit
skip_no_moto = pytest.mark.skipif(not HAS_MOTO, reason="moto not installed")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUCKET = "test-bucket"
_REGION = "us-east-1"


def _fake_body(content: bytes) -> MagicMock:
    """Return a mock StreamingBody-like object."""
    body = MagicMock()
    body.read.return_value = content
    return body


def _s3_response(content: bytes) -> dict:
    return {"Body": _fake_body(content)}



def _build_backend(
    bucket: str = _BUCKET,
    prefix: str = "",
    mock_client: MagicMock | None = None,
) -> tuple:
    """Instantiate S3FilesystemBackend with a mock boto3 client.

    The returned backend keeps the fake botocore.exceptions in sys.modules so
    that internal ``import botocore.exceptions`` calls inside methods work.

    Returns ``(backend, mock_client)``.
    """
    if mock_client is None:
        mock_client = MagicMock()

    # Build fake botocore.exceptions with a real subclass of Exception
    class _ClientError(Exception):
        def __init__(self, error_response, operation_name):
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(str(error_response))

    fake_botocore_exc = types.ModuleType("botocore.exceptions")
    fake_botocore_exc.ClientError = _ClientError  # type: ignore[attr-defined]

    fake_botocore = types.ModuleType("botocore")
    fake_botocore.exceptions = fake_botocore_exc  # type: ignore[attr-defined]

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = MagicMock(return_value=mock_client)  # type: ignore[attr-defined]
    fake_boto3.Session = MagicMock()  # type: ignore[attr-defined]

    # Remove any stale cached s3 module before re-importing
    sys.modules.pop("agenthicc.tools.fs.s3", None)

    # Inject fakes into sys.modules for both construction and later method calls
    sys.modules.setdefault("boto3", fake_boto3)
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.exceptions"] = fake_botocore_exc

    from agenthicc.tools.fs.s3 import S3FilesystemBackend  # fresh import

    backend = S3FilesystemBackend(bucket=bucket, prefix=prefix)

    # Swap out the real client reference for our mock
    backend._s3 = mock_client
    # Keep a reference to the ClientError class for use in tests
    backend._ClientError = _ClientError  # type: ignore[attr-defined]
    return backend, mock_client


def _make_paginator(pages: list[dict]) -> MagicMock:
    """Return a paginator mock that yields *pages* when iterated."""
    pager = MagicMock()
    pager.paginate.return_value = iter(pages)
    return pager


def _client_error(code: str, message: str = "error") -> Exception:
    """Build a ClientError using whatever is in sys.modules['botocore.exceptions']."""
    exc_mod = sys.modules.get("botocore.exceptions")
    if exc_mod is None:
        raise RuntimeError("botocore.exceptions not injected — call _build_backend first")
    return exc_mod.ClientError(  # type: ignore[attr-defined]
        {"Error": {"Code": code, "Message": message}}, "Operation"
    )


# ---------------------------------------------------------------------------
# @skip_no_moto tests — these run only when moto is installed.
# With moto absent they are skipped; the mock-based counterparts below
# cover the same logic unconditionally.
# ---------------------------------------------------------------------------


@skip_no_moto
class TestMotoRoundTrips:
    """Live moto tests (skipped unless moto is installed)."""

    @pytest.fixture
    def s3_setup(self):
        import boto3
        import moto

        with moto.mock_aws():
            conn = boto3.client("s3", region_name=_REGION)
            conn.create_bucket(Bucket=_BUCKET)
            yield conn

    def test_s3_write_read_roundtrip(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_text("hello.txt", "hello world")
        assert b.read_text("hello.txt") == "hello world"

    def test_s3_read_bytes(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_bytes("bin.bin", b"\x00\x01\x02")
        assert b.read_bytes("bin.bin") == b"\x00\x01\x02"

    def test_s3_append_text(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_text("app.txt", "line1\n")
        b.append_text("app.txt", "line2\n")
        assert b.read_text("app.txt") == "line1\nline2\n"

    def test_s3_delete_object(self, s3_setup):
        import botocore.exceptions
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_text("todel.txt", "bye")
        b.delete("todel.txt")
        with pytest.raises(botocore.exceptions.ClientError):
            b.read_bytes("todel.txt")

    def test_s3_exists_true_false(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_text("present.txt", "yes")
        assert b.exists("present.txt") is True
        assert b.exists("absent.txt") is False

    def test_s3_stat_fields(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_bytes("stat.bin", b"abc")
        st = b.stat("stat.bin")
        assert st.size == 3
        assert st.etag != ""
        assert st.backend == "s3"

    def test_s3_list_dir(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_text("dir/a.txt", "a")
        b.write_text("dir/b.txt", "b")
        entries = b.list_dir("dir")
        names = [e.name for e in entries]
        assert "a.txt" in names
        assert "b.txt" in names

    def test_s3_glob(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_text("glob/x.py", "")
        b.write_text("glob/y.txt", "")
        py_files = b.glob("*.py", path="glob")
        assert any(k.endswith("x.py") for k in py_files)
        assert not any(k.endswith("y.txt") for k in py_files)

    def test_s3_batch_write_parallel(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        files = [{"path": f"batch/file{i}.txt", "content": f"content{i}"} for i in range(10)]
        results = b.batch_write(files)
        assert len(results) == 10
        assert all(r["ok"] for r in results)
        assert b.read_text("batch/file5.txt") == "content5"

    def test_s3_batch_delete(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        paths = [f"bdel/file{i}.txt" for i in range(5)]
        for p in paths:
            b.write_text(p, "x")
        results = b.batch_delete(paths)
        assert all(r["ok"] for r in results)

    def test_s3_batch_read_partial_failure(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_text("partial/good.txt", "good")
        results = b.batch_read(["partial/good.txt", "partial/missing.txt"])
        by_path = {r["path"]: r for r in results}
        assert by_path["partial/good.txt"]["ok"] is True
        assert by_path["partial/missing.txt"]["ok"] is False

    def test_s3_path_escape_rejected(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        with pytest.raises(PermissionError):
            b.read_text("../escape.txt")

    def test_s3_key_strips_prefix(self, s3_setup):
        import boto3
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION, prefix="workspace/")
        b.write_text("myfile.txt", "data")
        conn = boto3.client("s3", region_name=_REGION)
        resp = conn.get_object(Bucket=_BUCKET, Key="workspace/myfile.txt")
        assert resp["Body"].read() == b"data"

    def test_s3_uri_path_accepted(self, s3_setup):
        from agenthicc.tools.fs.s3 import S3FilesystemBackend

        b = S3FilesystemBackend(bucket=_BUCKET, region=_REGION)
        b.write_text("myfile.txt", "via uri")
        assert b.read_text(f"s3://{_BUCKET}/myfile.txt") == "via uri"


# ---------------------------------------------------------------------------
# Always-run tests — use unittest.mock; no moto required
# ---------------------------------------------------------------------------


class TestS3WriteReadRoundtrip:
    def test_s3_write_read_roundtrip(self):
        backend, mock_client = _build_backend()
        mock_client.get_object.return_value = _s3_response(b"hello world")

        backend.write_text("hello.txt", "hello world")
        mock_client.put_object.assert_called_once_with(
            Bucket=_BUCKET, Key="hello.txt", Body=b"hello world"
        )

        text = backend.read_text("hello.txt")
        mock_client.get_object.assert_called_once_with(Bucket=_BUCKET, Key="hello.txt")
        assert text == "hello world"


class TestS3ReadBytes:
    def test_s3_read_bytes(self):
        backend, mock_client = _build_backend()
        mock_client.get_object.return_value = _s3_response(b"\x00\x01\x02")

        result = backend.read_bytes("bin.bin")

        mock_client.get_object.assert_called_once_with(Bucket=_BUCKET, Key="bin.bin")
        assert result == b"\x00\x01\x02"


class TestS3AppendText:
    def test_s3_append_text(self):
        backend, mock_client = _build_backend()
        # First read returns existing content; subsequent get_object for write
        mock_client.get_object.return_value = _s3_response(b"line1\n")

        n = backend.append_text("app.txt", "line2\n")

        # Should have read the file once, then written the combined content
        mock_client.get_object.assert_called_once_with(Bucket=_BUCKET, Key="app.txt")
        mock_client.put_object.assert_called_once_with(
            Bucket=_BUCKET, Key="app.txt", Body=b"line1\nline2\n"
        )
        assert n == len(b"line2\n")

    def test_s3_append_text_creates_if_missing(self):
        """append_text on a missing key should create the object."""
        backend, mock_client = _build_backend()

        mock_client.get_object.side_effect = _client_error("NoSuchKey", "Not Found")

        backend.append_text("new.txt", "first\n")

        mock_client.put_object.assert_called_once_with(
            Bucket=_BUCKET, Key="new.txt", Body=b"first\n"
        )


class TestS3DeleteObject:
    def test_s3_delete_object(self):
        backend, mock_client = _build_backend()
        # Simulate no directory objects
        pager = _make_paginator([{"Contents": [], "CommonPrefixes": []}])
        mock_client.get_paginator.return_value = pager

        backend.delete("todel.txt")

        mock_client.delete_object.assert_called_once_with(Bucket=_BUCKET, Key="todel.txt")

    def test_s3_delete_directory_uses_bulk_delete(self):
        """Deleting a path that expands to multiple keys uses delete_objects."""
        backend, mock_client = _build_backend()
        pager = _make_paginator(
            [{"Contents": [{"Key": "dir/a.txt"}, {"Key": "dir/b.txt"}]}]
        )
        mock_client.get_paginator.return_value = pager
        mock_client.delete_objects.return_value = {
            "Deleted": [{"Key": "dir/a.txt"}, {"Key": "dir/b.txt"}],
            "Errors": [],
        }

        backend.delete("dir")

        mock_client.delete_objects.assert_called_once()
        args = mock_client.delete_objects.call_args
        objects = args[1]["Delete"]["Objects"]
        keys = {o["Key"] for o in objects}
        assert keys == {"dir/a.txt", "dir/b.txt"}


class TestS3ExistsTrueFalse:
    def test_exists_true(self):
        backend, mock_client = _build_backend()
        mock_client.head_object.return_value = {"ContentLength": 5, "ETag": '"abc"'}

        assert backend.exists("present.txt") is True
        mock_client.head_object.assert_called_once_with(Bucket=_BUCKET, Key="present.txt")

    def test_exists_false(self):
        backend, mock_client = _build_backend()
        mock_client.head_object.side_effect = _client_error("404", "Not Found")

        assert backend.exists("absent.txt") is False


class TestS3StatFields:
    def test_s3_stat_fields(self):
        from agenthicc.tools.fs.backend import FileStat

        backend, mock_client = _build_backend()
        now = datetime.datetime(2024, 1, 15, 10, 0, 0, tzinfo=datetime.timezone.utc)
        mock_client.head_object.return_value = {
            "ContentLength": 42,
            "ETag": '"deadbeef"',
            "LastModified": now,
        }

        st = backend.stat("stat.bin")

        assert isinstance(st, FileStat)
        assert st.size == 42
        assert st.etag == "deadbeef"  # quotes stripped
        assert st.backend == "s3"
        assert st.is_file is True
        assert st.is_dir is False


class TestS3ListDir:
    def test_s3_list_dir(self):
        backend, mock_client = _build_backend()
        pager = _make_paginator(
            [
                {
                    "Contents": [
                        {"Key": "dir/a.txt", "Size": 10},
                        {"Key": "dir/b.txt", "Size": 20},
                        {"Key": "dir/", "Size": 0},  # directory marker — skipped
                    ],
                    "CommonPrefixes": [],
                }
            ]
        )
        mock_client.get_paginator.return_value = pager

        entries = backend.list_dir("dir")

        names = [e.name for e in entries]
        assert "a.txt" in names
        assert "b.txt" in names
        # directory marker key "dir/" has empty rel and should be skipped
        assert "" not in names

    def test_s3_list_dir_includes_subdirs(self):
        backend, mock_client = _build_backend()
        pager = _make_paginator(
            [
                {
                    "Contents": [{"Key": "root/file.txt", "Size": 5}],
                    "CommonPrefixes": [{"Prefix": "root/sub/"}],
                }
            ]
        )
        mock_client.get_paginator.return_value = pager

        entries = backend.list_dir("root")

        is_dirs = {e.name: e.is_dir for e in entries}
        assert is_dirs.get("file.txt") is False
        assert is_dirs.get("sub") is True


class TestS3Glob:
    def test_s3_glob(self):
        backend, mock_client = _build_backend()
        pager = _make_paginator(
            [
                {
                    "Contents": [
                        {"Key": "glob/x.py"},
                        {"Key": "glob/y.txt"},
                        {"Key": "glob/z.py"},
                    ]
                }
            ]
        )
        mock_client.get_paginator.return_value = pager

        py_files = backend.glob("*.py", path="glob")

        assert "glob/x.py" in py_files
        assert "glob/z.py" in py_files
        assert "glob/y.txt" not in py_files


class TestS3BatchWriteParallel:
    def test_s3_batch_write_parallel(self):
        """10 files written in one batch_write call all succeed."""
        backend, mock_client = _build_backend()
        files = [{"path": f"batch/file{i}.txt", "content": f"content{i}"} for i in range(10)]

        results = backend.batch_write(files)

        assert len(results) == 10
        assert all(r["ok"] for r in results)
        # Results should be in the same order as the input
        for i, r in enumerate(results):
            assert r["path"] == f"batch/file{i}.txt"
        # put_object should have been called once per file
        assert mock_client.put_object.call_count == 10


class TestS3BatchDelete:
    def test_s3_batch_delete(self):
        """batch_delete uses delete_objects and returns ok=True for all."""
        backend, mock_client = _build_backend()
        paths = [f"bdel/file{i}.txt" for i in range(5)]
        keys = [f"bdel/file{i}.txt" for i in range(5)]

        mock_client.delete_objects.return_value = {
            "Deleted": [{"Key": k} for k in keys],
            "Errors": [],
        }

        results = backend.batch_delete(paths)

        assert len(results) == 5
        assert all(r["ok"] for r in results)
        mock_client.delete_objects.assert_called_once()
        delete_arg = mock_client.delete_objects.call_args[1]["Delete"]
        submitted_keys = {o["Key"] for o in delete_arg["Objects"]}
        assert submitted_keys == set(keys)

    def test_s3_batch_delete_empty(self):
        backend, mock_client = _build_backend()
        results = backend.batch_delete([])
        assert results == []
        mock_client.delete_objects.assert_not_called()


class TestS3BatchReadPartialFailure:
    def test_s3_batch_read_partial_failure(self):
        """One missing key should produce ok=False; the good key ok=True."""
        backend, mock_client = _build_backend()
        missing_err = _client_error("NoSuchKey", "Not Found")

        def get_object_side_effect(Bucket, Key):
            if Key == "partial/missing.txt":
                raise missing_err
            return _s3_response(b"good content")

        mock_client.get_object.side_effect = get_object_side_effect

        results = backend.batch_read(["partial/good.txt", "partial/missing.txt"])

        by_path = {r["path"]: r for r in results}
        assert by_path["partial/good.txt"]["ok"] is True
        assert by_path["partial/good.txt"]["content"] == "good content"
        assert by_path["partial/missing.txt"]["ok"] is False
        assert by_path["partial/missing.txt"]["content"] is None


class TestS3PathEscapeRejected:
    def test_s3_path_escape_rejected(self):
        backend, mock_client = _build_backend()

        with pytest.raises(PermissionError, match="path traversal"):
            backend.read_text("../escape.txt")

    def test_s3_path_escape_in_subdir(self):
        backend, mock_client = _build_backend()

        with pytest.raises(PermissionError):
            backend.write_text("subdir/../../etc/passwd", "evil")

    def test_s3_path_escape_on_delete(self):
        backend, mock_client = _build_backend()

        with pytest.raises(PermissionError):
            backend.delete("subdir/../../../etc/passwd")


class TestS3KeyStripsPrefix:
    def test_s3_key_strips_prefix(self):
        """write_text with prefix='workspace' should store key as 'workspace/myfile.txt'."""
        # Note: pass prefix WITHOUT trailing slash; _key normalises it
        backend, mock_client = _build_backend(prefix="workspace")

        backend.write_text("myfile.txt", "data")

        mock_client.put_object.assert_called_once_with(
            Bucket=_BUCKET, Key="workspace/myfile.txt", Body=b"data"
        )

    def test_s3_prefix_applied_to_read(self):
        backend, mock_client = _build_backend(prefix="workspace")
        mock_client.get_object.return_value = _s3_response(b"prefixed")

        backend.read_text("myfile.txt")

        mock_client.get_object.assert_called_once_with(
            Bucket=_BUCKET, Key="workspace/myfile.txt"
        )


class TestS3UriPathAccepted:
    def test_s3_uri_path_accepted(self):
        """read_text('s3://bucket/myfile.txt') resolves to key 'myfile.txt'."""
        backend, mock_client = _build_backend()
        mock_client.get_object.return_value = _s3_response(b"via uri")

        text = backend.read_text(f"s3://{_BUCKET}/myfile.txt")

        mock_client.get_object.assert_called_once_with(Bucket=_BUCKET, Key="myfile.txt")
        assert text == "via uri"

    def test_s3_uri_with_prefix_in_path(self):
        """s3:// URI with a key that includes a prefix is accepted."""
        backend, mock_client = _build_backend()
        mock_client.get_object.return_value = _s3_response(b"nested")

        backend.read_text(f"s3://{_BUCKET}/some/nested/key.txt")

        mock_client.get_object.assert_called_once_with(
            Bucket=_BUCKET, Key="some/nested/key.txt"
        )


# ---------------------------------------------------------------------------
# Always-run: import-error and identity tests (no moto)
# ---------------------------------------------------------------------------


class TestS3MissingBoto3RaisesImportError:
    def test_s3_missing_boto3_raises_import_error(self):
        """Constructing the backend without boto3 raises ImportError."""
        # Remove any previously injected boto3 from sys.modules
        saved_boto3 = sys.modules.pop("boto3", None)
        # Also evict the cached s3 module so its __init__ re-runs
        sys.modules.pop("agenthicc.tools.fs.s3", None)

        try:
            from agenthicc.tools.fs import s3 as s3_mod

            with pytest.raises(ImportError, match="boto3"):
                s3_mod.S3FilesystemBackend(bucket="x")
        finally:
            # Re-inject boto3 for subsequent tests
            if saved_boto3 is not None:
                sys.modules["boto3"] = saved_boto3
            sys.modules.pop("agenthicc.tools.fs.s3", None)


class TestS3NameIsS3:
    def test_s3_name_is_s3(self):
        backend, _ = _build_backend(bucket="x")
        assert backend.name == "s3"


class TestS3RootProperty:
    def test_s3_root_property(self):
        backend, _ = _build_backend(bucket="test-bucket", prefix="")
        assert backend.root == "s3://test-bucket/"

    def test_s3_root_property_with_prefix(self):
        backend, _ = _build_backend(bucket="test-bucket", prefix="workspace")
        assert backend.root == "s3://test-bucket/workspace"
