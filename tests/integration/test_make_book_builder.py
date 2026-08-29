"""Integration coverage for executing a generated ``build_book.py``."""

from __future__ import annotations

import os
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
for arg in "$@"; do
  case "$arg" in
    -output-directory=*) outdir="${arg#-output-directory=}" ;;
  esac
done
mkdir -p "$outdir"
printf '%%PDF-1.7\\n1 0 obj\\n<<>>\\nendobj\\n' > "$outdir/book.pdf"
: > "$outdir/book.log"
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["BUILD_LOG"] = str(log_path)
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
