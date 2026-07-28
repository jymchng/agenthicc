"""Bounded read-only inspection tools used during workflow design."""

import importlib
import importlib.resources
import inspect
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from lauren_ai._tools import tool

from agenthicc.tools.base import ToolLike

_MAX_CHARS = 20_000
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})


def _limit(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(500, min(value, _MAX_CHARS))
    return 8_000


def _module_allowed(module: object) -> bool:
    return (
        isinstance(module, str)
        and module.startswith("agenthicc")
        and all(part.isidentifier() for part in module.split("."))
        and ".." not in module
    )


def make_inspection_tools() -> list[ToolLike]:
    """Return bounded documentation and source inspection callables."""

    @tool()
    async def inspect_agenthicc_documentation(
        path: str, max_chars: int = 8_000
    ) -> dict[str, object]:
        """Read one relative documentation file from the current checkout."""

        candidate = Path(path)
        if (
            candidate.is_absolute()
            or candidate.suffix.lower() not in _DOC_SUFFIXES
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            return {"ok": False, "error": "path must be a relative .md, .rst, or .txt path"}
        parts = candidate.parts
        content: str | None = None
        display_path = str(candidate)
        try:
            packaged = importlib.resources.files("agenthicc").joinpath("docs", *parts)
            if packaged.is_file():
                content = packaged.read_text(encoding="utf-8")
                display_path = f"agenthicc/docs/{candidate.as_posix()}"
        except (FileNotFoundError, OSError):
            pass
        if content is None:
            try:
                installed = distribution("agenthicc").locate_file(
                    Path("share") / "agenthicc" / "docs" / candidate
                )
            except PackageNotFoundError:
                installed = None
            installed_path = Path(str(installed)) if installed is not None else None
            if installed_path is not None and installed_path.is_file():
                content = installed_path.read_text(encoding="utf-8")
                display_path = str(installed_path)
        if content is None:
            source_path = Path(__file__).resolve().parents[4] / "docs" / candidate
            if source_path.is_file():
                content = source_path.read_text(encoding="utf-8")
                display_path = str(source_path)
        if content is None:
            return {"ok": False, "error": f"documentation file not found: {path}"}
        limit = _limit(max_chars)
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
        """Inspect one current agenthicc module or public/private symbol."""

        if not _module_allowed(module):
            return {"ok": False, "error": "only agenthicc.* modules may be inspected"}
        if symbol and not symbol.isidentifier():
            return {"ok": False, "error": "symbol must be a Python identifier"}
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
            source = (
                inspect.getsource(target)
                if inspect.isroutine(target) or inspect.isclass(target)
                else ""
            )
        except (OSError, TypeError):
            source = ""
        limit = _limit(max_chars)
        return {
            "ok": True,
            "module": module,
            "symbol": symbol or None,
            "signature": str(inspect.signature(target)) if callable(target) else "",
            "docstring": (inspect.getdoc(target) or "")[:limit],
            "source": source[:limit],
            "source_truncated": len(source) > limit,
            "names": sorted(dir(imported))[:500],
        }

    return [inspect_agenthicc_documentation, inspect_agenthicc_source]


__all__ = ["make_inspection_tools"]
