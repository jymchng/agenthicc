---
title: "PRD-27: Tool Plugin Security — Sandboxing, Permissions, and Audit"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-24-tool-plugin-discovery.md, prd-25-tool-plugin-registration.md
---

# PRD-27: Tool Plugin Security

## Executive Summary

Plugin tool files are arbitrary Python executed with the user's full OS
permissions.  Compared to built-in tools (which are audited, versioned, and
distributed with agenthicc), plugin files introduce unknown code from the
project directory.  This PRD specifies the **minimal viable security model**:
trust signals, opt-in restrictions, runtime resource limits, and an audit log.
It intentionally does not attempt VM-level sandboxing (too complex, breaks
`asyncio`); instead it focuses on transparency and explicit consent.

---

## Goals

| ID | Goal |
|----|------|
| G1 | First-time plugin load prints a one-time **trust prompt** listing each file before executing |
| G2 | Trusted file hashes are stored in `.agenthicc/trusted_plugins.json`; re-trust only on hash change |
| G3 | `agenthicc.toml` `[plugins] auto_trust = true` skips prompts (CI-friendly) |
| G4 | Each plugin tool call is appended to `.agenthicc/plugin_audit.jsonl` |
| G5 | `[plugins] allowed_modules` restricts which stdlib/third-party modules plugins may import |
| G6 | `[plugins] timeout_seconds` sets a per-call timeout for plugin tools (default 30 s) |
| G7 | `[plugins] disabled = ["weather_tools"]` prevents specific plugin files from loading |
| G8 | A plugin that raises `SecurityViolation` returns a tool error and logs the event |
| G9 | Missing dependencies are surfaced with a clear install hint; `auto_install = true` installs them silently |
| G10 | An interactive install prompt (`[I]nstall  [S]kip  [Q]uit`) is shown for missing deps when not in auto mode |

## Non-Goals
- Full process-level sandboxing (future: `seccomp`, `bwrap`, WebAssembly)
- Network egress filtering (use OS-level firewall or proxy)
- Code-signing of plugin files

---

## 1. Trust Model

### First-Time Load Prompt

When `.agenthicc/tools/weather_tools.py` is encountered for the first time
(no entry in `trusted_plugins.json`) or its SHA-256 has changed:

```
⚠  New plugin tool file detected:
   .agenthicc/tools/weather_tools.py (1,234 bytes, sha256=ab12…)

   This file contains Python code that will run with your permissions.
   Only trust files you wrote or have reviewed.

   [T]rust once  [A]lways trust  [S]kip this file  [Q]uit  > _
```

- **Trust once** — loads for this session; does not write to `trusted_plugins.json`
- **Always trust** — writes the hash to `trusted_plugins.json`; future loads auto-approve
- **Skip** — file is not loaded; session continues without it
- **Quit** — exits agenthicc

### `trusted_plugins.json`

```json
{
  "version": 1,
  "trusted": {
    ".agenthicc/tools/weather_tools.py": {
      "sha256": "ab12cd34ef56...",
      "trusted_at": "2026-06-12T10:00:00Z",
      "absolute_path": "/home/alice/projects/myapp/.agenthicc/tools/weather_tools.py"
    }
  }
}
```

- Stored in `.agenthicc/trusted_plugins.json` (project-local, committed to VCS is fine)
- `~/.agenthicc/trusted_plugins.json` for user-global plugins
- Hash mismatch → re-prompt

### Auto-Trust Mode

```toml
[plugins]
auto_trust = true    # skip trust prompts; load all discovered plugins
```

Intended for CI pipelines or fully-controlled environments.  A warning is
printed at session startup when `auto_trust = true`.

---

## 1b. Dependency Install Prompt

When a plugin declares `DEPENDENCIES` (or the AST scan infers missing imports)
and `auto_install = false`, the user sees an interactive prompt **before** the
file is skipped — giving them the option to install right now without restarting:

```
⚠  Plugin .agenthicc/tools/weather_tools.py requires missing packages:
     httpx>=0.27

   [I]nstall now  [S]kip this plugin  [Q]uit  > _
```

- **Install now** — runs `pip install httpx>=0.27` in the current environment,
  then retries loading the file.  If install succeeds, the plugin loads normally
  for this session.  The installed package persists (no cleanup); the dependency
  will be satisfied on future runs without prompting.
- **Skip** — the plugin is not loaded; session continues without it.
- **Quit** — exits agenthicc.

When `auto_install = true`, the prompt is skipped and install runs silently with
a log line:

```
[plugins] auto_install enabled — installing httpx>=0.27 for weather_tools.py
```

