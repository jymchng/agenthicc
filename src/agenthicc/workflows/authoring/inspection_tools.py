"""Bounded, read-only inspection tools used while designing workflows."""

from __future__ import annotations

import importlib
import importlib.resources
import inspect
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from lauren_ai._tools import tool
from agenthicc.tools.base import ToolLike

_MAX_CHARS = 20_000
_DOC_SUFFIXES = {".md", ".rst", ".txt"}


def _safe_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 8_000
    return max(500, min(value, _MAX_CHARS))


def _safe_doc_parts(path: str) -> tuple[str, ...] | None:
    candidate = Path(path)
    if candidate.is_absolute() or candidate.suffix.lower() not in _DOC_SUFFIXES:
        return None
    parts = tuple(candidate.parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def _find_doc(parts: tuple[str, ...]) -> tuple[str, str] | None:
    resource = importlib.resources.files("agenthicc").joinpath("docs", *parts)
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
    source_path = Path(__file__).resolve().parents[4] / "docs" / Path(*parts)
    if source_path.is_file():
        return str(source_path), source_path.read_text(encoding="utf-8")
    return None


def _allowed_module(module: str) -> bool:
    return (
        module == "agenthicc"
        or module.startswith("agenthicc.")
        and ".." not in module
        and all(part.isidentifier() for part in module.split("."))
    )


def make_inspection_tools() -> list[ToolLike]:
    """Return documentation and source inspection callables."""

    @tool()
    async def inspect_agenthicc_documentation(
        path: str, max_chars: int = 8_000
    ) -> dict[str, object]:
        """Read one bounded documentation file below the installed agenthicc docs directory.

        Args:
            path: Relative .md, .rst, or .txt path such as guides/workflows.md.
            max_chars: Maximum number of returned characters.
        """

        parts = _safe_doc_parts(path)
        if parts is None:
            return {"ok": False, "error": "path must be a relative documentation path"}
        found = _find_doc(parts)
        if found is None:
            return {"ok": False, "error": f"documentation file not found: {path}"}
        display_path, content = found
        limit = _safe_limit(max_chars)
        return {
            "ok": True,
            "path": display_path,
            "content": content[:limit],
            "truncated": len(content) > limit,
        }

    @tool()
    async def inspect_agenthicc_source(
        module: str,
        symbol: str = "",
        max_chars: int = 8_000,
    ) -> dict[str, object]:
        """Inspect current agenthicc source, including private symbols.

        Args:
            module: Dotted agenthicc module name.
            symbol: Optional Python identifier to inspect.
            max_chars: Maximum source and documentation characters.
        """

        if not _allowed_module(module):
            return {"ok": False, "error": "only agenthicc.* modules may be inspected"}
        if symbol and not symbol.isidentifier():
            return {"ok": False, "error": "symbol must be a Python identifier"}
        limit = _safe_limit(max_chars)
        try:
            imported = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"could not import {module}: {type(exc).__name__}: {exc}"}
        target: object = imported
        if symbol:
            if symbol not in vars(imported):
                return {"ok": False, "error": f"{module}.{symbol} was not found"}
            target = vars(imported)[symbol]
        try:
            signature = str(inspect.signature(target)) if callable(target) else ""
        except (TypeError, ValueError):
            signature = ""
        try:
            source = (
                inspect.getsource(target)
                if inspect.ismodule(target) or inspect.isclass(target) or inspect.isfunction(target)
                else ""
            )
        except (OSError, TypeError):
            source = ""
        return {
            "ok": True,
            "module": module,
            "symbol": symbol or None,
            "signature": signature,
            "docstring": (inspect.getdoc(target) or "")[:limit],
            "source": source[:limit],
            "source_truncated": len(source) > limit,
            "names": sorted(dir(imported))[:500],
        }

    return [inspect_agenthicc_documentation, inspect_agenthicc_source]


__all__ = ["make_inspection_tools"]
