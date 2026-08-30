"""Unit tests for make_book's clean, summary-only transition contract."""

from __future__ import annotations

import asyncio
import json
import inspect
import zipfile
from pathlib import Path

import pytest

import agenthicc.workflows.make_book.runner as make_book

pytestmark = pytest.mark.unit


def _tool(tools: list[object], name: str) -> object:
    return next(candidate for candidate in tools if getattr(candidate, "__name__", "") == name)


def _schema(tool: object) -> dict[str, object]:
    metadata = getattr(tool, "__lauren_ai_tool__")
    return metadata.parameters["input_schema"]


def _write_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("book.opf", "<package></package>")


def test_layout_budget_matches_builder_page_geometry() -> None:
    assert make_book._PAGE_WIDTH_IN == 8.0
    assert make_book._PAGE_HEIGHT_IN == 11.5
    assert make_book._PAGE_MARGIN_IN == 0.75
    assert make_book._CONTENT_WIDTH_IN == 6.5
    assert make_book._TARGET_MEDIA_WIDTH_IN == 6.175
    assert make_book._MAX_MEDIA_HEIGHT_IN == 8.05
    assert make_book._parse_layout_measurement("95%", axis="width") == 6.175
    assert make_book._parse_layout_measurement("70%", axis="height") == 8.05