When `interactive = false` (headless / `--headless` mode), missing deps cause
the plugin to be skipped with a warning; no prompt, no auto-install (even if
`auto_install = true`).  This prevents CI pipelines from hanging on a prompt
or mutating their environment unexpectedly.

### Implementation

```python
# src/agenthicc/plugins/deps.py  (new file, companion to trust.py)

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def prompt_install(
    path: Path,
    missing: list[str],
    *,
    auto_install: bool = False,
    install_target: str = "venv",
    interactive: bool = True,
) -> bool:
    """Handle missing dependencies for *path*.

    Returns True if the caller should retry loading (deps installed),
    False if the plugin should be skipped.
    """
    if not missing:
        return True

    if not interactive:
        log.warning(
            "Plugin %s skipped — missing: %s  (headless mode, skipping install)",
            path, missing,
        )
        return False

    if auto_install:
        log.info("[plugins] auto_install — installing %s for %s", missing, path.name)
        _run_install(missing, target=install_target)
        return True

    # Interactive prompt
    deps_str = " ".join(missing)
    print(
        f"\n⚠  Plugin {path} requires missing packages:\n"
        f"     {', '.join(missing)}\n"
    )
    while True:
        choice = input("   [I]nstall now  [S]kip  [Q]uit  > ").strip().upper()
        if choice == "I":
            _run_install(missing, target=install_target)
            return True   # caller retries _load_plugin_file
        if choice == "S":
            return False
        if choice == "Q":
            raise SystemExit(0)


def _run_install(requirements: list[str], target: str = "venv") -> None:
    flags = ["--user"] if target == "user" else []
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *flags, *requirements]
    )
```

### Updated `_scan_directory()` flow

```python
from agenthicc.plugins.deps import prompt_install

def _scan_directory(root, cfg=None):
    cfg = cfg or PluginSettings()
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name.startswith("_") or py_file.stem in cfg.disabled:
            continue
        result = _load_plugin_file(py_file, auto_install=False)  # never auto-install here
        if result.missing_deps:
            retry = prompt_install(
                py_file,
                result.missing_deps,
                auto_install=cfg.auto_install,
                install_target=cfg.install_target,
                interactive=True,
            )
            if retry:
                result = _load_plugin_file(py_file, auto_install=False)  # retry after install
        ...  # log errors, append result
```

---

## 2. Implementation: Trust Check

```python
# src/agenthicc/plugins/trust.py

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

TrustDecision = Literal["trust_once", "always_trust", "skip", "quit"]

_TRUST_FILE = ".agenthicc/trusted_plugins.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_trusted(trust_file: Path) -> dict:
    if not trust_file.exists():
        return {}
    try:
        return json.loads(trust_file.read_text())
    except Exception:
        return {}


def _save_trusted(trust_file: Path, data: dict) -> None:
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    trust_file.write_text(json.dumps(data, indent=2))


def check_trust(
    path: Path,
    *,
    auto_trust: bool = False,
    trust_file: Path | None = None,
    interactive: bool = True,
) -> TrustDecision:
    """Return the trust decision for a plugin file.

    Args:
        path: Absolute path to the plugin file.
        auto_trust: If True, always return "always_trust" without prompting.
        trust_file: Override location of trusted_plugins.json.
        interactive: If False (CI / headless), auto-skip untrusted files.
    """
    tf = trust_file or Path(_TRUST_FILE)
    current_hash = _sha256(path)
    trusted = _load_trusted(tf)

    key = str(path)
    entry = trusted.get(key)
    if entry and entry.get("sha256") == current_hash:
        return "trust_once"   # already trusted, same hash

    if auto_trust:
        log.warning("auto_trust enabled — loading %s without prompt", path)
        _record_trust(tf, trusted, key, current_hash, decision="always_trust")
        return "always_trust"

    if not interactive:
        log.warning("Headless mode — skipping untrusted plugin %s", path)
        return "skip"

    # Interactive prompt
    size = path.stat().st_size
    print(
        f"\n⚠  New plugin tool file detected:\n"
        f"   {path}  ({size:,} bytes, sha256={current_hash[:16]}…)\n\n"
        f"   This file contains Python code that will run with your permissions.\n"
        f"   Only trust files you wrote or have reviewed.\n"
    )
    while True:
        choice = input("   [T]rust once  [A]lways trust  [S]kip  [Q]uit  > ").strip().upper()
        if choice == "T":
            return "trust_once"
        if choice == "A":
            _record_trust(tf, trusted, key, current_hash, decision="always_trust")
            return "always_trust"
        if choice == "S":
            return "skip"
        if choice == "Q":
            return "quit"


def _record_trust(
    tf: Path,
    data: dict,
    key: str,
    sha256: str,
    *,
    decision: str,
) -> None:
    data.setdefault("version", 1)
    data.setdefault("trusted", {})[key] = {
        "sha256": sha256,
        "trusted_at": datetime.now(timezone.utc).isoformat(),
        "absolute_path": key,
        "decision": decision,
    }
    _save_trusted(tf, data)
```

