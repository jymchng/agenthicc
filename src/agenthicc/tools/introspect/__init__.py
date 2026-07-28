"""Self-inspection of agenthicc's own documentation and source code.

The agent working inside an agenthicc session frequently needs the *current*
API — which `PhaseSpec` fields exist, what `root_reducer` actually does, where
the capability gate is documented — and prose in a system prompt goes stale the
moment the code changes.  This module reads both from the installed artefact:

* **Documentation** — the `docs/` tree plus the LLM-facing `llms.txt` /
  `llms-full.txt` and `README.md`.  Resolved from a source checkout, from
  `share/agenthicc/docs` in an installed distribution, or from the
  ``AGENTHICC_DOCS_DIR`` override.
* **Source** — any module or symbol under the `agenthicc` package.

Two properties matter and are enforced here rather than in the tool wrappers:

1. **No imports.** Source lookup resolves a module name to a file path by
   walking the package directory and then parses it with :mod:`ast`.  Nothing is
   imported, so inspecting a module with a missing optional dependency (or a
   module with import side effects) is safe, and private symbols are reachable.
2. **Containment.** Every resolved path is verified to sit inside the documentation
   root or the `agenthicc` package directory. Traversal (``../``), absolute paths
   elsewhere, and escaping symlinks are refused before any read.

All reads are bounded: line windows for documents, hard result caps for searches,
and a byte ceiling for extracted source.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import re
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

__all__ = [
    "DocEntry",
    "SearchHit",
    "SourceLocation",
    "docs_root",
    "docs_unavailable_result",
    "extra_doc_names",
    "find_doc",
    "list_docs",
    "package_root",
    "read_doc",
    "resolve_source",
    "search_docs",
    "search_source",
]

#: Environment variable that overrides documentation discovery.
DOCS_DIR_ENV = "AGENTHICC_DOCS_DIR"

#: Marker file that identifies a real documentation tree.
_DOCS_MARKER = "index.md"

#: Top-level files served alongside the ``docs/`` tree. These are the canonical
#: LLM-facing descriptions of the package and belong to the doc surface even
#: though they live at the repository root.
_EXTRA_DOCS: tuple[str, ...] = ("llms.txt", "llms-full.txt", "README.md")

#: Suffixes served by the documentation tools.
_DOC_SUFFIXES: frozenset[str] = frozenset({".md", ".txt", ".json"})

#: Hard ceilings. Callers may ask for less, never more.
MAX_DOC_LINES = 2000
MAX_SEARCH_RESULTS = 200
MAX_SOURCE_BYTES = 120_000
_MAX_LINE_CHARS = 400


@dataclass(frozen=True)
class DocEntry:
    """One document available to the inspection tools.

    :param path: Key used to read the document, e.g. ``guides/tools.md`` or
        ``llms.txt``. Always POSIX-style and relative to the documentation root.
    :param title: First Markdown heading, or the filename when there is none.
    :param lines: Total line count.
    :param bytes: Size on disk.
    """

    path: str
    title: str
    lines: int
    bytes: int

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-serializable form returned by the tools."""
        return {"path": self.path, "title": self.title, "lines": self.lines, "bytes": self.bytes}


@dataclass(frozen=True)
class SearchHit:
    """One matching line found by a documentation or source search."""

    path: str
    line: int
    text: str

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-serializable form returned by the tools."""
        return {"path": self.path, "line": self.line, "text": self.text}


@dataclass(frozen=True)
class SourceLocation:
    """A resolved module or symbol in the agenthicc package."""

    module: str
    symbol: str
    path: str
    kind: str
    lineno: int
    end_lineno: int
    signature: str = ""
    docstring: str = ""
    source: str = ""
    truncated: bool = False
    members: tuple[dict[str, object], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-serializable form returned by the tools."""
        payload: dict[str, object] = {
            "module": self.module,
            "symbol": self.symbol,
            "path": self.path,
            "kind": self.kind,
            "lineno": self.lineno,
            "end_lineno": self.end_lineno,
            "signature": self.signature,
            "docstring": self.docstring,
        }
        if self.source:
            payload["source"] = self.source
            payload["truncated"] = self.truncated
        if self.members:
            payload["members"] = list(self.members)
        return payload


# ── root resolution ───────────────────────────────────────────────────────────


def package_root() -> Path:
    """Return the directory of the installed ``agenthicc`` package."""
    return Path(__file__).resolve().parents[2]


