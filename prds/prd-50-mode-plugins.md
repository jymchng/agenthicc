---
title: "PRD-50: Mode Plugins — User-Defined Modes via .agenthicc/modes/"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-47-mode-system-architecture.md
---

# PRD-50: Mode Plugins

## Executive Summary

Users can define project-specific (or user-global) operational modes by placing
a Python file in `.agenthicc/modes/`.  The file exports a `MODE` (or `MODES`)
object.  agenthicc loads these at session startup and they appear in the
Shift+Tab cycle alongside the built-in modes.

The pattern exactly mirrors the tool plugin convention (PRD-24) and command
plugin convention (PRD-46) for consistency.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `.agenthicc/modes/<name>.py` defines a project-scoped custom mode |
| G2 | `~/.agenthicc/modes/<name>.py` defines a user-global custom mode |
| G3 | Each file must export `MODE: Mode` or `MODES: list[Mode]` |
| G4 | Files that fail to load are skipped with a warning; session continues |
| G5 | Custom modes appear in the Shift+Tab cycle after the built-in modes |
| G6 | Project-local modes shadow user-global modes with the same name |
| G7 | `DEPENDENCIES: list[str]` declares required packages (same as PRD-24/46) |
| G8 | The trust model from PRD-27 applies — first-time load prompts for trust |
| G9 | A mode plugin may override a built-in mode by registering the same name |

---

## Filesystem Layout

```
~/.agenthicc/
└── modes/
    ├── focused.py          # personal "focused" mode (no distractions)
    └── customer_call.py    # gentle, clear explanations for live demos

.agenthicc/
└── modes/
    ├── strict.py           # enforce project coding standards
    ├── migration.py        # database migration assistant mode
    └── no_tests.py         # skip test generation (not recommended)
```

---

## Mode Plugin File Contract

Every mode plugin file **must** export:

- `MODE: Mode` — a single mode, OR
- `MODES: list[Mode]` — multiple modes

It **may** export:

- `DEPENDENCIES: list[str]` — PEP-508 requirements

### Minimal example

```python
# .agenthicc/modes/strict.py
"""Mode: enforce the project's architectural rules."""

from agenthicc.modes import Mode

_PATCH = """\
[MODE: STRICT]
You must follow these project-specific rules in every response:
1. All new code must have type hints.
2. No `print()` statements — use the logging module.
3. Functions longer than 20 lines must be refactored.
4. Every new public function needs a docstring.
If a user request would violate these rules, explain why and propose a
compliant alternative.
"""

MODE = Mode(
    name="Strict",
    label="STRICT",
    description="Enforces project coding standards",
    colour="yellow",
    system_patch=_PATCH,
    source_id="mode-plugin:strict",
)
```

### Read-only research mode example

```python
# .agenthicc/modes/research.py
from agenthicc.modes import Mode

_PATCH = """\
[MODE: RESEARCH]
You are a research assistant. Do not write or modify any files.
Focus exclusively on gathering information, reading existing code,
and synthesising what you find into a clear summary report.
Conclude with a "Findings" section and a "Recommended Next Steps" section.
"""

MODE = Mode(
    name="Research",
    label="RSCH",
    description="Read-only research — no file writes",
    colour="blue",
    system_patch=_PATCH,
    tool_filter=lambda name, _: not any(
        name.startswith(p) for p in ("write", "append", "delete", "patch",
                                      "move", "copy", "make", "run", "shell",
                                      "git_add", "git_commit")
    ),
    source_id="mode-plugin:research",
)
```

### Mode with a pre-flight hook

```python
# .agenthicc/modes/timed.py
"""Wrap every request with a timing report."""

from agenthicc.modes import Mode

def _pre(text, renderer):
    return text   # no modification, but could add timing context

def _post(content, renderer):
    import time
    s = getattr(renderer, "_status", None)
    if s:
        elapsed = time.monotonic() - s.intent_started_at if s.intent_started_at else 0
        return content + f"\n\n*(completed in {elapsed:.1f}s)*"
    return content

MODE = Mode(
    name="Timed",
    label="TIME",
    description="Appends elapsed time to every response",
    colour="cyan",
    pre_hook=_pre,
    post_hook=_post,
    source_id="mode-plugin:timed",
)
```

---

## Loader Implementation

