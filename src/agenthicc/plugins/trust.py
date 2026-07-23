from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

log = logging.getLogger(__name__)

TrustDecision = Literal["trust_once", "always_trust", "skip", "quit"]

_TRUST_FILE = ".agenthicc/trusted_plugins.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_trusted(trust_file: Path) -> dict[str, object]:
    if not trust_file.exists():
        return {}
    try:
        loaded = json.loads(trust_file.read_text())
        return cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_trusted(trust_file: Path, data: dict[str, object]) -> None:
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
    raw_entries = trusted.get("trusted", {})
    entries = raw_entries if isinstance(raw_entries, dict) else {}
    raw_entry = entries.get(key)
    entry = raw_entry if isinstance(raw_entry, dict) else {}
    if entry.get("sha256") == current_hash:
        return "trust_once"  # already trusted, same hash

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
    data: dict[str, object],
    key: str,
    sha256: str,
    *,
    decision: str,
) -> None:
    data.setdefault("version", 1)
    raw_entries = data.get("trusted")
    entries: dict[str, object] = raw_entries if isinstance(raw_entries, dict) else {}
    entries[key] = {
        "sha256": sha256,
        "trusted_at": datetime.now(timezone.utc).isoformat(),
        "absolute_path": key,
        "decision": decision,
    }
    data["trusted"] = entries
    _save_trusted(tf, data)