---

## 3. Audit Log

Every plugin tool call is appended to `.agenthicc/plugin_audit.jsonl`:

```jsonl
{"ts": "2026-06-12T10:05:22Z", "agent": "researcher", "tool": "search_arxiv", "args": {"query": "attention", "max_results": 5}, "ok": true, "duration_ms": 1203}
{"ts": "2026-06-12T10:05:30Z", "agent": "default", "tool": "get_current_weather", "args": {"city": "London"}, "ok": false, "error": "TimeoutError: 30s exceeded"}
```

```python
# src/agenthicc/plugins/audit.py

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_AUDIT_FILE = Path(".agenthicc/plugin_audit.jsonl")


def record_call(
    agent_name: str,
    tool_name: str,
    args: dict[str, Any],
    ok: bool,
    duration_ms: float,
    error: str | None = None,
    audit_file: Path | None = None,
) -> None:
    """Append one audit record for a completed plugin tool call."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent_name,
        "tool": tool_name,
        "args": args,
        "ok": ok,
        "duration_ms": round(duration_ms, 1),
    }
    if error:
        entry["error"] = error

    target = audit_file or _AUDIT_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as exc:
        log.warning("Failed to write plugin audit log: %s", exc)
```

The audit hook is installed inside `AgenthiccToolExecutor.execute()` for any
tool whose `__module__` indicates it was loaded from a plugin file
(`_agenthicc_plugin_` prefix in the module name — see PRD-24 loader).

---

## 4. Module Import Restrictions (`allowed_modules`)

```toml
[plugins]
allowed_modules = ["httpx", "pathlib", "json", "os.path", "datetime"]
# If unset, no restriction (default).
```

When set, the loader wraps `builtins.__import__` for the duration of the
plugin file's execution:

```python
# src/agenthicc/plugins/discovery.py  (amended)

import builtins
import sys


def _restricted_import(allowed: frozenset[str]):
    original = builtins.__import__

    def _guarded(name, *args, **kwargs):
        root = name.split(".")[0]
        if root not in allowed and root not in sys.stdlib_module_names:
            raise ImportError(
                f"Plugin import of '{name}' is not allowed. "
                f"Add it to [plugins] allowed_modules in agenthicc.toml."
            )
        return original(name, *args, **kwargs)

    return _guarded


# Usage in _load_plugin_file() when allowed_modules is configured:
# builtins.__import__ = _restricted_import(frozenset(cfg.plugins.allowed_modules))
# try:
#     spec.loader.exec_module(module)
# finally:
#     builtins.__import__ = original_import
```

---

## 5. `[plugins]` Config Section

```toml
[plugins]
auto_trust = false                          # require explicit trust prompts
auto_install = false                        # auto-install missing deps via pip
install_target = "venv"                     # "venv" (current env) or "user" (--user flag)
allowed_modules = []                        # empty = no restriction
timeout_seconds = 30.0                      # per-call timeout for plugin tools
disabled = ["old_crm_tools", "broken_api"] # file stems to skip entirely
trust_file = ".agenthicc/trusted_plugins.json"
audit_file = ".agenthicc/plugin_audit.jsonl"
```

### `PluginSettings` dataclass

```python
# src/agenthicc/config.py  (addition)

from dataclasses import dataclass, field


@dataclass
class PluginSettings:
    auto_trust: bool = False
    auto_install: bool = False          # pip-install missing deps without prompting
    install_target: str = "venv"        # "venv" or "user"
    allowed_modules: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    disabled: list[str] = field(default_factory=list)
    trust_file: str = ".agenthicc/trusted_plugins.json"
    audit_file: str = ".agenthicc/plugin_audit.jsonl"
```

`AgenthiccConfig` gains a `plugins: PluginSettings` field alongside
`execution`, `memory`, etc.

---

## 6. Integration Points

### `_scan_directory()` (PRD-24) — add trust + disabled checks

```python
def _scan_directory(root: Path, cfg: PluginSettings | None = None) -> list[LoadResult]:
    cfg = cfg or PluginSettings()
    for py_file in sorted(root.rglob("*.py")):
        if py_file.stem in cfg.disabled:
            log.info("Plugin %s disabled by config — skipping", py_file)
            continue
        decision = check_trust(py_file, auto_trust=cfg.auto_trust)
        if decision == "quit":
            raise SystemExit(0)
        if decision == "skip":
            continue
        result = _load_plugin_file(py_file)
        ...
```