```python
# src/agenthicc/modes/plugin_loader.py

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .mode import Mode

log = logging.getLogger(__name__)

__all__ = ["ModeLoadResult", "ModePluginSet", "discover_mode_plugins"]


@dataclass
class ModeLoadResult:
    path: Path
    modes: list[Mode] = field(default_factory=list)
    error: str | None = None
    missing_deps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing_deps


@dataclass
class ModePluginSet:
    results: list[ModeLoadResult] = field(default_factory=list)

    @property
    def all_modes(self) -> list[Mode]:
        return [m for r in self.results for m in r.modes if r.ok]

    @property
    def failed(self) -> list[ModeLoadResult]:
        return [r for r in self.results if not r.ok]


def _check_missing(requirements: list[str]) -> list[str]:
    import importlib.metadata, re  # noqa: PLC0415
    missing = []
    for req in requirements:
        pkg = re.split(r"[>=<!~\[]", req)[0].strip()
        try:
            importlib.metadata.version(pkg)
        except Exception:
            missing.append(req)
    return missing


def _load_mode_file(path: Path) -> ModeLoadResult:
    module_name = f"_agenthicc_mode_{path.stem}_{abs(hash(str(path)))}"

    # Probe for DEPENDENCIES
    declared_deps: list[str] = []
    try:
        probe = importlib.util.spec_from_file_location(f"{module_name}_probe", path)
        if probe and probe.loader:
            pm = importlib.util.module_from_spec(probe)
            probe.loader.exec_module(pm)  # type: ignore[union-attr]
            declared_deps = list(getattr(pm, "DEPENDENCIES", []))
    except Exception:
        pass

    if declared_deps:
        missing = _check_missing(declared_deps)
        if missing:
            return ModeLoadResult(path=path, missing_deps=missing)

    # Full import
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return ModeLoadResult(path=path, error="could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        return ModeLoadResult(path=path, error=f"{type(exc).__name__}: {exc}")

    single = getattr(module, "MODE", None)
    multi  = getattr(module, "MODES", None)

    if single is None and multi is None:
        return ModeLoadResult(path=path, modes=[])   # no export — skip silently

    modes: list[Mode] = []
    if single is not None:
        if isinstance(single, Mode):
            modes.append(single)
        else:
            return ModeLoadResult(path=path, error="MODE must be a Mode instance")
    if multi is not None:
        if not isinstance(multi, (list, tuple)):
            return ModeLoadResult(path=path, error="MODES must be a list")
        for item in multi:
            if isinstance(item, Mode):
                modes.append(item)
            else:
                log.warning("Mode plugin %s: non-Mode item in MODES skipped: %r", path, item)

    # Auto-set source_id when the default was left unchanged
    for m in modes:
        if m.source_id == "builtin":
            object.__setattr__(m, "source_id", f"mode-plugin:{path.stem}")

    return ModeLoadResult(path=path, modes=modes)


def _scan_modes_dir(root: Path) -> list[ModeLoadResult]:
    if not root.is_dir():
        return []
    results: list[ModeLoadResult] = []
    for py_file in sorted(root.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        result = _load_mode_file(py_file)
        if result.missing_deps:
            log.warning(
                "Mode plugin %s skipped — missing: %s\n  Fix: pip install %s",
                py_file, result.missing_deps, " ".join(result.missing_deps),
            )
        elif result.error:
            log.error("Mode plugin %s failed to load: %s", py_file, result.error)
        elif result.modes:
            log.debug("Loaded mode(s) from %s: %s",
                      py_file, [m.name for m in result.modes])
        results.append(result)
    return results


def discover_mode_plugins(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> ModePluginSet:
    """Discover mode plugins from user-global then project-local directories."""
    user_root    = (user_dir    or Path.home() / ".agenthicc") / "modes"
    project_root = (project_dir or Path(".agenthicc"))         / "modes"

    results: list[ModeLoadResult] = []
    results.extend(_scan_modes_dir(user_root))
    results.extend(_scan_modes_dir(project_root))
    return ModePluginSet(results=results)
```

---

## Session Startup Integration

```python
# In InlineRenderer.run() — after building the mode registry from builtins:

from agenthicc.modes.plugin_loader import discover_mode_plugins  # noqa: PLC0415

_mode_plugins = discover_mode_plugins(
    project_dir=_Path(".agenthicc"),
    user_dir=_Path.home() / ".agenthicc",
)
for mode in _mode_plugins.all_modes:
    _mode_registry.register(mode)   # project-local overrides user-global

if _mode_plugins.all_modes:
    from rich.console import Console as _RC
    names = ", ".join(m.name for m in _mode_plugins.all_modes)
    _RC().print(f"[dim]Loaded {len(_mode_plugins.all_modes)} mode plugin(s): {names}[/dim]")
```