def test_every_phase_transition_requires_only_a_summary(tmp_path: Path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    transition_tools = [
        _tool(
            make_book._make_toc_tools(event, data, manifest_path=str(tmp_path / "toc.json")),
            "submit_toc",
        ),
        _tool(
            make_book._make_research_tools(event, data, 1, research_dir=str(tmp_path / "research")),
            "submit_research",
        ),
        _tool(
            make_book._make_chapter_tools(
                event,
                data,
                output_dir=str(tmp_path),
                assets_dir=str(tmp_path / "assets"),
                chapter_index=0,
                chapter_title="Chapter",
            ),
            "confirm_chapter_complete",
        ),
        _tool(
            make_book._make_assets_tools(event, data, assets_dir=str(tmp_path / "assets")),
            "confirm_assets_ready",
        ),
        _tool(
            make_book._make_front_matter_tools(event, data, output_dir=str(tmp_path)),
            "confirm_front_matter_ready",
        ),
        _tool(
            make_book._make_back_matter_tools(event, data, output_dir=str(tmp_path)),
            "confirm_back_matter_ready",
        ),
        _tool(
            make_book._make_layout_tools(
                event,
                data,
                output_dir=str(tmp_path),
                assets_dir=str(tmp_path / "assets"),
                phase="layout_review",
            ),
            "confirm_layout_ready",
        ),
        _tool(
            make_book._make_layout_tools(
                event,
                data,
                output_dir=str(tmp_path),
                assets_dir=str(tmp_path / "assets"),
                phase="final_layout_review",
            ),
            "confirm_layout_ready",
        ),
    ]
    compile_tools = make_book._make_compile_tools(event, data, output_dir=str(tmp_path))
    transition_tools.extend(
        [
            _tool(compile_tools, "mark_book_complete"),
            _tool(compile_tools, "reject_book"),
        ]
    )

    for transition in transition_tools:
        assert set(inspect.signature(transition).parameters) == {"summary"}
        schema = _schema(transition)
        assert set(schema["properties"]) == {"summary"}
        assert schema["required"] == ["summary"]

    builder = _tool(compile_tools, "create_build_book")
    assert inspect.signature(builder).parameters == {}
    assert _schema(builder)["properties"] == {}


@pytest.mark.asyncio
async def test_toc_gate_reads_existing_manifest_and_never_creates_it(tmp_path: Path) -> None:
    manifest = tmp_path / "toc.json"
    event = asyncio.Event()
    data: dict[str, object] = {}
    submit = _tool(
        make_book._make_toc_tools(event, data, manifest_path=str(manifest)),
        "submit_toc",
    )

    missing = await submit(summary="plan")
    assert missing["ok"] is False
    assert not manifest.exists()
    assert data == {}

    manifest.write_text(
        json.dumps(
            {
                "title": "Book",
                "chapters": [{"title": "Chapter", "outline": "Outline"}],
                "output_dir": str(tmp_path / "book"),
            }
        ),
        encoding="utf-8",
    )
    accepted = await submit(summary="A concise plan")
    assert accepted["ok"] is True
    assert data["title"] == "Book"
    assert data["toc_manifest_path"] == str(manifest.resolve())
    assert event.is_set() is True


@pytest.mark.asyncio
async def test_research_gate_requires_existing_note_for_each_chapter(tmp_path: Path) -> None:
    root = tmp_path / "research"
    root.mkdir()
    (root / "ch01-notes.md").write_text("one", encoding="utf-8")
    event = asyncio.Event()
    data: dict[str, object] = {}
    submit = _tool(
        make_book._make_research_tools(event, data, 2, research_dir=str(root)),
        "submit_research",
    )

    missing = await submit(summary="handoff")
    assert missing["ok"] is False
    assert event.is_set() is False
    assert data == {}

    (root / "ch02-notes.md").write_text("two", encoding="utf-8")
    (root / "sources.md").write_text("https://source.test", encoding="utf-8")
    accepted = await submit(summary="handoff")
    assert accepted["ok"] is True
    assert data["files"] == [
        "ch01-notes.md",
        "ch02-notes.md",
        "sources.md",
    ]


@pytest.mark.asyncio
async def test_chapter_gate_derives_canonical_path_count_and_assets(tmp_path: Path) -> None:
    chapter_dir = tmp_path / "chapters"
    assets_dir = tmp_path / "assets"
    chapter_dir.mkdir()
    assets_dir.mkdir()
    (assets_dir / "figure.svg").write_text("<svg />", encoding="utf-8")
    (chapter_dir / "01-recovery.md").write_text(
        "# Recovery\n\nThe system uses a figure.\n\n![Figure](../assets/figure.svg)\n",
        encoding="utf-8",
    )
    event = asyncio.Event()
    data: dict[str, object] = {}
    confirm = _tool(
        make_book._make_chapter_tools(
            event,
            data,
            output_dir=str(tmp_path),
            assets_dir=str(assets_dir),
            chapter_index=0,
            chapter_title="Recovery",
        ),
        "confirm_chapter_complete",
    )

    result = await confirm(summary="Chapter is written and checked")

    assert result["ok"] is True
    assert data["chapter_index"] == 0
    assert data["file_path"] == "chapters/01-recovery.md"
    assert data["word_count"] == 10
    assert data["assets"] == ["assets/figure.svg"]
    assert data["receipt"]["contract_version"] == "make_book.transitions.v2"
    assert event.is_set() is True


@pytest.mark.asyncio
async def test_chapter_gate_rejects_missing_artifact_without_mutation(tmp_path: Path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    confirm = _tool(
        make_book._make_chapter_tools(
            event,
            data,
            output_dir=str(tmp_path),
            assets_dir=str(tmp_path / "assets"),
            chapter_index=0,
            chapter_title="Chapter",
        ),
        "confirm_chapter_complete",
    )

    result = await confirm(summary="done")

    assert result["ok"] is False
    assert event.is_set() is False
    assert data == {}


@pytest.mark.asyncio
async def test_asset_gate_requires_many_files_free_unsplash_and_manifest(
    tmp_path: Path,
) -> None:
    assets_dir = tmp_path / "assets"
    event = asyncio.Event()
    data: dict[str, object] = {}
    confirm = _tool(
        make_book._make_assets_tools(
            event,
            data,
            assets_dir=str(assets_dir),
            minimum_assets=6,
        ),
        "confirm_assets_ready",
    )

    missing = await confirm(summary="assets")
    assert missing["ok"] is False
    assert not assets_dir.exists()

    assets_dir.mkdir()
    for index in range(5):
        (assets_dir / f"figure-{index}.svg").write_text("<svg />", encoding="utf-8")
    unsplash = assets_dir / "unsplash"
    unsplash.mkdir()
    (unsplash / "photo.jpg").write_bytes(b"jpeg fixture")
    (unsplash / "manifest.json").write_text(
        json.dumps([{"file": "photo.jpg", "source_url": "https://unsplash.com/photos/free"}]),
        encoding="utf-8",
    )

    accepted = await confirm(summary="Six varied assets are ready")
    assert accepted["ok"] is True
    assert data["unsplash_manifest"] == "assets/unsplash/manifest.json"
    assert len(data["assets"]) == 6


@pytest.mark.asyncio
async def test_asset_gate_rejects_unsplash_plus_provenance(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    root.mkdir()
    for index in range(6):
        (root / f"asset-{index}.svg").write_text("<svg />", encoding="utf-8")
    unsplash = root / "unsplash"
    unsplash.mkdir()
    (unsplash / "photo.jpg").write_bytes(b"fixture")
    (unsplash / "manifest.json").write_text(
        '{"source_url": "https://plus.unsplash.com/paid"}', encoding="utf-8"
    )
    event = asyncio.Event()
    data: dict[str, object] = {}
    confirm = _tool(
        make_book._make_assets_tools(event, data, assets_dir=str(root)),
        "confirm_assets_ready",
    )

    result = await confirm(summary="assets")
    assert result["ok"] is False
    assert event.is_set() is False
    assert data == {}


@pytest.mark.asyncio
async def test_front_and_back_matter_gates_scan_existing_markdown(tmp_path: Path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    front = _tool(
        make_book._make_front_matter_tools(event, data, output_dir=str(tmp_path)),
        "confirm_front_matter_ready",
    )
    back = _tool(
        make_book._make_back_matter_tools(event, data, output_dir=str(tmp_path)),
        "confirm_back_matter_ready",
    )

    assert (await front(summary="missing"))["ok"] is False
    (tmp_path / "front-matter").mkdir()
    (tmp_path / "front-matter" / "preface.md").write_text("# Preface", encoding="utf-8")
    (tmp_path / "front-matter" / "contents.md").write_text("# Contents", encoding="utf-8")
    front_rejected = await front(summary="Do not accept a hand-written TOC")
    assert front_rejected["ok"] is False
    assert event.is_set() is False
    (tmp_path / "front-matter" / "contents.md").unlink()
    assert (await front(summary="Preface ready"))["ok"] is True
    event.clear()
    (tmp_path / "back-matter").mkdir()
    (tmp_path / "back-matter" / "index.md").write_text("# Index", encoding="utf-8")
    (tmp_path / "back-matter" / "contents.md").write_text("# Contents", encoding="utf-8")
    back_rejected = await back(summary="Do not accept a hand-written TOC")
    assert back_rejected["ok"] is False
    assert event.is_set() is False
    (tmp_path / "back-matter" / "contents.md").unlink()
    assert (await back(summary="Index ready"))["ok"] is True


def _png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


@pytest.mark.asyncio
async def test_layout_gate_enforces_image_and_table_bounds_without_writing(
    tmp_path: Path,
) -> None:
    assets = tmp_path / "assets"
    chapters = tmp_path / "chapters"
    assets.mkdir()
    chapters.mkdir()
    image = assets / "figure.png"
    image.write_bytes(_png_header(700, 700))
    chapter = chapters / "01-layout.md"
    chapter.write_text(
        "# Layout\n\n![Figure](../assets/figure.png){width=6.2in height=8.1in}\n",
        encoding="utf-8",
    )
    event = asyncio.Event()
    data: dict[str, object] = {}
    confirm = _tool(
        make_book._make_layout_tools(
            event,
            data,
            output_dir=str(tmp_path),
            assets_dir=str(assets),
            phase="layout_review",
        ),
        "confirm_layout_ready",
    )

    oversized = await confirm(summary="Check the chapter layout")
    assert oversized["ok"] is False
    assert event.is_set() is False
    assert data == {}

    chapter.write_text(
        "# Layout\n\n![Figure](../assets/figure.png){width=6.175in height=8.05in}\n",
        encoding="utf-8",
    )
    accepted = await confirm(summary="The image is bounded")
    assert accepted["ok"] is True
    assert data["image_count"] == 1
    assert data["table_count"] == 0
    assert data["receipt"]["max_width_in"] == 6.175
    assert data["receipt"]["max_height_in"] == 8.05

    event.clear()
    data.clear()
    rows = "\n".join(f"| {index} | {'x' * 110} |" for index in range(28))
    chapter.write_text(
        f"# Layout\n\n| Key | Description |\n| --- | --- |\n{rows}\n",
        encoding="utf-8",
    )
    too_tall = await confirm(summary="Check the table layout")
    assert too_tall["ok"] is False
    assert event.is_set() is False
    assert data == {}


def test_make_book_prompts_define_generated_toc_and_layout_phases() -> None:
    phases = {phase.name: phase for phase in make_book.MakeBookWorkflow.phases}
    assert "contents.md" in phases["front_matter"].system_prompt_override
    assert "contents.md" in phases["back_matter"].system_prompt_override
    assert "6.175in" in phases["layout_review"].system_prompt_override
    assert "8.05in" in phases["final_layout_review"].system_prompt_override
    assert "600-DPI REQUIREMENT" in phases["assets"].system_prompt_override
    assert "savefig(..., dpi=600)" in phases["assets"].system_prompt_override
    assert "mermaid-cli PNG output with Pillow" in phases["assets"].system_prompt_override
    assert len(make_book.MakeBookWorkflow.phases) == 9


@pytest.mark.asyncio
async def test_compile_gate_verifies_existing_builder_pdf_and_epub(tmp_path: Path) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = make_book._make_compile_tools(event, data, output_dir=str(tmp_path), title="Book")
    complete = _tool(tools, "mark_book_complete")
    create_builder = _tool(tools, "create_build_book")

    missing = await complete(summary="compiled")
    assert missing["ok"] is False
    assert not (tmp_path / "dist").exists()

    await create_builder()
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "book.pdf").write_bytes(b"not a PDF")
    invalid = await complete(summary="compiled")
    assert invalid["ok"] is False
    assert event.is_set() is False

    (tmp_path / "dist" / "book.pdf").write_bytes(b"%PDF-1.7\nfixture")
    missing_epub = await complete(summary="compiled")
    assert missing_epub["ok"] is False
    assert "EPUB" in missing_epub["error"]

    _write_epub(tmp_path / "dist" / "book.epub")
    accepted = await complete(summary="PDF compiled and validated")
    assert accepted["ok"] is True
    assert data["pdf_path"] == "dist/book.pdf"
    assert data["epub_path"] == "dist/book.epub"


@pytest.mark.asyncio
async def test_reject_gate_requires_only_summary_and_rejects_whole_book() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    reject = _tool(make_book._make_compile_tools(event, data), "reject_book")

    assert (await reject(summary=""))["ok"] is False
    accepted = await reject(summary="The PDF source needs correction")
    assert accepted["ok"] is True
    assert data["action"] == "reject"
    assert data["chapter_index"] == -1
    assert event.is_set() is True


def test_checkpoint_round_trip_preserves_manifest_and_bounded_receipts() -> None:
    context = make_book.MakeBookContext(
        intent="book",
        run_id="run-1",
        state=make_book.MakeBookState.RESEARCH,
        toc_manifest_path="/tmp/run-1/toc.json",
        transition_receipts=[
            {
                "contract_version": "make_book.transitions.v2",
                "phase": "toc",
                "file_path": "Book/chapters/01.md",
            }
        ],
    )

    payload = make_book.MakeBookWorkflow.checkpoint_context_to_payload(context)
    restored = make_book.MakeBookWorkflow.checkpoint_context_from_payload(payload)

    assert restored.toc_manifest_path == context.toc_manifest_path
    assert restored.transition_receipts == context.transition_receipts
    assert "shared_memory" not in json.dumps(payload)
