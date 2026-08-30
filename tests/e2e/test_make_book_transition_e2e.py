"""End-to-end make_book journey with deterministic filesystem artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.workflows.make_book.runner import MakeBookRunner, MakeBookWorkflow

pytestmark = pytest.mark.e2e


def _runner_config() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_handle=None,
        session_memory=object(),
        cfg=SimpleNamespace(
            execution=SimpleNamespace(effective_usable_budget=lambda: 10_000),
        ),
    )


async def _drive_make_book_turn(output: Path, **kwargs: object) -> None:
    tools = kwargs["tools"]
    assert isinstance(tools, list)
    by_name = {getattr(tool, "__name__", ""): tool for tool in tools}
    if "submit_toc" in by_name:
        prompt = str(kwargs["system_prompt"])
        match = re.search(r"Write a small JSON object to (.+?) with title", prompt)
        assert match is not None
        manifest = Path(match.group(1))
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "title": "E2E Book",
                    "chapters": [{"title": "First Chapter", "outline": "The outline"}],
                    "output_dir": str(output),
                }
            ),
            encoding="utf-8",
        )
        await by_name["submit_toc"](summary="The one-chapter plan is ready.")
    elif "submit_research" in by_name:
        root = output / "research"
        root.mkdir(parents=True, exist_ok=True)
        for name in ("ch01-notes.md", "sources.md", "summary.md"):
            (root / name).write_text("durable research", encoding="utf-8")
        await by_name["submit_research"](summary="Research is complete.")
    elif "confirm_assets_ready" in by_name:
        assets = output / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        for index in range(5):
            (assets / f"diagram-{index}.mmd").write_text("flowchart LR", encoding="utf-8")
        unsplash = assets / "unsplash"
        unsplash.mkdir(parents=True, exist_ok=True)
        (unsplash / "photo.jpg").write_bytes(b"jpeg fixture")
        (unsplash / "manifest.json").write_text(
            json.dumps([{"file": "photo.jpg", "source_url": "https://unsplash.com/photos/free"}]),
            encoding="utf-8",
        )
        await by_name["confirm_assets_ready"](
            summary="Six varied assets, including free Unsplash photography, are ready."
        )
    elif "confirm_chapter_complete" in by_name:
        chapters = output / "chapters"
        chapters.mkdir(parents=True, exist_ok=True)
        (chapters / "01-first-chapter.md").write_text(
            "# First Chapter\n\nContent.", encoding="utf-8"
        )
        await by_name["confirm_chapter_complete"](summary="Chapter is written.")
    elif "confirm_layout_ready" in by_name:
        await by_name["confirm_layout_ready"](
            summary="All images, diagrams, and tables fit the 8.5x11 page bounds."
        )
    elif "confirm_front_matter_ready" in by_name:
        directory = output / "front-matter"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "preface.md").write_text("# Preface", encoding="utf-8")
        await by_name["confirm_front_matter_ready"](summary="Front matter is ready.")
    elif "confirm_back_matter_ready" in by_name:
        directory = output / "back-matter"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.md").write_text("# Index", encoding="utf-8")
        await by_name["confirm_back_matter_ready"](summary="Back matter is ready.")
    elif "mark_book_complete" in by_name:
        await by_name["create_build_book"]()
        dist = output / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "e2e-book.pdf").write_bytes(b"%PDF-1.7\ne2e fixture")
        await by_name["mark_book_complete"](summary="The PDF is compiled and valid.")


@pytest.mark.asyncio
async def test_make_book_end_to_end_records_verified_phase_receipts(tmp_path: Path) -> None:
    runner = object.__new__(MakeBookRunner)
    runner._cfg = _runner_config()
    runner.run_phase = lambda **kwargs: _drive_make_book_turn(  # type: ignore[method-assign]
        tmp_path / "book", **kwargs
    )

    context = await runner.run("write a technical book")

    assert context.state.name == "COMPLETE"
    assert context.pdf_path == "dist/e2e-book.pdf"
    assert context.build_script_path.endswith("build_book.py")
    assert [receipt["phase"] for receipt in context.transition_receipts] == [
        "toc",
        "research",
        "assets",
        "chapter",
        "layout_review",
        "front_matter",
        "back_matter",
        "final_layout_review",
        "compile",
    ]
    assert all(
        receipt["contract_version"] == "make_book.transitions.v2"
        for receipt in context.transition_receipts
    )

    restored = MakeBookWorkflow.checkpoint_context_from_payload(
        MakeBookWorkflow.checkpoint_context_to_payload(context), memory="rehydrated"
    )
    assert restored.shared_memory == "rehydrated"
    assert restored.toc_manifest_path == context.toc_manifest_path
    assert restored.transition_receipts == context.transition_receipts