---

## Conflict Resolution

Plugin modes are registered AFTER built-in modes.  If a plugin mode has the
same name as a built-in (e.g. a project overrides `Plan` with stricter rules),
`ModeRegistry.register()` replaces the existing entry in-place — preserving
the original cycle position.  This allows customisation without reordering.

---

## Quick-Start

```bash
mkdir -p .agenthicc/modes

cat > .agenthicc/modes/strict.py << 'EOF'
from agenthicc.modes import Mode

MODE = Mode(
    name="Strict",
    label="STRICT",
    description="Enforce project coding standards",
    colour="yellow",
    system_patch="""
[MODE: STRICT]
All new code must have type hints, docstrings, and unit tests.
Refactor any function longer than 20 lines.
""",
)
EOF

uv run agenthicc
# Startup: "Loaded 1 mode plugin(s): Strict"
# Press Shift+Tab until "[STRICT] ❯" appears
# Ask: "add a login function" → agent follows strict rules
```

---

## Tests

```python
# tests/unit/test_mode_plugins.py  (pytestmark = pytest.mark.unit)

def test_load_single_mode(tmp_path):
    f = tmp_path / "custom.py"
    f.write_text(
        "from agenthicc.modes import Mode\n"
        "MODE = Mode('Custom', 'CUST', 'A custom mode')\n"
    )
    from agenthicc.modes.plugin_loader import _load_mode_file
    result = _load_mode_file(f)
    assert result.ok
    assert len(result.modes) == 1
    assert result.modes[0].name == "Custom"


def test_load_multiple_modes(tmp_path):
    f = tmp_path / "multi.py"
    f.write_text(
        "from agenthicc.modes import Mode\n"
        "MODES = [Mode('M1', 'M1', 'd1'), Mode('M2', 'M2', 'd2')]\n"
    )
    from agenthicc.modes.plugin_loader import _load_mode_file
    result = _load_mode_file(f)
    assert result.ok
    assert {m.name for m in result.modes} == {"M1", "M2"}


def test_source_id_derived_from_stem(tmp_path):
    f = tmp_path / "my_mode.py"
    f.write_text("from agenthicc.modes import Mode\nMODE = Mode('X', 'X', '')\n")
    from agenthicc.modes.plugin_loader import _load_mode_file
    result = _load_mode_file(f)
    assert result.modes[0].source_id == "mode-plugin:my_mode"


def test_syntax_error_captured(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def bad syntax!!!\n")
    from agenthicc.modes.plugin_loader import _load_mode_file
    result = _load_mode_file(f)
    assert not result.ok


def test_private_files_skipped(tmp_path):
    d = tmp_path / "modes"
    d.mkdir()
    (d / "_helper.py").write_text("from agenthicc.modes import Mode\nMODE=Mode('H','H','')\n")
    from agenthicc.modes.plugin_loader import discover_mode_plugins
    ps = discover_mode_plugins(project_dir=tmp_path)
    assert ps.all_modes == []


def test_plugin_appears_in_cycle(tmp_path):
    f = tmp_path / "modes" / "research.py"
    f.parent.mkdir()
    f.write_text("from agenthicc.modes import Mode\nMODE=Mode('Research','RSCH','Research mode')\n")
    from agenthicc.modes import build_default_registry, ModeManager
    from agenthicc.modes.plugin_loader import discover_mode_plugins
    reg = build_default_registry()
    ps = discover_mode_plugins(project_dir=tmp_path)
    for m in ps.all_modes:
        reg.register(m)
    names = [m.name for m in reg]
    assert "Research" in names


def test_plugin_overrides_builtin(tmp_path):
    """A plugin with the same name as a builtin replaces it in-place."""
    f = tmp_path / "modes" / "Plan.py"
    f.parent.mkdir()
    f.write_text(
        "from agenthicc.modes import Mode\n"
        "MODE = Mode('Plan', 'PLAN+', 'Custom stricter plan mode')\n"
    )
    from agenthicc.modes import build_default_registry
    from agenthicc.modes.plugin_loader import discover_mode_plugins
    reg = build_default_registry()
    original_pos = [m.name for m in reg].index("Plan")
    ps = discover_mode_plugins(project_dir=tmp_path)
    for m in ps.all_modes:
        reg.register(m)
    # Same position, replaced content
    assert [m.name for m in reg].index("Plan") == original_pos
    assert reg.get("Plan").description == "Custom stricter plan mode"
```
