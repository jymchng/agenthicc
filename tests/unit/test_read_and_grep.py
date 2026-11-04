"""TOOLR-10: numbered reads, binary detection, and one grep with two backends."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agenthicc.tools.fs import GrepFilesTool, ReadFileTool, SearchFilesTool
from agenthicc.tools.fs.file_cache import configure_file_cache
from agenthicc.tools.fs.search import (
    GrepQuery,
    SearchFile,
    ripgrep_path,
    search_with_python,
    search_with_ripgrep,
)

pytestmark = pytest.mark.unit

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00"
)
needs_rg = pytest.mark.skipif(ripgrep_path() is None, reason="rg is not installed")


@pytest.fixture(autouse=True)
def _no_file_cache() -> None:
    configure_file_cache(None)


def _context(root: Path) -> dict[str, object]:
    return {"workspace_root": str(root)}


async def _read(root: Path, **arguments: object) -> dict[str, object]:
    return await ReadFileTool().execute(arguments, _context(root))


async def _grep(root: Path, **arguments: object) -> dict[str, object]:
    return await GrepFilesTool().execute(arguments, _context(root))


# ── read_file ─────────────────────────────────────────────────────────────────


async def test_a_read_is_numbered_by_default_and_raw_on_request(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\nc\n")

    numbered = await _read(tmp_path, path="f.txt")
    raw = await _read(tmp_path, path="f.txt", line_numbers=False)

    assert numbered["content"] == "1\ta\n2\tb\n3\tc"
    assert numbered["total_lines"] == 3 and str(numbered["version"]).count(":") == 1
    assert raw["content"] == "a\nb\nc\n"


async def test_numbers_share_one_width_and_a_range_starts_at_its_offset(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("".join(f"line {n}\n" for n in range(1, 13)))

    whole = await _read(tmp_path, path="f.txt")
    window = await _read(tmp_path, path="f.txt", offset=9, limit=2)

    assert str(whole["content"]).splitlines()[0] == " 1\tline 1"
    assert window["content"] == " 9\tline 9\n10\tline 10"
    assert window["lines_returned"] == 2 and window["total_lines"] == 12
    assert "truncated" not in window


async def test_a_png_is_binary_with_its_size_and_type(tmp_path: Path) -> None:
    (tmp_path / "pixel.png").write_bytes(_PNG)

    result = await _read(tmp_path, path="pixel.png")

    assert result["ok"] is True and result["binary"] is True
    assert result["size_bytes"] == len(_PNG) and result["mime_type"] == "image/png"
    assert "content" not in result


async def test_undecodable_text_is_binary_unless_an_encoding_is_given(tmp_path: Path) -> None:
    (tmp_path / "latin.txt").write_bytes("café\n".encode("latin-1"))

    detected = await _read(tmp_path, path="latin.txt")
    decoded = await _read(tmp_path, path="latin.txt", encoding="latin-1", line_numbers=False)

    assert detected["binary"] is True
    assert decoded["content"] == "café\n"


async def test_a_bad_range_is_an_invalid_argument(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a\n")

    result = await _read(tmp_path, path="f.txt", offset=0)

    assert result["ok"] is False and result["error_code"] == "invalid_argument"


# ── grep_files ────────────────────────────────────────────────────────────────


def _project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n# TODO: fix\ny = 2\nz = 3\nw = 4\n")
    (root / "src" / "notes.md").write_text("todo in markdown\n")
    (root / "src" / "crlf.py").write_text("first\r\nTodo with crlf\r\nlast\r\n", newline="")
    (root / "src" / "unicode.py").write_text("ÿÿÿ todo after wide chars\n")
    (root / "src" / "latin.py").write_bytes("café todo\n".encode("latin-1"))
    (root / "src" / "data.bin").write_bytes(b"todo\x00binary\n")
    (root / "src" / "dense.py").write_text("".join(f"todo {n}\n" for n in range(30)))


async def test_smart_case_glob_and_context(tmp_path: Path) -> None:
    _project(tmp_path)

    found = await _grep(tmp_path, pattern="todo")
    upper = await _grep(tmp_path, pattern="TODO")
    python_only = await _grep(tmp_path, pattern="todo", glob=["*.py"])
    with_context = await _grep(tmp_path, pattern="TODO", context_lines=2, glob=["app.py"])

    lines = [match["line"] for match in found["matches"]]  # type: ignore[union-attr]
    assert "# TODO: fix" in lines and "todo in markdown" in lines
    assert [match["file"] for match in upper["matches"]] == ["src/app.py"]  # type: ignore[union-attr]
    assert all(str(m["file"]).endswith(".py") for m in python_only["matches"])  # type: ignore[union-attr]
    (match,) = with_context["matches"]  # type: ignore[misc]
    assert match["context_before"] == ["x = 1"] and match["context_after"] == ["y = 2", "z = 3"]


async def test_a_cut_list_still_reports_the_real_totals(tmp_path: Path) -> None:
    _project(tmp_path)

    result = await _grep(tmp_path, pattern="todo", max_results=10)

    assert len(result["matches"]) == 10  # type: ignore[arg-type]
    assert result["total_matches"] == 35 and result["files_matched"] == 6
    assert result["truncated"] is True


async def test_output_modes_and_fixed_strings(tmp_path: Path) -> None:
    _project(tmp_path)

    files = await _grep(tmp_path, pattern="todo", output="files", exclude=["dense.py"])
    counts = await _grep(tmp_path, pattern="todo", output="count")
    literal = await _grep(tmp_path, pattern="x = 1", fixed_string=True)
    regex_text = await _grep(tmp_path, pattern="(", fixed_string=True)

    assert "src/dense.py" not in files["files"]  # type: ignore[operator]
    assert counts["counts"]["src/dense.py"] == 30  # type: ignore[index]
    assert literal["total_matches"] == 1
    assert regex_text["ok"] is True and regex_text["total_matches"] == 0


async def test_a_path_naming_a_file_searches_that_file(tmp_path: Path) -> None:
    _project(tmp_path)

    result = await _grep(tmp_path, pattern="todo", path="src/dense.py", max_results=2)

    assert result["files_searched"] == 1 and result["total_matches"] == 30
    assert [match["line_number"] for match in result["matches"]] == [1, 2]  # type: ignore[union-attr]


async def test_grep_honours_gitignore_defaults(tmp_path: Path) -> None:
    _project(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("todo in a dependency\n")

    result = await _grep(tmp_path, pattern="dependency")

    assert result["total_matches"] == 0


# ── the two backends agree ────────────────────────────────────────────────────


def _files(root: Path) -> list[SearchFile]:
    return [
        SearchFile(str(path.relative_to(root)), path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


@needs_rg
@pytest.mark.parametrize(
    "query",
    [
        GrepQuery.create("todo"),
        GrepQuery.create("TODO"),
        GrepQuery.create("todo", case_sensitive=True),
        GrepQuery.create("todo", context_lines=2),
        GrepQuery.create("todo \\d+", max_results=5),
        GrepQuery.create("x = 1", fixed_string=True),
        GrepQuery.create("todo", output="count"),
        GrepQuery.create("^last$"),
    ],
    ids=lambda query: f"{query.pattern}-{query.case_sensitive}-{query.context_lines}",
)
async def test_ripgrep_returns_what_python_returns(tmp_path: Path, query: GrepQuery) -> None:
    _project(tmp_path)
    files = _files(tmp_path)
    rg = ripgrep_path()
    assert rg is not None

    expected = search_with_python(files, query)
    actual = await search_with_ripgrep(rg, tmp_path, files, query)

    assert actual is not None and actual.backend == "ripgrep"
    assert actual.matches == expected.matches
    assert actual.counts == expected.counts
    assert actual.total_matches == expected.total_matches


@needs_rg
async def test_a_pattern_rg_cannot_compile_falls_back_to_python(tmp_path: Path) -> None:
    _project(tmp_path)

    result = await _grep(tmp_path, pattern="todo(?= in)")

    assert result["backend"] == "python" and result["total_matches"] == 1


async def test_without_rg_python_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path)
    monkeypatch.setattr("agenthicc.tools.fs.search.shutil.which", lambda _name: None)

    result = await _grep(tmp_path, pattern="todo")

    assert result["backend"] == "python" and result["total_matches"] == 35


# ── search_files ──────────────────────────────────────────────────────────────


async def test_search_files_can_list_the_most_recent_first(tmp_path: Path) -> None:
    now = time.time()
    for age, name in [(300, "old.py"), (10, "new.py"), (100, "mid.py")]:
        path = tmp_path / name
        path.write_text("x\n")
        os.utime(path, (now - age, now - age))

    by_path = await SearchFilesTool().execute({"pattern": "*.py"}, _context(tmp_path))
    by_mtime = await SearchFilesTool().execute(
        {"pattern": "*.py", "sort": "mtime"}, _context(tmp_path)
    )

    assert by_path["matches"] == ["mid.py", "new.py", "old.py"]
    assert by_mtime["matches"] == ["new.py", "mid.py", "old.py"]