def _candidate_doc_roots() -> list[Path]:
    """Return every plausible documentation root, most specific first."""
    candidates: list[Path] = []

    override = os.environ.get(DOCS_DIR_ENV, "").strip()
    if override:
        candidates.append(Path(override).expanduser())

    # Source checkout: <repo>/docs, with the package at <repo>/src/agenthicc.
    package = package_root()
    for parent in package.parents:
        candidates.append(parent / "docs")

    # Installed distribution: data files land under <prefix>/share/agenthicc/docs.
    prefixes = {sys.prefix, sys.base_prefix, sysconfig.get_path("data") or ""}
    candidates.extend(
        Path(prefix) / "share" / "agenthicc" / "docs" for prefix in prefixes if prefix
    )
    return candidates


@lru_cache(maxsize=8)
def _resolve_docs_root(override: str) -> Path | None:
    """Cached documentation-root resolution, keyed by the env override value."""
    for candidate in _candidate_doc_roots():
        try:
            if (candidate / _DOCS_MARKER).is_file():
                return candidate.resolve()
        except OSError:  # pragma: no cover — unreadable candidate path
            continue
    return None


def docs_root() -> Path | None:
    """Return the documentation root, or ``None`` when no tree can be found.

    Checked in order: ``AGENTHICC_DOCS_DIR``, a source checkout's ``docs/``,
    then ``<prefix>/share/agenthicc/docs`` for an installed distribution. A
    candidate counts only when it contains ``index.md``.
    """
    return _resolve_docs_root(os.environ.get(DOCS_DIR_ENV, "").strip())


def extra_doc_names() -> tuple[str, ...]:
    """Return the top-level document names served alongside the docs tree."""
    return _EXTRA_DOCS


def _extra_doc_path(root: Path, name: str) -> Path | None:
    """Return the on-disk path for top-level document *name*, if it exists.

    In an installed distribution the file sits inside the packaged docs tree; in
    a source checkout it sits at the repository root, one level above ``docs/``.
    """
    for candidate in (root / name, root.parent / name):
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover — unreadable candidate path
            continue
    return None


# ── documentation ─────────────────────────────────────────────────────────────


def _title_of(path: Path, text: str) -> str:
    """Return the document's first Markdown heading, or its filename."""
    for line in text.splitlines()[:60]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.name


