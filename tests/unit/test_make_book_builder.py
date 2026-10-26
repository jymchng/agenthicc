"""Unit coverage for the generated ``make_book`` PDF builder."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import pytest

from agenthicc.workflows.make_book.builder import (
    build_book_script,
    slugify_book_title,
    write_build_book_script,
)
from agenthicc.workflows.make_book.runner import (
    MakeBookContext,
    MakeBookState,
    MakeBookWorkflow,
    _make_compile_tools,
)

pytestmark = pytest.mark.unit


def _named_tool(tools: list[object], name: str) -> object:
    return next(tool for tool in tools if getattr(tool, "__name__", "") == name)


def _write_epub(path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("book.opf", "<package></package>")


def _load_bundled_builder():
    path = Path(__file__).parents[2] / "src/agenthicc/workflows/make_book/build_book.py"
    spec = importlib.util.spec_from_file_location("bundled_make_book_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_slugify_book_title_is_stable_and_safe() -> None:
    assert slugify_book_title("  HTTP/3: A Practical Guide! ") == "http-3-a-practical-guide"
    assert slugify_book_title("---", fallback="untitled") == "untitled"


def test_generated_builder_is_valid_python_and_contains_pipeline() -> None:
    source = build_book_script(
        title='A book called "Build It" with a \'triple\' quote """',
        author="An Author",
    )

    ast.parse(source)
    assert "A book called" in source
    assert "Build It" in source
    assert "front-matter" in source
    assert "chapters" in source
    assert "back-matter" in source
    assert '"pandoc",' in source
    assert '"xelatex",' in source
    assert "--keep-intermediates" in source
    assert "attach_cover" in source
    assert "TRIM_W_IN = 8.0" in source
    assert "TRIM_H_IN = 11.5" in source
    assert "MARGIN_IN = 0.75" in source
    assert "CONTENT_W_IN = TRIM_W_IN - (2 * MARGIN_IN)" in source
    assert "IMAGE_TARGET_W_IN = CONTENT_W_IN * IMAGE_WIDTH_FRACTION" in source
    assert "IMAGE_DPI = 600" in source
    assert "paperwidth=8.0in,paperheight=11.5in,margin=0.75in" in source
    assert r"0.95\textwidth" in source
    assert "dpi=(IMAGE_DPI, IMAGE_DPI)" in source
    assert "normalize_raster_images" in source
    assert "build_epub" in source
    assert "--split-level=1" in source
    assert 'output.with_suffix(".epub")' in source
    assert "maxheight" in source
    assert "contents.md" in source
    assert "--toc" in source


def test_default_output_is_timestamped_unique_and_collision_safe(tmp_path: Path) -> None:
    script = write_build_book_script(tmp_path, title="Timestamped Book", author="Author")
    spec = importlib.util.spec_from_file_location("timestamped_book_builder", script)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    with patch.object(builder, "datetime") as clock:
        clock.now.return_value.strftime.return_value = "20261025-120000-123456"
        first = builder.default_output()
        assert first.parent == tmp_path / "dist"
        assert first.name == "timestamped-book 20261025-120000-123456.pdf"
        first.parent.mkdir(parents=True)
        first.write_bytes(b"previous PDF")
        first.with_suffix(".epub").write_bytes(b"previous EPUB")

        second = builder.default_output()

    assert second == first.with_name("timestamped-book 20261025-120000-123456-1.pdf")
    assert first.read_bytes() == b"previous PDF"
    assert first.with_suffix(".epub").read_bytes() == b"previous EPUB"


def test_write_build_book_script_is_executable_and_refreshable(tmp_path) -> None:
    destination = write_build_book_script(tmp_path, title="First Edition", author="Author")

    assert destination == tmp_path / "build_book.py"
    assert destination.is_file()
    assert destination.stat().st_mode & 0o111
    assert "First Edition" in destination.read_text(encoding="utf-8")

    write_build_book_script(tmp_path, title="Second Edition", author="Another Author")
    refreshed = destination.read_text(encoding="utf-8")
    assert "Second Edition" in refreshed
    assert "First Edition" not in refreshed


