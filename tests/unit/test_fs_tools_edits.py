"""TOOLR-01: lossless, exact, atomic and stale-safe file edits."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from agenthicc.tools.fs import PatchFileTool, ReadFileTool, WriteFileTool
from agenthicc.tools.fs.agent_tools import apply_diff, touch_file, truncate_file
from agenthicc.tools.fs.edits import FILE_VERSIONS, EditError, decode_for_edit

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _ctx(root: Path) -> dict[str, object]:
    return {"workspace_root": str(root)}


async def _patch(root: Path, **args: object) -> dict[str, object]:
    return await PatchFileTool().execute(args, _ctx(root))


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        (b"alpha\r\nbeta\r\ngamma\r\n", "utf-8"),
        (b"alpha\nbeta\r\ngamma\n", "utf-8"),
        (b"\xef\xbb\xbfalpha\nbeta\n", "utf-8"),
        ("alpha\nbéta\n".encode("latin-1"), "latin-1"),
    ],
    ids=["crlf", "mixed", "bom", "latin-1"],
)
async def test_an_edit_changes_only_its_own_bytes(
    workspace: Path, data: bytes, encoding: str
) -> None:
    target = workspace / "f.txt"
    target.write_bytes(data)

    result = await _patch(
        workspace, path="f.txt", old_content="alpha", new_content="ALPHA", encoding=encoding
    )

    assert result["ok"] is True, result
    assert target.read_bytes() == data.replace(b"alpha", b"ALPHA")


async def test_an_lf_anchor_matches_a_crlf_file_and_keeps_crlf(workspace: Path) -> None:
    target = workspace / "f.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = await _patch(workspace, path="f.txt", old_content="one\ntwo", new_content="1\n2")

    assert result["ok"] is True
    assert target.read_bytes() == b"1\r\n2\r\nthree\r\n"


async def test_an_undecodable_file_is_refused_and_left_alone(workspace: Path) -> None:
    target = workspace / "f.txt"
    original = b"a\r\nb\xff\r\n"
    target.write_bytes(original)

    result = await _patch(workspace, path="f.txt", old_content="a", new_content="A")

    assert result["ok"] is False and result["error_code"] == "undecodable"
    assert result["offset"] == 4
    assert target.read_bytes() == original


async def test_an_ambiguous_anchor_writes_nothing_unless_replace_all(workspace: Path) -> None:
    target = workspace / "f.py"
    target.write_text("x = 1\nreturn None\ny = 2\nreturn None\n")

    refused = await _patch(
        workspace, path="f.py", old_content="return None", new_content="return 0"
    )
    assert refused["ok"] is False and refused["error_code"] == "ambiguous_match"
    assert refused["occurrences"] == 2 and refused["lines"] == [2, 4]
    assert "return None" in target.read_text()

    allowed = await _patch(
        workspace,
        path="f.py",
        old_content="return None",
        new_content="return 0",
        replace_all=True,
    )
    assert allowed["ok"] is True and allowed["replacements"] == 2
    assert "return None" not in target.read_text()
    assert allowed["diff"].count("+return 0") == 2


async def test_an_executable_stays_executable(workspace: Path) -> None:
    target = workspace / "run.sh"
    target.write_text("echo old\n")
    target.chmod(0o755)

    await _patch(workspace, path="run.sh", old_content="old", new_content="new")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


async def test_a_file_changed_since_it_was_read_is_refused_unless_forced(
    workspace: Path,
) -> None:
    target = workspace / "f.txt"
    target.write_text("first\n")
    read = await ReadFileTool().execute({"path": "f.txt"}, _ctx(workspace))
    assert read["ok"] is True and read["version"]
    await asyncio.sleep(0.01)
    target.write_text("someone else's longer edit\n")

    stale = await _patch(workspace, path="f.txt", old_content="edit", new_content="change")
    assert stale["ok"] is False and stale["error_code"] == "stale_file"
    overwrite = await WriteFileTool().execute(
        {"path": "f.txt", "content": "mine\n"}, _ctx(workspace)
    )
    assert overwrite["error_code"] == "stale_file"
    assert target.read_text() == "someone else's longer edit\n"

    forced = await _patch(
        workspace, path="f.txt", old_content="edit", new_content="change", force=True
    )
    assert forced["ok"] is True
    # The tool's own write is the new baseline: the next edit is not stale.
    again = await _patch(workspace, path="f.txt", old_content="change", new_content="x")
    assert again["ok"] is True


async def test_an_explicit_version_mismatch_is_always_refused(workspace: Path) -> None:
    target = workspace / "f.txt"
    target.write_text("a\n")

    result = await _patch(
        workspace,
        path="f.txt",
        old_content="a",
        new_content="b",
        expected_version="0:0",
        force=True,
    )

    assert result["error_code"] == "stale_file" and result["current_version"]
    assert target.read_text() == "a\n"


async def test_an_interrupted_write_leaves_the_original_and_no_temporary(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = workspace / "f.txt"
    target.write_bytes(b"original\n")

    def failing_replace(source: object, destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    result = await _patch(workspace, path="f.txt", old_content="original", new_content="new")

    assert result["ok"] is False and result["error_code"] == "internal"
    assert target.read_bytes() == b"original\n"
    assert sorted(path.name for path in workspace.iterdir()) == ["f.txt"]


async def test_apply_diff_finds_a_drifted_hunk_and_returns_no_file_copy(workspace: Path) -> None:
    target = workspace / "f.txt"
    body = [f"line {n}" for n in range(1, 31)]
    target.write_bytes(("\r\n".join(body) + "\r\n").encode())
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -3,3 +3,3 @@\n line 10\n-line 11\n+LINE 11\n line 12\n"

    result = await apply_diff(path="f.txt", diff=diff)

    assert result["ok"] is True, result
    assert result["hunks"] == [{"hunk": 1, "line": 10, "offset": 7}]
    assert "result" not in result and "line 1\r\n" not in str(result)
    assert (
        target.read_bytes() == ("\r\n".join(body).replace("line 11", "LINE 11") + "\r\n").encode()
    )


async def test_apply_diff_inserts_after_a_line(workspace: Path) -> None:
    target = workspace / "f.txt"
    target.write_text("a\nb\n")

    result = await apply_diff(path="f.txt", diff="@@ -1,0 +2,1 @@\n+inserted\n")

    assert result["ok"] is True, result
    assert target.read_text() == "a\ninserted\nb\n"


async def test_touch_updates_the_mtime_of_an_existing_file(workspace: Path) -> None:
    target = workspace / "f.txt"
    target.write_text("x")
    os.utime(target, (0, 0))

    result = await touch_file(path="f.txt")

    assert result["ok"] is True and result["created"] is False
    assert target.stat().st_mtime > 0


async def test_truncate_is_atomic_and_pads_when_growing(workspace: Path) -> None:
    target = workspace / "f.bin"
    target.write_bytes(b"abcdef")

    shrunk = await truncate_file(path="f.bin", size=2)
    grown = await truncate_file(path="f.bin", size=4)

    assert shrunk["new_size"] == 2 and grown["new_size"] == 4
    assert target.read_bytes() == b"ab\0\0"


def test_decode_reports_the_newline_convention() -> None:
    assert decode_for_edit(b"a\r\nb\r\n").newline == "\r\n"
    assert decode_for_edit(b"a\nb").newline == "\n"
    assert decode_for_edit(b"a\nb\r\n").newline == "mixed"
    assert decode_for_edit(b"ab").newline == "none"
    with pytest.raises(EditError) as raised:
        decode_for_edit(b"ok", "no-such-encoding")
    assert raised.value.code == "invalid_argument"


def test_versions_are_per_path(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("x")
    FILE_VERSIONS.record(path, "1:1")
    with pytest.raises(EditError):
        FILE_VERSIONS.check(path)
    FILE_VERSIONS.check(path, force=True)
    FILE_VERSIONS.forget(path)
    FILE_VERSIONS.check(path)