def _read_text(path: Path) -> str | None:
    """Read *path* as UTF-8 text, returning ``None`` when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _entry_for(root: Path, path: Path, key: str) -> DocEntry | None:
    """Build a :class:`DocEntry` for *path* published under *key*."""
    text = _read_text(path)
    if text is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:  # pragma: no cover — file vanished between calls
        size = len(text.encode("utf-8"))
    return DocEntry(
        path=key,
        title=_title_of(path, text),
        lines=len(text.splitlines()),
        bytes=size,
    )


def list_docs(section: str = "") -> list[DocEntry]:
    """Return every available document, optionally filtered by *section* prefix.

    *section* matches the leading path component or any path prefix, so
    ``"guides"`` returns the guides and ``"reference/c"`` narrows further.
    Returns an empty list when no documentation tree is available.
    """
    root = docs_root()
    if root is None:
        return []

    wanted = section.strip().strip("/")
    entries: list[DocEntry] = []

    for name in _EXTRA_DOCS:
        path = _extra_doc_path(root, name)
        if path is not None and (not wanted or name.startswith(wanted)):
            entry = _entry_for(root, path, name)
            if entry is not None:
                entries.append(entry)

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in _DOC_SUFFIXES:
            continue
        try:
            key = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover — rglob always yields descendants
            continue
        if key in _EXTRA_DOCS:
            continue  # already served as a top-level document
        if wanted and not key.startswith(wanted):
            continue
        entry = _entry_for(root, path, key)
        if entry is not None:
            entries.append(entry)

    return entries


def find_doc(path: str) -> Path | None:
    """Resolve documentation *path* to a contained file, or ``None``.

    Refuses traversal, absolute paths, and anything resolving outside the
    documentation root — except the explicitly allowed top-level documents.
    """
    root = docs_root()
    if root is None:
        return None

    key = path.strip().replace("\\", "/").lstrip("/")
    if not key:
        return None

    if key in _EXTRA_DOCS:
        return _extra_doc_path(root, key)

    candidate = root / key
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    if not resolved.is_file() or resolved.suffix not in _DOC_SUFFIXES:
        return None
    return resolved


def read_doc(path: str, start_line: int = 1, max_lines: int = 400) -> dict[str, object]:
    """Read a bounded window of the document at *path*.

    :param path: Key from :func:`list_docs`, e.g. ``guides/workflows.md``.
    :param start_line: 1-based first line to return.
    :param max_lines: Window size, clamped to :data:`MAX_DOC_LINES`.
    :returns: ``ok: True`` with ``content``, ``total_lines``, ``truncated`` and
        ``next_start_line``; otherwise ``ok: False`` with ``error`` and ``fix``.
    """
    root = docs_root()
    if root is None:
        return docs_unavailable_result()

    resolved = find_doc(path)
    if resolved is None:
        return {
            "ok": False,
            "error": f"No agenthicc document is published at {path!r}.",
            "fix": "Call list_agenthicc_docs() to see the available paths.",
        }

    text = _read_text(resolved)
    if text is None:
        return {
            "ok": False,
            "error": f"{path} could not be read as text.",
            "fix": "Pick a different document from list_agenthicc_docs().",
        }

    lines = text.splitlines()
    total = len(lines)
    window = max(1, min(int(max_lines), MAX_DOC_LINES))
    start = max(1, int(start_line))
    selected = lines[start - 1 : start - 1 + window]
    end = start + len(selected) - 1

    return {
        "ok": True,
        "path": path,
        "title": _title_of(resolved, text),
        "total_lines": total,
        "start_line": start,
        "end_line": max(end, start - 1),
        "truncated": end < total,
        "next_start_line": end + 1 if end < total else 0,
        "content": "\n".join(selected),
    }


def search_docs(query: str, max_results: int = 40, section: str = "") -> dict[str, object]:
    """Search the documentation for *query* and return matching lines.

    *query* is treated as a regular expression when it compiles, and as a
    case-insensitive substring otherwise, so a plain phrase always works.
    """
    root = docs_root()
    if root is None:
        return docs_unavailable_result()
    if not query.strip():
        return {
            "ok": False,
            "error": "A non-empty query is required.",
            "fix": "Call search_agenthicc_docs('capability gate') with the phrase to find.",
        }

    matcher = _build_matcher(query)
    limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    hits: list[SearchHit] = []
    scanned = 0

    for entry in list_docs(section):
        resolved = find_doc(entry.path)
        if resolved is None:
            continue
        text = _read_text(resolved)
        if text is None:
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), start=1):
            if matcher(line):
                hits.append(SearchHit(entry.path, number, _clip(line.strip())))
                if len(hits) >= limit:
                    return _search_result(query, hits, scanned, truncated=True)

    return _search_result(query, hits, scanned, truncated=False)


def docs_unavailable_result() -> dict[str, object]:
    """Return the shared failure result for a missing documentation tree."""
    return {
        "ok": False,
        "error": "No agenthicc documentation tree is available in this installation.",
        "fix": (
            f"Set {DOCS_DIR_ENV} to a directory containing index.md, or use "
            "inspect_agenthicc_source to read the code directly."
        ),
    }


def _search_result(
    query: str,
    hits: list[SearchHit],
    scanned: int,
    *,
    truncated: bool,
) -> dict[str, object]:
    """Package search *hits* into the tool result shape."""
    return {
        "ok": True,
        "query": query,
        "files_scanned": scanned,
        "match_count": len(hits),
        "truncated": truncated,
        "matches": [hit.as_dict() for hit in hits],
    }


def _build_matcher(query: str) -> Callable[[str], bool]:
    """Return a predicate matching *query* as a regex, falling back to substring."""
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        needle = query.lower()
        return lambda line: needle in line.lower()
    return lambda line: pattern.search(line) is not None


def _clip(text: str) -> str:
    """Clip *text* so one long line cannot dominate a result set."""
    return text if len(text) <= _MAX_LINE_CHARS else text[:_MAX_LINE_CHARS] + "…"


# ── source ────────────────────────────────────────────────────────────────────


def _module_file(module: str) -> Path | None:
    """Resolve dotted *module* to a file inside the agenthicc package.

    Purely path-based — nothing is imported, so a module with an unavailable
    optional dependency is still inspectable.
    """
    parts = module.split(".")
    if not parts or parts[0] != "agenthicc":
        return None
    if any(not part.isidentifier() for part in parts):
        return None

    root = package_root()
    base = root.joinpath(*parts[1:]) if len(parts) > 1 else root
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover — unreadable candidate path
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            return resolved
    return None


def _split_target(target: str) -> tuple[str, str] | None:
    """Split *target* into ``(module, symbol)``.

    Accepts ``pkg.mod:Symbol``, ``pkg.mod:Class.method``, the all-dots form
    ``pkg.mod.Symbol``, and a bare module. Returns ``None`` when no module under
    ``agenthicc`` matches.
    """
    cleaned = target.strip()
    if not cleaned:
        return None

    if ":" in cleaned:
        module, _, symbol = cleaned.partition(":")
        module, symbol = module.strip(), symbol.strip()
        return (module, symbol) if _module_file(module) is not None else None

    parts = cleaned.split(".")
    for cut in range(len(parts), 0, -1):
        module = ".".join(parts[:cut])
        if _module_file(module) is not None:
            return module, ".".join(parts[cut:])
    return None


#: Node types a named definition can be. Precise enough that every attribute
#: access below is statically checked — no getattr fallbacks needed.
_Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Assign | ast.AnnAssign

#: Node types that can contain named definitions.
_Container = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _signature_of(node: _Definition) -> str:
    """Return a readable one-line signature for a definition node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        args = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        return f"{prefix} {node.name}({args}){returns}"
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(base) for base in node.bases)
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    return _clip(ast.unparse(node))


