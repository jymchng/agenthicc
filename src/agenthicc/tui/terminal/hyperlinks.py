"""Clickable paths and URLs: OSC 8 hyperlinks for the transcript (TUX-10 §2.5).

One helper for every surface that shows a workspace path: the tool rows, the
diff headers and the notices in the scrollback use it today, and the
transcript view (TUX-09) calls the same functions::

    links = Hyperlinks.for_state(app_state)          # [ui] hyperlinks, editor_links
    url = links.url("src/app.py", line=12)           # file:///work/src/app.py  (or vscode://…)
    text = Text("src/app.py", style=links.style("src/app.py"))
    links.linkify(notice_text)                       # link every real path and URL in a Text

Rich writes the escape sequence itself for any style that carries a ``link``
and only when the console is a terminal, so a pipe or a file never receives
one. When links are off :meth:`Hyperlinks.url` returns ``None`` and every other
method leaves its input as it was.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from rich.style import Style

if TYPE_CHECKING:
    from rich.text import Text

    from agenthicc.tui.terminal.backend import TerminalBackend

__all__ = [
    "EDITOR_SCHEMES",
    "Hyperlinks",
    "autolink_urls",
    "links_enabled",
]

#: ``[ui] editor_links`` → the editor's URL template (``{path}`` is the absolute path).
EDITOR_SCHEMES: Mapping[str, str] = {
    "vscode": "vscode://file{path}:{line}",
    "cursor": "cursor://file{path}:{line}",
    "zed": "zed://file{path}:{line}",
    "idea": "idea://open?file={path}&line={line}",
}

#: A URL in running text; trailing punctuation belongs to the sentence.
_URL = re.compile(r"https?://[^\s<>\"'`)\]]+[^\s<>\"'`)\].,;:!?]")
#: A path in a notice: ``src/app.py``, ``./a/b.txt``, ``/abs/x.py``, ``README.md``, with ``:line``.
_PATH = re.compile(
    r"(?<![\w/.~-])(?P<path>(?:\.{0,2}/)?(?:[\w.@+-]+/)*[\w@+-][\w.@+-]*\.[A-Za-z0-9]{1,8}"
    r"|(?:\.{0,2}/)?(?:[\w.@+-]+/)+[\w.@+-]+)(?::(?P<line>\d+))?(?![\w/])"
)


def links_enabled(mode: str, *, supported: bool, screen_reader: bool = False) -> bool:
    """Whether links are written: ``on`` always, ``off`` never, ``auto`` where *supported*.

    ``screen_reader`` turns every mode off: a link is a style, and the reader
    should hear the text. *supported* is the backend's answer for this terminal.
    """
    if screen_reader or mode == "off":
        return False
    return mode == "on" or supported


@dataclass(frozen=True)
class Hyperlinks:
    """Where workspace paths point, and whether to link them at all."""

    enabled: bool
    root: Path
    #: ``vscode``, ``cursor``, ``idea``, ``zed`` or ``""`` for a ``file://`` URL.
    editor: str = ""

    @classmethod
    def for_state(
        cls,
        app_state: object,
        *,
        backend: TerminalBackend | None = None,
    ) -> Hyperlinks:
        """The links the session's ``[ui]`` settings ask for, rooted at its directory."""
        from agenthicc.tui.preferences import ui_settings

        ui = ui_settings(app_state)
        mode = ui.hyperlinks if ui is not None else "auto"
        reader = ui is not None and ui.screen_reader
        supported = False
        if mode == "auto" and not reader:
            if backend is None:
                from agenthicc.tui.terminal.backend import get_backend

                backend = get_backend()
            supported = backend.supports_hyperlinks()
        enabled = links_enabled(mode, supported=supported, screen_reader=reader)
        return cls(enabled, _root_of(app_state), ui.editor_links if ui is not None else "")

    def absolute(self, path: str | Path) -> Path:
        """*path* as an absolute path under :attr:`root` (``~`` expanded)."""
        candidate = Path(path).expanduser()
        return candidate if candidate.is_absolute() else self.root / candidate

    def url(self, path: str | Path, line: int | None = None) -> str | None:
        """The URL of *path* (``file://``, or the editor's scheme with *line*); ``None`` when off."""
        if not self.enabled or not str(path):
            return None
        target = self.absolute(path)
        template = EDITOR_SCHEMES.get(self.editor)
        if template is None:
            return target.as_uri()
        return template.format(path=quote(target.as_posix(), safe="/"), line=max(1, line or 1))

    def style(self, path: str | Path, line: int | None = None) -> Style | None:
        """A Rich style that links to *path*, or ``None`` when links are off."""
        url = self.url(path, line)
        return None if url is None else Style(link=url)

    def style_if_exists(self, path: str | Path, line: int | None = None) -> Style | None:
        """Like :meth:`style`, but only for a file or directory that is there.

        A tool row's target may be a document name or a count ("3 files"), and
        a deleted file has nothing to open.
        """
        if not self.enabled or not str(path) or "\n" in str(path):
            return None
        try:
            present = self.absolute(path).exists()
        except (OSError, ValueError):
            return None
        return self.style(path, line) if present else None

    def linkify(self, text: Text) -> Text:
        """Link the URLs and the existing workspace files named in *text* (in place).

        A word is a path only when it names a file that exists under the
        workspace root, so ``e.g.`` and ``v1.2`` stay plain. Returns *text*.
        """
        if not self.enabled:
            return text
        plain = text.plain
        for match in _URL.finditer(plain):
            text.stylize(Style(link=match.group(0)), match.start(), match.end())
        for match in _PATH.finditer(plain):
            candidate = self.absolute(match.group("path"))
            try:
                exists = candidate.is_file()
            except OSError:
                exists = False
            if exists:
                line = match.group("line")
                text.stylize(
                    Style(link=self.url(candidate, int(line) if line else None)),
                    match.start(),
                    match.end(),
                )
        return text


def _root_of(app_state: object) -> Path:
    """The session's working directory, or the process's."""
    try:
        conversation = object.__getattribute__(app_state, "conversation")
        directory = conversation.current_directory()
    except AttributeError:
        return Path.cwd()
    return Path(directory) if isinstance(directory, str) and directory else Path.cwd()


_FENCE = re.compile(r"^\s*(```|~~~)")


def autolink_urls(markdown: str) -> str:
    """Wrap bare URLs in *markdown* as ``<autolinks>`` so Rich draws them as links.

    Fenced code, inline code and URLs that are already part of a Markdown link
    (``[text](url)``, ``<url>``) are left alone.
    """
    out: list[str] = []
    fenced = False
    for line in markdown.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            out.append(line)
        elif fenced or "http" not in line:
            out.append(line)
        else:
            pieces = line.split("`")
            for index in range(0, len(pieces), 2):  # even pieces are outside inline code
                pieces[index] = _wrap_urls(pieces[index])
            out.append("`".join(pieces))
    return "\n".join(out)


def _wrap_urls(piece: str) -> str:
    def wrap(match: re.Match[str]) -> str:
        before = piece[match.start() - 1] if match.start() else ""
        if before in "(<[\"'":  # already a Markdown link, an autolink or an attribute
            return match.group(0)
        return f"<{match.group(0)}>"

    return _URL.sub(wrap, piece)
