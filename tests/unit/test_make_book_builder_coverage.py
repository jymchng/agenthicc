"""Coverage for the generated make-book build helper's safe branches."""

from __future__ import annotations

import sys
import zipfile
import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.workflows.make_book import build_book


def test_builder_header_postprocess_and_dist_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_subprocess(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_book.subprocess, "run", fake_subprocess)
    assert build_book.run(["echo", "builder"]).returncode == 0
    assert calls == [["echo", "builder"]]
    header = tmp_path / "header.tex"
    build_book.write_header_tex(header)
    assert "paperwidth=6.0in" in header.read_text(encoding="utf-8")
    assert build_book.dist_name().endswith(".pdf")
    assert build_book.dist_name(".epub").endswith(".epub")

    tex = tmp_path / "book.tex"
    tex.write_text(
        "\\maketitle\n\\frontmatter\n\\tableofcontents\n"
        "\\chapter{Contents}\\label{contents}\\n\\n\\newpage\\n\\n"
        "\\chapter{Preface}\n",
        encoding="utf-8",
    )
    build_book.postprocess_tex(tex)
    text = tex.read_text(encoding="utf-8")
    assert "\\maketitle" not in text
    assert "\\mainmatter" in text
    assert "\\chapter{Contents}" not in text

    without_frontmatter = tmp_path / "without-frontmatter.tex"
    without_frontmatter.write_text("\\tableofcontents\n", encoding="utf-8")
    build_book.postprocess_tex(without_frontmatter)
    assert "\\clearpage\n\\tableofcontents" in without_frontmatter.read_text(encoding="utf-8")

    monkeypatch.setattr(build_book, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    assert build_book.build_epub([], tmp_path / "book.epub", tmp_path) is False


def test_builder_validates_epub_and_handles_cover_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "book.epub"

    def make_epub(cmd: list[object], **_kwargs: object) -> SimpleNamespace:
        target = Path(str(cmd[cmd.index("-o") + 1]))
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/content.opf", "metadata")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_book, "run", make_epub)
    assert build_book.build_epub([], output, tmp_path) is True
    assert build_book.image_to_cover_pdf(tmp_path / "missing.png", tmp_path / "cover.pdf") is False
    assert build_book.attach_covers(tmp_path, output) == output
    assert "Cover images not found" in capsys.readouterr().out

    invalid = tmp_path / "invalid.epub"
    invalid.write_bytes(b"not a zip")
    monkeypatch.setattr(build_book, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    assert build_book.build_epub([], invalid, tmp_path) is False

    missing_mimetype = tmp_path / "missing-mimetype.epub"
    with zipfile.ZipFile(missing_mimetype, "w") as archive:
        archive.writestr("book.opf", "metadata")
    assert build_book.build_epub([], missing_mimetype, tmp_path) is False
    wrong_mimetype = tmp_path / "wrong-mimetype.epub"
    with zipfile.ZipFile(wrong_mimetype, "w") as archive:
        archive.writestr("mimetype", "wrong")
        archive.writestr("book.opf", "metadata")
    assert build_book.build_epub([], wrong_mimetype, tmp_path) is False

    from PIL import Image

    cover_image = tmp_path / "cover.png"
    Image.new("RGBA", (10, 5), (10, 20, 30, 128)).save(cover_image)
    monkeypatch.setattr(build_book, "TRIM_W_PT", 72)
    monkeypatch.setattr(build_book, "TRIM_H_PT", 72)
    monkeypatch.setattr(build_book, "COVER_DPI", 10)
    assert build_book.image_to_cover_pdf(cover_image, tmp_path / "cover.pdf") is True

    monkeypatch.setattr(build_book, "COVER", Path("cover.png"))
    monkeypatch.setattr(build_book, "BACK_COVER", Path("back.png"))
    assert build_book.attach_covers(tmp_path, output) == output

    original_import = builtins.__import__

    def no_pillow(name: str, *args: object, **kwargs: object) -> object:
        if name == "PIL":
            raise ImportError("Pillow unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pillow)
    assert build_book.image_to_cover_pdf(cover_image, tmp_path / "no-pillow.pdf") is False


def test_builder_builds_and_cleans_intermediates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "chapter.md"
    source.write_text("# Chapter\n", encoding="utf-8")
    monkeypatch.setattr(build_book, "HERE", tmp_path)
    monkeypatch.setattr(build_book, "FRONT", [source])
    monkeypatch.setattr(build_book, "CHAPTERS", [])
    monkeypatch.setattr(build_book, "BACK", [])
    monkeypatch.setattr(build_book, "COVER", tmp_path / "missing-front.png")
    monkeypatch.setattr(build_book, "BACK_COVER", tmp_path / "missing-back.png")
    monkeypatch.setattr(build_book, "sys", SimpleNamespace(argv=["build_book.py"]))

    pdflatex_calls = 0

    def fake_run(cmd: list[object], **kwargs: object) -> SimpleNamespace:
        nonlocal pdflatex_calls
        if "-o" in cmd:
            output = Path(str(cmd[cmd.index("-o") + 1]))
            if output.suffix == ".tex":
                output.write_text("\\maketitle\n\\tableofcontents\n", encoding="utf-8")
            else:
                with zipfile.ZipFile(output, "w") as archive:
                    archive.writestr("mimetype", "application/epub+zip")
                    archive.writestr("book.opf", "metadata")
        if cmd and str(cmd[0]) == "pdflatex":
            pdflatex_calls += 1
            build_dir = Path(str(kwargs["cwd"]))
            (build_dir / "book.pdf").write_bytes(b"pdf")
            (build_dir / "book.log").write_text(
                "Rerun to get cross-references right\n" if pdflatex_calls == 2 else "stable\n",
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_book, "run", fake_run)
    (tmp_path / "texput.log").write_text("stray\n", encoding="utf-8")
    assert build_book.build() == 0
    assert list((tmp_path / "dist").glob("*.pdf"))
    assert list((tmp_path / "dist").glob("*.epub"))
    assert not (tmp_path / ".build").exists()
    assert pdflatex_calls == 3


def test_builder_handles_cover_merge_and_epub_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    build_dir = tmp_path / ".build"
    build_dir.mkdir()
    cover = tmp_path / "cover.png"
    Image.new("RGB", (2, 2), "white").save(cover)
    book = tmp_path / "book.pdf"
    book.write_bytes(b"book")
    monkeypatch.setattr(build_book, "COVER", Path("cover.png"))
    monkeypatch.setattr(build_book, "BACK_COVER", Path("back.png"))
    monkeypatch.setattr(build_book, "TRIM_W_PT", 72)
    monkeypatch.setattr(build_book, "TRIM_H_PT", 72)
    monkeypatch.setattr(build_book, "COVER_DPI", 10)

    class _Reader:
        def __init__(self, _path: str) -> None:
            self.pages = [object()]

    class _Writer:
        def add_page(self, _page: object) -> None:
            return None

        def append(self, _reader: object) -> None:
            return None

        def write(self, handle: object) -> None:
            handle.write(b"merged")  # type: ignore[union-attr]

    fake_pypdf = SimpleNamespace(PdfReader=_Reader, PdfWriter=_Writer)
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)
    assert build_book.attach_covers(tmp_path, book) == book
    assert book.read_bytes() == b"merged"
    monkeypatch.setattr(build_book, "image_to_cover_pdf", lambda *_args: False)
    assert build_book.attach_covers(tmp_path, book) == book

    monkeypatch.setattr(build_book, "HERE", tmp_path)
    monkeypatch.setattr(build_book, "FRONT", [])
    monkeypatch.setattr(build_book, "CHAPTERS", [])
    monkeypatch.setattr(build_book, "BACK", [])
    monkeypatch.setattr(build_book, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    assert build_book.build() == 1


def test_builder_stops_when_pandoc_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_book, "HERE", tmp_path)
    monkeypatch.setattr(build_book, "FRONT", [])
    monkeypatch.setattr(build_book, "CHAPTERS", [])
    monkeypatch.setattr(build_book, "BACK", [])
    monkeypatch.setattr(build_book, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    monkeypatch.setattr(sys, "argv", ["build_book.py"])
    assert build_book.build() == 1


def test_builder_reports_missing_pdf_after_latex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_book, "HERE", tmp_path)
    monkeypatch.setattr(build_book, "FRONT", [])
    monkeypatch.setattr(build_book, "CHAPTERS", [])
    monkeypatch.setattr(build_book, "BACK", [])

    def no_pdf(cmd: list[object], **kwargs: object) -> SimpleNamespace:
        if "-o" in cmd:
            output = Path(str(cmd[cmd.index("-o") + 1]))
            output.write_text("\\tableofcontents\n", encoding="utf-8")
        if cmd and str(cmd[0]) == "pdflatex":
            build_dir = Path(str(kwargs["cwd"]))
            (build_dir / "book.log").write_text("stable\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_book, "run", no_pdf)
    assert build_book.build() == 1
