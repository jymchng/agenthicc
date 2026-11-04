"""TOOLR-10: grep_files over TUIR-00's 33,000-file project finishes in under 1 s with rg."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenthicc.tools.fs import GrepFilesTool
from agenthicc.tools.fs.search import ripgrep_path

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(ripgrep_path() is None, reason="rg is not installed"),
]

#: Budget for one whole-project search with rg.
GREP_BUDGET_S = 1.0


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("grep-project")
    (root / ".gitignore").write_text("node_modules/\n")
    made = 0
    for top in ("src", "node_modules", "tests", "assets"):
        for a in range(30):
            for b in range(30):
                directory = root / top / f"pkg{a}" / f"mod{b}"
                directory.mkdir(parents=True, exist_ok=True)
                for c in range(10):
                    (directory / f"file{c}.py").write_text(
                        f"VALUE = {c}\ndef handler_{a}_{b}_{c}():\n    return VALUE\n"
                    )
                    made += 1
                    if made >= 33_000:
                        yield root
                        return
    yield root


async def test_a_whole_project_search_with_rg_is_under_a_second(project: Path) -> None:
    from agenthicc.tools.fs.file_cache import configure_file_cache

    configure_file_cache(None)
    context = {"workspace_root": str(project)}
    await GrepFilesTool().execute({"pattern": "handler_0_0_0"}, context)  # warm the page cache

    started = time.perf_counter()
    result = await GrepFilesTool().execute({"pattern": "handler_1[0-9]_3_"}, context)
    elapsed = time.perf_counter() - started

    assert result["ok"] is True and result["backend"] == "ripgrep"
    assert result["total_matches"] == 300  # assets/, src/ and tests/, not node_modules/
    assert "searched the first 20000 files" in str(result["note"])  # the discovery cap is reported
    assert elapsed < GREP_BUDGET_S, f"took {elapsed:.2f} s"
