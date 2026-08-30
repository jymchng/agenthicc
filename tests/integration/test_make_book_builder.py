"""Integration coverage for executing a generated ``build_book.py``."""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.workflows.make_book.builder import write_build_book_script

pytestmark = pytest.mark.integration


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_generated_builder_runs_pandoc_and_xelatex_and_cleans_intermediates(tmp_path) -> None:
    for directory in ("front-matter", "chapters", "back-matter"):
        (tmp_path / directory).mkdir()
    (tmp_path / "front-matter" / "01-preface.md").write_text("# Preface\n", encoding="utf-8")
    (tmp_path / "front-matter" / "contents.md").write_text(
        "# Wrong hand-written TOC\n", encoding="utf-8"
    )
    (tmp_path / "chapters" / "01-introduction.md").write_text(
        "# Introduction\n\nA chapter.\n", encoding="utf-8"
    )
    (tmp_path / "back-matter" / "01-index.md").write_text("# Index\n", encoding="utf-8")
    script = write_build_book_script(tmp_path, title="Integration Book", author="Test Author")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.log"
    _executable(
        bin_dir / "pandoc",
        """#!/bin/sh
set -eu
printf 'pandoc\\n' >> "$BUILD_LOG"
printf '%s\\n' "$*" >> "$ARGS_LOG"
out=''
previous=''
for arg in "$@"; do
  if [ "$previous" = "-o" ]; then out="$arg"; fi
  previous="$arg"
done
cat > "$out" <<'EOF'
\\documentclass{book}
\\begin{document}
\\tableofcontents
\\chapter{Preface}
\\chapter{Introduction}
\\chapter{Index}
\\begin{longtable}{ll}
Alpha & Beta \\\\
\\end{longtable}
\\end{document}
EOF
""",
    )
    _executable(
        bin_dir / "xelatex",
        """#!/bin/sh
set -eu
printf 'xelatex\\n' >> "$BUILD_LOG"
outdir='.'
tex=''
for arg in "$@"; do
  case "$arg" in
    -output-directory=*) outdir="${arg#-output-directory=}" ;;
  esac
  case "$arg" in
    *.tex) tex="$arg" ;;
  esac
done
mkdir -p "$outdir"
if grep -q 'begin{adjustbox}' "$tex"; then printf 'bounded\\n' >> "$BUILD_LOG"; fi
printf '%%PDF-1.7\\n1 0 obj\\n<<>>\\nendobj\\n' > "$outdir/book.pdf"
: > "$outdir/book.log"
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["BUILD_LOG"] = str(log_path)
    environment["ARGS_LOG"] = str(tmp_path / "pandoc-args.log")
    result = subprocess.run(
        [sys.executable, str(script), "--out", "dist/result.pdf"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    output = tmp_path / "dist" / "result.pdf"
    assert output.read_bytes().startswith(b"%PDF")
    assert not (tmp_path / ".build_book").exists()
    commands = log_path.read_text(encoding="utf-8").splitlines()
    assert commands.count("pandoc") == 1
    assert commands.count("xelatex") == 2
    assert "contents.md" not in (tmp_path / "pandoc-args.log").read_text(encoding="utf-8")


@pytest.mark.skipif(
    shutil.which("pandoc") is None or shutil.which("xelatex") is None,
    reason="the real Pandoc/XeLaTeX toolchain is not installed",
)
def test_generated_builder_compiles_real_toc_and_bounded_table(tmp_path) -> None:
    """Exercise the generated LaTeX header against the installed toolchain."""

    for directory in ("front-matter", "chapters", "back-matter"):
        (tmp_path / directory).mkdir()
    (tmp_path / "front-matter" / "preface.md").write_text(
        "# Preface\n\nThis book starts here.\n", encoding="utf-8"
    )
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "figure.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    (tmp_path / "chapters" / "01-introduction.md").write_text(
        "# Introduction\n\n![Figure](../assets/figure.png)\n\n"
        "| Key | Description |\n| --- | --- |\n| A | A short value |\n",
        encoding="utf-8",
    )
    (tmp_path / "back-matter" / "index.md").write_text("# Index\n", encoding="utf-8")
    script = write_build_book_script(tmp_path, title="Real Build", author="Test Author")

    result = subprocess.run(
        [sys.executable, str(script), "--out", "dist/real.pdf"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (tmp_path / "dist" / "real.pdf").read_bytes().startswith(b"%PDF-")
    assert not (tmp_path / ".build_book").exists()
