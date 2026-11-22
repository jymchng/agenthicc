"""Execute extension source without trusting a timestamp-based bytecode cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

__all__ = ["execute_extension_source"]


def execute_extension_source(path: Path, module: ModuleType) -> None:
    """Load the current bytes even when the source's mtime and size match."""
    code = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    exec(code, vars(module))