### `AgenthiccToolExecutor.execute()` — add audit hook

```python
# After the tool call completes in execute():
from agenthicc.plugins.audit import record_call

if _is_plugin_tool(tool):   # check __module__ prefix
    record_call(
        agent_name=ctx.get("agent_name", "default"),
        tool_name=tool.name,
        args=args,
        ok=env.ok,
        duration_ms=env.duration_ms or 0,
        error=env.error,
    )
```

---

## Tests

```python
# tests/unit/test_plugin_trust.py

import pytest
from pathlib import Path
from unittest.mock import patch
from agenthicc.plugins.trust import check_trust, _sha256

pytestmark = pytest.mark.unit


def test_known_hash_skips_prompt(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("x = 1\n")
    tf = tmp_path / "trusted.json"
    h = _sha256(f)
    tf.write_text(
        f'{{"version":1,"trusted":{{"{f}":{{"sha256":"{h}"}}}}}}'
    )
    decision = check_trust(f, trust_file=tf, interactive=True)
    assert decision == "trust_once"   # hash matches → no prompt


def test_auto_trust_skips_prompt(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("x = 1\n")
    tf = tmp_path / "trusted.json"
    decision = check_trust(f, auto_trust=True, trust_file=tf)
    assert decision in ("trust_once", "always_trust")


def test_headless_mode_skips_untrusted(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("x = 1\n")
    tf = tmp_path / "trusted.json"
    decision = check_trust(f, interactive=False, trust_file=tf)
    assert decision == "skip"


# tests/unit/test_plugin_audit.py

from agenthicc.plugins.audit import record_call


def test_record_call_writes_jsonl(tmp_path):
    import json
    audit = tmp_path / "audit.jsonl"
    record_call(
        agent_name="researcher",
        tool_name="search_arxiv",
        args={"query": "llm"},
        ok=True,
        duration_ms=500.0,
        audit_file=audit,
    )
    lines = audit.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool"] == "search_arxiv"
    assert record["ok"] is True


# tests/unit/test_plugin_deps.py

from unittest.mock import patch, MagicMock
from pathlib import Path
from agenthicc.plugins.deps import prompt_install

pytestmark = pytest.mark.unit


def test_prompt_install_headless_skips(tmp_path):
    """Headless mode must never block or install."""
    result = prompt_install(
        tmp_path / "t.py",
        ["httpx>=0.27"],
        auto_install=True,   # even with auto_install=True ...
        interactive=False,   # ... headless wins
    )
    assert result is False   # skip


def test_prompt_install_auto_install_calls_pip(tmp_path):
    with patch("agenthicc.plugins.deps._run_install") as mock_install:
        result = prompt_install(
            tmp_path / "t.py",
            ["httpx>=0.27"],
            auto_install=True,
            interactive=True,
        )
    assert result is True
    mock_install.assert_called_once_with(["httpx>=0.27"], target="venv")


def test_prompt_install_interactive_install_choice(tmp_path):
    with patch("builtins.input", return_value="I"), \
         patch("agenthicc.plugins.deps._run_install") as mock_install:
        result = prompt_install(
            tmp_path / "t.py",
            ["requests"],
            auto_install=False,
            interactive=True,
        )
    assert result is True
    mock_install.assert_called_once()


def test_prompt_install_interactive_skip_choice(tmp_path):
    with patch("builtins.input", return_value="S"):
        result = prompt_install(
            tmp_path / "t.py",
            ["requests"],
            auto_install=False,
            interactive=True,
        )
    assert result is False


def test_prompt_install_quit_raises_system_exit(tmp_path):
    with patch("builtins.input", return_value="Q"), pytest.raises(SystemExit):
        prompt_install(
            tmp_path / "t.py",
            ["requests"],
            auto_install=False,
            interactive=True,
        )
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_plugin_trust.py \
                                 tests/unit/test_plugin_audit.py -v

# Manual: first-time load should prompt
rm -f .agenthicc/trusted_plugins.json
uv run agenthicc
# → ⚠ New plugin tool file detected: .agenthicc/tools/weather_tools.py
# → [T]rust once → session starts normally

# Headless / CI
AGENTHICC_PLUGINS_AUTO_TRUST=true uv run agenthicc --headless
# → logs "auto_trust enabled" warning, loads all plugins without prompts

# Audit log
tail -f .agenthicc/plugin_audit.jsonl
# → shows one JSONL line per plugin tool call
```
