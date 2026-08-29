"""Unit coverage for the generated ``make_book`` PDF builder."""

from __future__ import annotations

import ast
import asyncio

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


def test_slugify_book_title_is_stable_and_safe() -> None:
    assert slugify_book_title("  HTTP/3: A Practical Guide! ") == "http-3-a-practical-guide"
    assert slugify_book_title("---", fallback="untitled") == "untitled"


def test_generated_builder_is_valid_python_and_contains_pipeline() -> None:
    source = build_book_script(title='A book called "Build It"', author="An Author")

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
async def test_mark_book_complete_creates_builder_as_safety_net(tmp_path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = _make_compile_tools(
        event,
        data,
        output_dir=str(tmp_path),
        title="A Technical Book",
        author="Author",
    )

    result = await _named_tool(tools, "mark_book_complete")(
        pdf_path="dist/a-technical-book.pdf",
        summary="Built and validated.",
    )

    assert result["ok"] is True
    assert event.is_set() is True
    assert (tmp_path / "build_book.py").is_file()
    assert data["build_script_path"] == str(tmp_path / "build_book.py")


@pytest.mark.asyncio
async def test_compile_tool_can_place_builder_in_workflow_package(tmp_path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    book_root = tmp_path / "generated-book"
    workflow_dir = tmp_path / "workflow" / "make_book"
    tools = _make_compile_tools(
        event,
        data,
        output_dir=str(book_root),
        builder_dir=str(workflow_dir),
        title="A Technical Book",
        author="Author",
    )

    result = await _named_tool(tools, "create_build_book")()

    assert result["ok"] is True
    destination = workflow_dir / "build_book.py"
    assert destination.is_file()
    assert not (book_root / "build_book.py").exists()
    assert str(book_root.resolve()) in destination.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_create_build_book_requires_a_target_directory() -> None:
    tools = _make_compile_tools(asyncio.Event(), {})

    result = await _named_tool(tools, "create_build_book")()

    assert result["ok"] is False
    assert "output directory" in result["error"]


def test_build_script_path_round_trips_in_checkpoint() -> None:
    context = MakeBookContext(
        intent="write a book",
        state=MakeBookState.COMPLETE,
        build_script_path="books/example/build_book.py",
    )

    payload = MakeBookWorkflow.checkpoint_context_to_payload(context)
    restored = MakeBookWorkflow.checkpoint_context_from_payload(payload)

    assert restored.build_script_path == "books/example/build_book.py"