def _kind_of(node: _Definition) -> str:
    """Return a short machine label for a definition node."""
    if isinstance(node, ast.AsyncFunctionDef):
        return "async function"
    if isinstance(node, ast.FunctionDef):
        return "function"
    if isinstance(node, ast.ClassDef):
        return "class"
    return "variable"


def _first_lineno(node: _Definition) -> int:
    """Return the first line of *node*, including any decorators."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return min([node.lineno, *(dec.lineno for dec in node.decorator_list)])
    return node.lineno


def _last_lineno(node: _Definition) -> int:
    """Return the last line of *node*, falling back to its first line."""
    return node.end_lineno if node.end_lineno is not None else node.lineno


def _named_children(node: _Container) -> list[tuple[str, _Definition]]:
    """Return ``(name, node)`` for every named definition directly inside *node*."""
    found: list[tuple[str, _Definition]] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((child.name, child))
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            found.append((child.target.id, child))
        elif isinstance(child, ast.Assign):
            found.extend((t.id, child) for t in child.targets if isinstance(t, ast.Name))
    return found


def _find_symbol(tree: ast.Module, dotted: str) -> _Definition | None:
    """Walk *dotted* (``Class.method``) through the definitions of *tree*."""
    current: _Container = tree
    found: _Definition | None = None
    parts = dotted.split(".")
    for index, part in enumerate(parts):
        match = next((node for name, node in _named_children(current) if name == part), None)
        if match is None:
            return None
        found = match
        if index == len(parts) - 1:
            break
        if not isinstance(match, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return None  # a variable has no body, so the remaining parts cannot resolve
        current = match
    return found


def _outline(tree: ast.Module) -> tuple[dict[str, object], ...]:
    """Return one entry per top-level definition, with class members nested."""
    members: list[dict[str, object]] = []
    for name, node in _named_children(tree):
        entry: dict[str, object] = {
            "name": name,
            "kind": _kind_of(node),
            "lineno": _first_lineno(node),
            "signature": _signature_of(node),
        }
        if isinstance(node, ast.ClassDef):
            entry["members"] = [
                {
                    "name": child_name,
                    "kind": _kind_of(child),
                    "lineno": _first_lineno(child),
                    "signature": _signature_of(child),
                }
                for child_name, child in _named_children(node)
            ]
        members.append(entry)
    return tuple(members)


def resolve_source(target: str, *, include_source: bool = True) -> dict[str, object]:
    """Resolve *target* to a module or symbol in the agenthicc package.

    :param target: ``agenthicc.kernel.reducer``,
        ``agenthicc.kernel.reducer:root_reducer``,
        ``agenthicc.workflows.plugin:WorkflowPlugin.build_runner``, or the
        all-dots equivalent.
    :param include_source: When False, only the location, signature, docstring,
        and (for modules) the outline are returned — much cheaper for large
        modules.
    :returns: ``ok: True`` plus the resolved location, or ``ok: False`` with an
        ``error`` and a concrete ``fix``.
    """
    cleaned = target.strip()
    if not cleaned:
        return {
            "ok": False,
            "error": "A target is required.",
            "fix": "Pass a module such as 'agenthicc.kernel.reducer' or a symbol "
            "such as 'agenthicc.workflows.plugin:PhaseSpec'.",
        }
    if not cleaned.split(":")[0].strip().startswith("agenthicc"):
        return {
            "ok": False,
            "error": f"{cleaned!r} is not part of the agenthicc package.",
            "fix": "Only 'agenthicc.*' modules are inspectable. Use read_file for other files.",
        }

    split = _split_target(cleaned)
    if split is None:
        return {
            "ok": False,
            "error": f"No agenthicc module matches {cleaned!r}.",
            "fix": "Check the dotted path, or search for the symbol with "
            "search_agenthicc_source('<name>').",
        }
    module, symbol = split
    path = _module_file(module)
    if path is None:  # pragma: no cover — _split_target already verified this
        return {
            "ok": False,
            "error": f"Module {module!r} has no source file.",
            "fix": "Inspect the parent package instead.",
        }

    text = _read_text(path)
    if text is None:
        return {
            "ok": False,
            "error": f"{module} could not be read from {path}.",
            "fix": "Inspect a different module.",
        }
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover — shipped source always parses
        return {
            "ok": False,
            "error": f"{module} does not parse: line {exc.lineno}: {exc.msg}",
            "fix": "Inspect a different module.",
        }

    lines = text.splitlines()
    rel = _relative(path)

    if not symbol:
        location = SourceLocation(
            module=module,
            symbol="",
            path=rel,
            kind="module",
            lineno=1,
            end_lineno=len(lines),
            signature=module,
            docstring=ast.get_docstring(tree) or "",
            members=_outline(tree),
        )
        if include_source:
            body, truncated = _bounded("\n".join(lines))
            location = dataclasses.replace(location, source=body, truncated=truncated)
        return {"ok": True, **location.as_dict()}

    node = _find_symbol(tree, symbol)
    if node is None:
        available = ", ".join(name for name, _ in _named_children(tree)[:40])
        return {
            "ok": False,
            "error": f"{module} defines no symbol named {symbol!r}.",
            "fix": f"Available top-level names: {available or '(none)'}",
        }

    start = _first_lineno(node)
    end = max(_last_lineno(node), start)
    docstring = ast.get_docstring(node) if isinstance(node, _DOCSTRING_NODES) else None
    body, truncated = _bounded("\n".join(lines[start - 1 : end])) if include_source else ("", False)

    location = SourceLocation(
        module=module,
        symbol=symbol,
        path=rel,
        kind=_kind_of(node),
        lineno=start,
        end_lineno=end,
        signature=_signature_of(node),
        docstring=docstring or "",
        source=body,
        truncated=truncated,
        members=_outline_of_class(node) if isinstance(node, ast.ClassDef) else (),
    )
    return {"ok": True, **location.as_dict()}


#: Node types :func:`ast.get_docstring` accepts.
_DOCSTRING_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _outline_of_class(node: ast.ClassDef) -> tuple[dict[str, object], ...]:
    """Return one entry per member defined directly in class *node*."""
    return tuple(
        {
            "name": name,
            "kind": _kind_of(child),
            "lineno": _first_lineno(child),
            "signature": _signature_of(child),
        }
        for name, child in _named_children(node)
    )


def _bounded(source: str) -> tuple[str, bool]:
    """Clamp *source* to :data:`MAX_SOURCE_BYTES`, reporting whether it was cut."""
    encoded = source.encode("utf-8")
    if len(encoded) <= MAX_SOURCE_BYTES:
        return source, False
    return encoded[:MAX_SOURCE_BYTES].decode("utf-8", errors="ignore"), True


def _relative(path: Path) -> str:
    """Return *path* relative to the package's parent, for readable output."""
    root = package_root()
    try:
        return f"agenthicc/{path.relative_to(root).as_posix()}"
    except ValueError:  # pragma: no cover — callers only pass contained paths
        return str(path)


