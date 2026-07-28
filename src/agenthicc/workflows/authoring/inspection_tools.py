"""Read-only documentation and API inspection tools for authoring agents."""

from __future__ import annotations

import importlib
import importlib.resources
import inspect
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path


_MAX_INSPECTION_CHARS = 20_000
_ALLOWED_DOC_SUFFIXES = {".md", ".rst", ".txt"}


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 8_000
    return max(500, min(value, _MAX_INSPECTION_CHARS))


def _safe_doc_parts(path: str) -> tuple[str, ...] | None:
    candidate = Path(path)
    if candidate.is_absolute() or candidate.suffix.lower() not in _ALLOWED_DOC_SUFFIXES:
        return None
    parts = tuple(candidate.parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def _documentation_resource(parts: tuple[str, ...]) -> tuple[str, str] | None:
    """Return ``(display_path, text)`` from package data or a source checkout."""

    package_docs = importlib.resources.files("agenthicc").joinpath("docs")
    resource = package_docs.joinpath(*parts)
    try:
        if resource.is_file():
            return f"agenthicc/docs/{'/'.join(parts)}", resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        pass

    try:
        installed = distribution("agenthicc").locate_file(
            Path("share") / "agenthicc" / "docs" / Path(*parts)
        )
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        installed_path = Path(str(installed))
        if installed_path.is_file():
            return str(installed_path), installed_path.read_text(encoding="utf-8")

    # The source checkout keeps the canonical docs at repository root.  The
    # package-data path above is used after installation, so this fallback
    # makes the same tool useful during local authoring and in tests.
    repository_docs = Path(__file__).resolve().parents[4] / "docs"
    source_path = repository_docs.joinpath(*parts)
    if source_path.is_file():
        return str(source_path), source_path.read_text(encoding="utf-8")
    return None


def _module_is_allowed(module: str) -> bool:
    return (
        module == "agenthicc"
        or module.startswith("agenthicc.")
        and ".." not in module
        and all(part.isidentifier() for part in module.split("."))
    )


def make_authoring_inspection_tools() -> list[Callable[..., object]]:
    """Return bounded, read-only tools available in every ``create_*`` design turn."""

    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    @_tool()
    async def inspect_agenthicc_documentation(
        path: str,
        max_chars: int = 8_000,
    ) -> dict[str, object]:
        """Read one installed agenthicc documentation file.

        Use repository-relative paths such as ``guides/workflows.md`` or
        ``guides/command-execution.md``.  The tool is read-only and refuses
        absolute paths and traversal outside the packaged documentation.

        Args:
            path: Relative path below the installed agenthicc ``docs`` directory.
            max_chars: Maximum returned characters, bounded by the tool.
        """

        parts = _safe_doc_parts(path)
        if parts is None:
            return {
                "ok": False,
                "error": "path must be a relative .md, .rst, or .txt documentation path",
            }
        found = _documentation_resource(parts)
        if found is None:
            return {"ok": False, "error": f"documentation file not found: {path}"}
        display_path, content = found
        limit = _limit(max_chars)
        truncated = len(content) > limit
        return {
            "ok": True,
            "path": display_path,
            "content": content[:limit],
            "truncated": truncated,
        }

    @_tool()
    async def inspect_agenthicc_source(
        module: str,
        symbol: str = "",
        max_chars: int = 8_000,
    ) -> dict[str, object]:
        """Inspect the current installed Python API surface with ``inspect``.

        Only ``agenthicc`` modules are importable.  With no symbol, the tool
        returns module names and module source; with a symbol, it returns the
        symbol's signature, docstring, and source. Public and private Python
        identifiers are inspectable. Use this before generating
        code so the artifact targets the installed API rather than stale memory.

        Args:
            module: Dotted module name beginning with ``agenthicc``.
            symbol: Optional public or private class, function, or constant in
                the module.
            max_chars: Maximum source/doc text returned, bounded by the tool.
        """

        if not _module_is_allowed(module):
            return {"ok": False, "error": "only agenthicc.* modules may be inspected"}
        if symbol and not symbol.isidentifier():
            return {"ok": False, "error": "symbol must be a Python identifier"}
        limit = _limit(max_chars)
        try:
            imported = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"could not import {module}: {type(exc).__name__}: {exc}"}

        target: object = imported
        if symbol:
            module_members = vars(imported)
            if symbol not in module_members:
                return {"ok": False, "error": f"{module}.{symbol} was not found"}
            target = module_members[symbol]

        if (
            inspect.ismodule(target)
            or inspect.isclass(target)
            or inspect.isfunction(target)
            or inspect.ismethod(target)
        ):
            try:
                source = inspect.getsource(target)
            except (OSError, TypeError):
                source = ""
        else:
            source = ""
        doc = inspect.getdoc(target) or ""
        try:
            signature = str(inspect.signature(target)) if callable(target) else ""
        except (TypeError, ValueError):
            signature = ""
        all_names = sorted(dir(imported))
        public_names = [name for name in all_names if not name.startswith("_")]
        return {
            "ok": True,
            "module": module,
            "symbol": symbol or None,
            "signature": signature,
            "docstring": doc[:limit],
            "source": source[:limit],
            "source_truncated": len(source) > limit,
            "public_names": public_names[:500],
            "names": all_names[:500],
        }

    return [inspect_agenthicc_documentation, inspect_agenthicc_source]