@pytest.mark.asyncio
async def test_create_build_book_tool_writes_builder_without_advancing_phase(tmp_path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = _make_compile_tools(
        event,
        data,
        output_dir=str(tmp_path),
        title="A Technical Book",
        author="Author",
    )

    result = await _named_tool(tools, "create_build_book")()

    assert result["ok"] is True
    assert event.is_set() is False
    path = tmp_path / "build_book.py"
    assert path.is_file()
    assert data["build_script_path"] == str(path)


@pytest.mark.asyncio
async def test_mark_book_complete_verifies_existing_pdf_and_epub(tmp_path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = _make_compile_tools(
        event,
        data,
        output_dir=str(tmp_path),
        title="A Technical Book",
        author="Author",
    )

    dist = tmp_path / "dist"
    dist.mkdir()
    old_pdf = dist / "previous-edition.pdf"
    old_epub = dist / "previous-edition.epub"
    old_pdf.write_bytes(b"%PDF-1.7\nold edition\n")
    _write_epub(old_epub)
    await _named_tool(tools, "create_build_book")()
    ignored_old = await _named_tool(tools, "mark_book_complete")(
        summary="A previous build is present."
    )
    assert ignored_old["ok"] is False
    assert "new PDF" in ignored_old["error"]

    pdf = dist / "a-technical-book 20261025-120000-123456.pdf"
    pdf.write_bytes(b"%PDF-1.7\nvalid test fixture\n")
    _write_epub(pdf.with_suffix(".epub"))
    result = await _named_tool(tools, "mark_book_complete")(
        summary="The new timestamped PDF and EPUB were compiled and validated."
    )

    assert result["ok"] is True
    assert event.is_set() is True
    assert (tmp_path / "build_book.py").is_file()
    assert data["build_script_path"] == str(tmp_path / "build_book.py")
    assert data["pdf_path"] == "dist/a-technical-book 20261025-120000-123456.pdf"
    assert data["epub_path"] == "dist/a-technical-book 20261025-120000-123456.epub"
    assert old_pdf.read_bytes() == b"%PDF-1.7\nold edition\n"
    assert old_epub.is_file()


@pytest.mark.asyncio
async def test_mark_book_complete_rejects_an_unmatched_epub(tmp_path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = _make_compile_tools(event, data, output_dir=str(tmp_path))
    await _named_tool(tools, "create_build_book")()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "book.pdf").write_bytes(b"%PDF-1.7\nvalid test fixture\n")
    _write_epub(dist / "different-book.epub")

    result = await _named_tool(tools, "mark_book_complete")(summary="Compiled outputs")

    assert result["ok"] is False
    assert "does not match" in result["error"]
    assert event.is_set() is False


@pytest.mark.parametrize("mutation", ["removed", "overwritten"])
@pytest.mark.asyncio
async def test_mark_book_complete_rejects_changes_to_preexisting_outputs(
    tmp_path: Path, mutation: str
) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = _make_compile_tools(event, data, output_dir=str(tmp_path))
    dist = tmp_path / "dist"
    dist.mkdir()
    old_pdf = dist / "previous.pdf"
    old_epub = dist / "previous.epub"
    old_pdf.write_bytes(b"%PDF-1.7\nold")
    _write_epub(old_epub)
    await _named_tool(tools, "create_build_book")()

    if mutation == "removed":
        old_epub.unlink()
    else:
        old_pdf.write_bytes(b"%PDF-1.7\nchanged")
    new_pdf = dist / "new-build 20261025-120000-123456.pdf"
    new_pdf.write_bytes(b"%PDF-1.7\nnew")
    _write_epub(new_pdf.with_suffix(".epub"))

    result = await _named_tool(tools, "mark_book_complete")(summary="New build finished")

    assert result["ok"] is False
    assert "pre-existing dist outputs" in result["error"]
    assert event.is_set() is False
    assert "build_script_path" in data
    assert "pdf_path" not in data
    assert "epub_path" not in data


@pytest.mark.asyncio
async def test_compile_tool_places_builder_in_book_output_directory(tmp_path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    book_root = tmp_path / "generated-book"
    tools = _make_compile_tools(
        event,
        data,
        output_dir=str(book_root),
        title="A Technical Book",
        author="Author",
    )

    result = await _named_tool(tools, "create_build_book")()

    assert result["ok"] is True
    destination = book_root / "build_book.py"
    assert destination.is_file()
    assert str(book_root.resolve()) in destination.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_create_build_book_requires_a_target_directory() -> None:
    tools = _make_compile_tools(asyncio.Event(), {})

    result = await _named_tool(tools, "create_build_book")()

    assert result["ok"] is False
    assert "output directory" in result["error"]


@pytest.mark.asyncio
async def test_compile_tools_expose_bounded_fit_for_hyrox_builder_reference() -> None:
    tools = _make_compile_tools(asyncio.Event(), {})

    listed = await _named_tool(tools, "list_build_book_reference")()
    assert listed["ok"] is True
    assert str(listed["path"]).endswith("workflows/make_book/build_book.py")
    assert int(listed["line_count"]) > 300
    assert any("build_epub" in str(section) for section in listed["sections"])
    assert "Fit-for-HYROX" in listed["message"]

    excerpt = await _named_tool(tools, "read_build_book_reference")(
        start_line=150,
        end_line=220,
    )
    assert excerpt["ok"] is True
    assert "build_epub" in excerpt["content"]
    too_large = await _named_tool(tools, "read_build_book_reference")(
        start_line=1,
        end_line=201,
    )
    assert too_large["ok"] is False


def test_bundled_fit_for_hyrox_reference_builds_a_valid_epub(tmp_path, monkeypatch) -> None:
    builder = _load_bundled_builder()
    build_dir = tmp_path / ".build"
    build_dir.mkdir()
    output = tmp_path / "dist" / "book.epub"
    output.parent.mkdir()
    commands: list[list[object]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        target = Path(command[command.index("-o") + 1])
        _write_epub(target)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder, "run", fake_run)
    source = tmp_path / "chapters" / "01-introduction.md"
    source.parent.mkdir()
    source.write_text("# Introduction\n", encoding="utf-8")

    assert builder.build_epub([source], output, build_dir) is True
    assert output.is_file()
    assert (
        (build_dir / "epub.css")
        .read_text(encoding="utf-8")
        .startswith("/* Generated by build_book.py")
    )
    assert len(commands) == 1
    assert "--split-level=1" in commands[0]
    with zipfile.ZipFile(output) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"


def test_build_script_path_round_trips_in_checkpoint() -> None:
    context = MakeBookContext(
        intent="write a book",
        state=MakeBookState.COMPLETE,
        build_script_path="books/example/build_book.py",
        layout_summary="Chapter layout is bounded.",
        layout_files=["chapters/01.md"],
        layout_image_count=2,
        layout_table_count=1,
        final_layout_summary="Final layout is bounded.",
        epub_path="dist/example.epub",
    )

    payload = MakeBookWorkflow.checkpoint_context_to_payload(context)
    restored = MakeBookWorkflow.checkpoint_context_from_payload(payload)

    assert restored.build_script_path == "books/example/build_book.py"
    assert restored.epub_path == "dist/example.epub"
    assert restored.layout_summary == "Chapter layout is bounded."
    assert restored.layout_files == ["chapters/01.md"]
    assert restored.layout_image_count == 2
    assert restored.layout_table_count == 1
    assert restored.final_layout_summary == "Final layout is bounded."