def search_source(query: str, max_results: int = 40, module: str = "") -> dict[str, object]:
    """Search the agenthicc package source for *query* and return matching lines.

    :param query: Regular expression, or a plain substring when it does not
        compile as one.
    :param max_results: Result cap, clamped to :data:`MAX_SEARCH_RESULTS`.
    :param module: Restrict the search to this dotted module or subpackage.
    """
    if not query.strip():
        return {
            "ok": False,
            "error": "A non-empty query is required.",
            "fix": "Call search_agenthicc_source('def root_reducer') with the text to find.",
        }

    root = package_root()
    scope = root
    if module.strip():
        target = _module_file(module.strip())
        if target is None:
            return {
                "ok": False,
                "error": f"No agenthicc module or subpackage matches {module!r}.",
                "fix": "Drop the module filter, or use a dotted path such as 'agenthicc.kernel'.",
            }
        scope = target.parent if target.name == "__init__.py" else target

    matcher = _build_matcher(query)
    limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    paths = [scope] if scope.is_file() else sorted(scope.rglob("*.py"))
    hits: list[SearchHit] = []
    scanned = 0

    for path in paths:
        text = _read_text(path)
        if text is None:
            continue
        scanned += 1
        rel = _relative(path)
        for number, line in enumerate(text.splitlines(), start=1):
            if matcher(line):
                hits.append(SearchHit(rel, number, _clip(line.rstrip())))
                if len(hits) >= limit:
                    return _search_result(query, hits, scanned, truncated=True)

    return _search_result(query, hits, scanned, truncated=False)
