"""Integration coverage for make_book's filesystem-backed phase gates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import agenthicc.workflows.make_book.runner as make_book

pytestmark = pytest.mark.integration


def _named(tools: list[object], name: str) -> object:
    return next(tool for tool in tools if getattr(tool, "__name__", "") == name)


@pytest.mark.asyncio
async def test_complete_file_backed_gate_sequence_and_checkpoint(tmp_path: Path) -> None:
    """A complete handoff uses existing artifacts and records every receipt."""

    output = tmp_path / "book"
    (output / "research").mkdir(parents=True)
    (output / "assets" / "unsplash").mkdir(parents=True)
    (output / "chapters").mkdir()
    (output / "front-matter").mkdir()
    (output / "back-matter").mkdir()
    (output / "dist").mkdir()
    (output / "research" / "ch01-notes.md").write_text("Evidence.", encoding="utf-8")
    (output / "research" / "sources.md").write_text("https://source.test", encoding="utf-8")
    (output / "research" / "summary.md").write_text("Summary.", encoding="utf-8")
    for index in range(5):
        (output / "assets" / f"figure-{index}.svg").write_text(
            '<svg width="100" height="100" viewBox="0 0 100 100"></svg>',
            encoding="utf-8",
        )
    (output / "assets" / "unsplash" / "photo.jpg").write_bytes(b"jpeg fixture")
    (output / "assets" / "unsplash" / "manifest.json").write_text(
        json.dumps([{"file": "photo.jpg", "source_url": "https://unsplash.com/photos/free"}]),
        encoding="utf-8",
    )
    (output / "chapters" / "01-chapter.md").write_text(
        "# Chapter\n\nText.\n\n![Figure](../assets/figure-0.svg)\n",
        encoding="utf-8",
    )
    (output / "front-matter" / "preface.md").write_text("# Preface\n", encoding="utf-8")
    (output / "back-matter" / "index.md").write_text("# Index\n", encoding="utf-8")
    (output / "dist" / "book.pdf").write_bytes(b"%PDF-1.7\nfixture")

    receipts: list[dict[str, object]] = []
    event = asyncio.Event()
    data: dict[str, object] = {}
    manifest = output / "toc.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Book",
                "chapters": [{"title": "Chapter", "outline": "Outline"}],
                "output_dir": str(output),
            }
        ),
        encoding="utf-8",
    )
    toc = _named(
        make_book._make_toc_tools(event, data, manifest_path=str(manifest)),
        "submit_toc",
    )
    assert (await toc(summary="Plan is ready"))["ok"] is True
    receipts.append(data["receipt"])

    event.clear()
    research = _named(
        make_book._make_research_tools(event, data, 1, research_dir=str(output / "research")),
        "submit_research",
    )
    assert (await research(summary="Evidence-backed handoff"))["ok"] is True
    receipts.append(data["receipt"])

    event.clear()
    chapter = _named(
        make_book._make_chapter_tools(
            event,
            data,
            output_dir=str(output),
            assets_dir=str(output / "assets"),
            chapter_index=0,
            chapter_title="Chapter",
        ),
        "confirm_chapter_complete",
    )
    assert (await chapter(summary="Chapter is ready"))["ok"] is True
    receipts.append(data["receipt"])

    event.clear()
    assets = _named(
        make_book._make_assets_tools(event, data, assets_dir=str(output / "assets")),
        "confirm_assets_ready",
    )
    assert (await assets(summary="Six varied assets and free Unsplash provenance"))["ok"] is True
    receipts.append(data["receipt"])

    event.clear()
    layout = _named(
        make_book._make_layout_tools(
            event,
            data,
            output_dir=str(output),
            assets_dir=str(output / "assets"),
            phase="layout_review",
        ),
        "confirm_layout_ready",
    )
    assert (await layout(summary="Chapter media and tables fit the page"))["ok"] is True
    receipts.append(data["receipt"])

    event.clear()
    front = _named(
        make_book._make_front_matter_tools(event, data, output_dir=str(output)),
        "confirm_front_matter_ready",
    )
    assert (await front(summary="Front matter is ready"))["ok"] is True
    receipts.append(data["receipt"])

    event.clear()
    back = _named(
        make_book._make_back_matter_tools(event, data, output_dir=str(output)),
        "confirm_back_matter_ready",
    )
    assert (await back(summary="Back matter is ready"))["ok"] is True
    receipts.append(data["receipt"])

    event.clear()
    final_layout = _named(
        make_book._make_layout_tools(
            event,
            data,
            output_dir=str(output),
            assets_dir=str(output / "assets"),
            phase="final_layout_review",
        ),
        "confirm_layout_ready",
    )
    assert (await final_layout(summary="All final media and tables fit the page"))["ok"] is True
    receipts.append(data["receipt"])

    event.clear()
    compile_tools = make_book._make_compile_tools(
        event, data, output_dir=str(output), title="Book", author="Author"
    )
    await _named(compile_tools, "create_build_book")()
    assert (await _named(compile_tools, "mark_book_complete")(summary="PDF is valid"))["ok"] is True
    receipts.append(data["receipt"])

    context = make_book.MakeBookContext(
        intent="book",
        run_id="integration-run",
        state=make_book.MakeBookState.COMPLETE,
        output_dir=str(output),
        toc_manifest_path=str(manifest),
        transition_receipts=receipts,
    )
    payload = make_book.MakeBookWorkflow.checkpoint_context_to_payload(context)
    restored = make_book.MakeBookWorkflow.checkpoint_context_from_payload(
        payload, memory="session-memory"
    )
    assert restored.shared_memory == "session-memory"
    assert restored.toc_manifest_path == str(manifest)
    assert restored.transition_receipts == context.transition_receipts
    assert [receipt["phase"] for receipt in restored.transition_receipts] == [
        "toc",
        "research",
        "chapter",
        "assets",
        "layout_review",
        "front_matter",
        "back_matter",
        "final_layout_review",
        "compile",
    ]
