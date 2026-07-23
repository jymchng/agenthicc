"""Portable tests for optional and platform-specific adapters."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from agenthicc.tui.cbreak_reader import Key

pytestmark = pytest.mark.unit


class _MemFS:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs = {"/"}

    def mkdir(self, path: str) -> None:
        if path in self.dirs:
            raise RuntimeError("exists")
        self.dirs.add(path.rstrip("/"))

    def _parent(self, path: str) -> None:
        parent = path.rsplit("/", 1)[0] or "/"
        self.dirs.add(parent)

    def writeFile(self, path: str, value: object, options: object = None) -> None:
        self._parent(path)
        if isinstance(value, str):
            self.files[path] = value.encode()
        else:
            self.files[path] = bytes(value)  # type: ignore[arg-type]

    def readFile(self, path: str, options: object = None) -> object:
        if path not in self.files:
            raise FileNotFoundError(path)
        if isinstance(options, dict):
            return self.files[path].decode()
        return self.files[path]

    def unlink(self, path: str) -> None:
        del self.files[path]

    def stat(self, path: str) -> SimpleNamespace:
        if path in self.dirs:
            return SimpleNamespace(mode=0o040755, size=0, mtime=1000, ctime=1000)
        if path in self.files:
            return SimpleNamespace(mode=0o100644, size=len(self.files[path]), mtime=1000, ctime=1000)
        raise FileNotFoundError(path)

    def readdir(self, path: str) -> list[str]:
        names = {".", ".."}
        prefix = path.rstrip("/") + "/"
        for directory in self.dirs:
            if directory.startswith(prefix):
                rest = directory[len(prefix) :].split("/", 1)[0]
                if rest:
                    names.add(rest)
        for file in self.files:
            if file.startswith(prefix):
                rest = file[len(prefix) :].split("/", 1)[0]
                if rest:
                    names.add(rest)
        return sorted(names)


def test_pyodide_backend_full_memory_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    fs = _MemFS()
    pyodide = types.ModuleType("pyodide")
    pyodide_fs = types.ModuleType("pyodide.FS")
    for name in ("mkdir", "writeFile", "readFile", "unlink", "stat", "readdir"):
        setattr(pyodide_fs, name, getattr(fs, name))
    monkeypatch.setitem(sys.modules, "pyodide", pyodide)
    monkeypatch.setitem(sys.modules, "pyodide.FS", pyodide_fs)
    from agenthicc.tools.fs.pyodide import PyodideFilesystemBackend

    backend = PyodideFilesystemBackend()
    assert backend.root == "/workspace"
    assert backend.write_text("src/a.txt", "hello\nWorld") == 11
    assert backend.read_text("src/a.txt") == "hello\nWorld"
    assert backend.read_bytes("src/a.txt") == b"hello\nWorld"
    assert backend.read_lines("src/a.txt", 2) == (["World"], 2)
    assert backend.append_text("src/a.txt", "\nagain") == 17
    backend.write_bytes("src/b.txt", b"needle")
    assert backend.exists("src/b.txt")
    assert backend.stat("src/b.txt").is_file
    assert backend.list_dir("src", recursive=True)
    assert backend.glob("*.txt", "src")
    assert backend.grep("NEEDLE", "src", case_sensitive=False)
    assert backend.batch_read(["src/a.txt", "missing"])[1]["ok"] is False
    assert backend.batch_write([{"path": "src/c.txt", "content": "c"}])[0]["ok"]
    assert backend.batch_delete(["src/c.txt", "missing"])[1]["ok"] is False
    backend.copy("src/a.txt", "src/copy.txt")
    backend.move("src/copy.txt", "src/moved.txt")
    backend.truncate("src/moved.txt", 2)
    backend.make_directory("nested/dir")
    with pytest.raises(PermissionError):
        backend.read_text("../../escape")


def test_windows_decoder_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import agenthicc.tui.terminal.windows_backend as win

    assert win._decode_key_event(win._VK_TAB, "", win._SHIFT_PRESSED) == (Key.SHIFT_TAB, "")
    assert win._decode_key_event(win._VK_TAB, "", 0) == (Key.TAB, "")
    assert win._decode_key_event(win._VK_RETURN, "", win._LEFT_CTRL_PRESSED) == (
        Key.CTRL_ENTER,
        "",
    )
    for vk, expected in win._VK_KEYS.items():
        assert win._decode_key_event(vk, "", 0) == (expected, "")
    for char, expected in (
        ("\x03", Key.CTRL_C),
        ("\x04", Key.CTRL_D),
        ("\x15", Key.CTRL_U),
        ("\x16", Key.CTRL_V),
        ("\x08", Key.BACKSPACE),
        ("\r", Key.ENTER),
        ("\n", Key.CTRL_ENTER),
        ("\t", Key.TAB),
        ("@", Key.AT),
        ("x", Key.CHAR),
    ):
        decoded = win._decode_key_event(0, char, 0)
        assert decoded is not None and decoded[0] is expected
    assert win._decode_key_event(0, "", 0) is None
    backend = win.WindowsBackend()
    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.getwch = lambda: "x"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    assert backend._read_key_getwch() == (Key.CHAR, "x")
    values = iter(["\xe0", "H"])
    fake_msvcrt.getwch = lambda: next(values)  # type: ignore[attr-defined]
    assert backend._read_key_getwch() == (Key.UP, "")
    values = iter(["\x00", "\x0f"])
    fake_msvcrt.getwch = lambda: next(values)  # type: ignore[attr-defined]
    assert backend._read_key_getwch() == (Key.SHIFT_TAB, "")
    assert backend.is_interactive() is False


def test_posix_backend_noninteractive(monkeypatch: pytest.MonkeyPatch) -> None:
    import agenthicc.tui.terminal.posix_backend as posix

    monkeypatch.setattr(posix.sys.stdin, "isatty", lambda: False)
    backend = posix.PosixBackend()
    assert backend.is_interactive() is False
    with backend.enter_raw_mode():
        pass
    backend.restore()


def test_outlook_non_windows_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    import agenthicc.tools.outlook.win32_backend as outlook

    monkeypatch.setattr(outlook, "WIN32_AVAILABLE", False)
    assert outlook.list_emails() == outlook._NOT_WINDOWS
    assert outlook.read_email(1) == outlook._NOT_WINDOWS
    assert outlook.send_email(["x@example.com"], "subject", "body") == outlook._NOT_WINDOWS
    assert outlook.reply_email(1, "body") == outlook._NOT_WINDOWS
    assert outlook.search_emails("query") == outlook._NOT_WINDOWS
    assert outlook.move_email(1, "Archive") == outlook._NOT_WINDOWS
    assert outlook.list_folders() == outlook._NOT_WINDOWS
    assert outlook.calendar_events("2026-01-01", "2026-01-02") == outlook._NOT_WINDOWS
    assert outlook.create_event("x", "2026-01-01", "2026-01-01") == outlook._NOT_WINDOWS
    assert outlook.word_read_document("x.docx") == outlook._NOT_WINDOWS
    assert outlook.excel_read_range("x.xlsx") == outlook._NOT_WINDOWS
    assert outlook.run_vba_macro("macro") == outlook._NOT_WINDOWS


def test_s3_backend_uses_safe_key_and_batch_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    class ClientError(Exception):
        def __init__(self, code: str) -> None:
            self.response = {"Error": {"Code": code}}

    class Paginator:
        def __init__(self, client: "FakeClient") -> None:
            self.client = client

        def paginate(self, **kwargs: object) -> list[dict[str, object]]:
            prefix = str(kwargs.get("Prefix", ""))
            keys = [key for key in self.client.objects if key.startswith(prefix)]
            contents = [{"Key": key, "Size": len(self.client.objects[key])} for key in keys]
            page: dict[str, object] = {"Contents": contents}
            if kwargs.get("Delimiter") == "/":
                page["CommonPrefixes"] = [{"Prefix": prefix + "folder/"}]
            return [page]

    class FakeClient:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {"root/a.txt": b"hello\nneedle"}
            self.calls: list[str] = []

        def get_object(self, **kwargs: object) -> dict[str, object]:
            key = str(kwargs["Key"])
            if key not in self.objects:
                raise ClientError("NoSuchKey")
            return {"Body": SimpleNamespace(read=lambda: self.objects[key])}

        def put_object(self, **kwargs: object) -> None:
            self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])  # type: ignore[arg-type]

        def get_paginator(self, _name: str) -> Paginator:
            return Paginator(self)

        def delete_objects(self, **kwargs: object) -> dict[str, object]:
            deleted = []
            for item in kwargs["Delete"]["Objects"]:  # type: ignore[index]
                key = item["Key"]
                self.objects.pop(key, None)
                deleted.append({"Key": key})
            return {"Deleted": deleted}

        def delete_object(self, **kwargs: object) -> None:
            self.objects.pop(str(kwargs["Key"]), None)

        def copy_object(self, **kwargs: object) -> None:
            source = kwargs["CopySource"]  # type: ignore[assignment]
            self.objects[str(kwargs["Key"])] = self.objects[source["Key"]]  # type: ignore[index]

        def head_object(self, **kwargs: object) -> dict[str, object]:
            key = str(kwargs["Key"])
            if key not in self.objects:
                raise ClientError("404")
            return {
                "ContentLength": len(self.objects[key]),
                "ETag": '"etag"',
                "LastModified": datetime.datetime.now(datetime.timezone.utc),
            }

    import datetime

    client = FakeClient()
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_args, **_kwargs: client  # type: ignore[attr-defined]
    boto3.Session = lambda **_kwargs: SimpleNamespace(client=lambda *_a, **_k: client)  # type: ignore[attr-defined]
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError  # type: ignore[attr-defined]
    botocore.exceptions = exceptions  # type: ignore[attr-defined]
    config = types.ModuleType("botocore.config")
    config.Config = lambda **kwargs: kwargs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "botocore.config", config)
    from agenthicc.tools.fs.s3 import S3FilesystemBackend, _make_path_style_config

    backend = S3FilesystemBackend("bucket", prefix="root", endpoint_url="http://s3")
    assert backend.name == "s3"
    assert backend.root == "s3://bucket/root"
    assert backend._key("s3://bucket/a.txt") == "root/a.txt"
    assert backend._key("s3://other/a.txt") == "root/a.txt"
    with pytest.raises(PermissionError):
        backend.read_text("../escape")
    assert backend.read_text("a.txt").startswith("hello")
    assert backend.read_lines("a.txt", 2) == (["needle"], 2)
    assert backend.write_text("b.txt", "b") == 1
    assert backend.append_text("b.txt", "c") == 1
    backend.truncate("b.txt", 1)
    assert backend.exists("b.txt") is True
    assert backend.exists("missing") is False
    assert backend.stat("b.txt").etag == "etag"
    assert backend.list_dir(".", recursive=False)
    assert backend.glob("*.txt")
    monkeypatch.setattr(backend, "glob", lambda *_args, **_kwargs: ["a.txt"])
    assert backend.grep("needle", case_sensitive=False)
    assert backend.batch_read(["a.txt", "missing"])[1]["ok"] is False
    assert backend.batch_write([{"path": "c.txt", "content": "c"}])[0]["ok"]
    backend.copy("c.txt", "d.txt")
    backend.move("d.txt", "e.txt")
    backend.make_directory("folder")
    assert backend.batch_delete(["c.txt", "e.txt"])
    assert _make_path_style_config() == {"s3": {"addressing_style": "path"}}
