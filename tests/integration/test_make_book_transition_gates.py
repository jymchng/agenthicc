"""Integration coverage for make_book's filesystem-backed phase gates."""

from __future__ import annotations

import asyncio
import json
import zipfile
from types import SimpleNamespace
from pathlib import Path

import pytest

import agenthicc.workflows.make_book.runner as make_book

pytestmark = pytest.mark.integration


def _named(tools: list[object], name: str) -> object:
    return next(tool for tool in tools if getattr(tool, "__name__", "") == name)


def _write_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("book.opf", "<package></package>")


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
    _write_epub(output / "dist" / "book.epub")

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
    assert (await _named(compile_tools, "mark_book_complete")(summary="PDF and EPUB are valid"))[
        "ok"
    ] is True
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


@pytest.mark.asyncio
async def test_resume_recovers_verified_eighth_chapter_without_restarting_earlier_work(
    tmp_path: Path,
) -> None:
    output = tmp_path / "book"
    chapter_dir = output / "chapters"
    chapter_dir.mkdir(parents=True)
    chapters = [
        make_book.ChapterInfo(
            index=index,
            title=f"Chapter {index + 1}",
            file_path=f"chapters/{index + 1:02d}-chapter-{index + 1}.md",
            status="written",
            word_count=100 + index,
        )
        for index in range(7)
    ]
    chapters.append(make_book.ChapterInfo(index=7, title="Chapter 8"))
    chapter_path = make_book._canonical_chapter_path(str(output), 7, "Chapter 8")
    chapter_path.parent.mkdir(parents=True, exist_ok=True)
    chapter_path.write_text("# Chapter 8\n\nVerified recovered content.\n", encoding="utf-8")
    prior_receipts = [
        {"phase": "chapter", "chapter_index": index, "file_path": chapter.file_path}
        for index, chapter in enumerate(chapters[:7])
    ]
    context = make_book.MakeBookContext(
        intent="write the book",
        run_id="same-run-id",
        state=make_book.MakeBookState.FAILED,
        output_dir=str(output),
        assets_dir=str(output / "assets"),
        chapters=chapters,
        current_chapter_index=7,
        fail_reason="chapter 8 never confirmed completion",
        transition_receipts=list(prior_receipts),
        artifacts={f"chapter_{index}": "preserved" for index in range(7)},
    )
    runner = object.__new__(make_book.MakeBookRunner)
    runner._cfg = SimpleNamespace(session_memory=object(), workflow_handle=None)
    phase_calls: list[str] = []

    async def unexpected_phase(**kwargs: object) -> None:
        phase_calls.append(str(kwargs.get("text", "")))
        raise AssertionError("a valid existing chapter should be recovered without an LLM turn")

    async def finish_after_chapters(
        ctx: make_book.MakeBookContext,
        _memory: object,
        *,
        final: bool,
    ) -> make_book.MakeBookState:
        assert final is False
        return make_book.MakeBookState.COMPLETE

    runner.run_phase = unexpected_phase  # type: ignore[method-assign]
    runner._layout_review = finish_after_chapters  # type: ignore[method-assign]

    resumed = await runner.resume(context)

    assert resumed.state is make_book.MakeBookState.COMPLETE
    assert resumed.run_id == "same-run-id"
    assert resumed.current_chapter_index == 8
    assert resumed.chapters[7].status == "written"
    assert resumed.chapters[7].file_path == str(chapter_path.relative_to(output))
    assert [chapter.file_path for chapter in resumed.chapters[:7]] == [
        chapter.file_path for chapter in chapters[:7]
    ]
    assert resumed.transition_receipts[:7] == prior_receipts
    assert len(resumed.transition_receipts) == 8
    assert resumed.transition_receipts[-1]["chapter_index"] == 7
    assert all(resumed.artifacts[f"chapter_{index}"] == "preserved" for index in range(7))
    assert phase_calls == []


@pytest.mark.asyncio
async def test_resume_retries_only_current_chapter_when_artifact_fails_gate(
    tmp_path: Path,
) -> None:
    chapters = [
        make_book.ChapterInfo(
            index=index,
            title=f"Chapter {index + 1}",
            file_path=f"chapters/{index + 1:02d}-chapter-{index + 1}.md",
            status="written",
        )
        for index in range(7)
    ]
    chapters.append(make_book.ChapterInfo(index=7, title="Chapter 8"))
    context = make_book.MakeBookContext(
        intent="write the book",
        run_id="same-run-id",
        state=make_book.MakeBookState.FAILED,
        output_dir=str(tmp_path),
        assets_dir=str(tmp_path / "assets"),
        chapters=chapters,
        current_chapter_index=7,
        fail_reason="chapter 8 never confirmed completion",
    )
    runner = object.__new__(make_book.MakeBookRunner)
    runner._cfg = SimpleNamespace(session_memory=object(), workflow_handle=None)
    prompts: list[str] = []

    async def stop_after_bounded_attempts(**kwargs: object) -> None:
        prompts.append(f"{kwargs.get('text', '')}\n{kwargs.get('system_prompt', '')}")

    runner.run_phase = stop_after_bounded_attempts  # type: ignore[method-assign]

    resumed = await runner.resume(context)

    assert resumed.state is make_book.MakeBookState.FAILED
    assert resumed.fail_reason == "chapter 8 never confirmed completion"
    assert resumed.current_chapter_index == 7
    assert len(prompts) == make_book._MAX_ATTEMPTS
    assert all("chapter 8 of 8" in prompt.lower() for prompt in prompts)
    assert all("chapter 1 of 8" not in prompt.lower() for prompt in prompts)
    assert [chapter.status for chapter in resumed.chapters[:7]] == ["written"] * 7
