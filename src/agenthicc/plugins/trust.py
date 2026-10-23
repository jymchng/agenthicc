"""Digest-bound trust for Python extensions (PRD-250 S-02).

A repository can ship Python extensions under ``.agenthicc/``: tools, slash
commands, workflows, modes, agents and CLI plugins. Importing one runs it with
the user's permissions, so agenthicc imports an extension file only when it is
trusted:

* files under a *trusted root* are trusted by location — in a normal session
  the only trusted root is the user's own ``~/.agenthicc`` directory;
* any other file needs a trust record binding its resolved path to the SHA-256
  of its current bytes. Editing the file invalidates the record.

Records live in ``~/.agenthicc/trust.json`` — never in the repository, which
could otherwise forge its own trust — and are managed with ``agenthicc trust``.
An untrusted extension is reported and skipped; it is never imported.

Every loader asks :func:`current_extension_trust`, which returns the user's
policy unless a caller installed another one with
:func:`override_extension_trust` (the test suite installs a policy that also
trusts its temporary directories).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "ExtensionTrust",
    "TrustRecord",
    "UntrustedExtension",
    "current_extension_trust",
    "override_extension_trust",
]

log = logging.getLogger(__name__)

_STORE_VERSION = 1


def _user_root() -> Path:
    return Path.home() / ".agenthicc"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class TrustRecord:
    """One trusted extension file, bound to the content that was reviewed."""

    path: str
    sha256: str
    kind: str
    trusted_at: str


@dataclass(frozen=True)
class UntrustedExtension:
    """An extension file a loader skipped because it is not trusted."""

    path: Path
    kind: str
    reason: str


class ExtensionTrust:
    """Decides whether an extension file may be imported, and records trust."""

    def __init__(
        self,
        *,
        store_path: Path | None = None,
        trusted_roots: Sequence[Path] | None = None,
    ) -> None:
        user_root = _user_root()
        self._store_path = store_path if store_path is not None else user_root / "trust.json"
        roots = trusted_roots if trusted_roots is not None else (user_root,)
        self._trusted_roots = tuple(root.expanduser().resolve() for root in roots)

    @property
    def store_path(self) -> Path:
        return self._store_path

    @property
    def trusted_roots(self) -> tuple[Path, ...]:
        return self._trusted_roots

    # ── decisions ────────────────────────────────────────────────────────────

    def check(self, path: Path, *, kind: str) -> UntrustedExtension | None:
        """Return why *path* may not be imported, or ``None`` if it may."""
        resolved = path.expanduser().resolve()
        if any(resolved.is_relative_to(root) for root in self._trusted_roots):
            return None
        record = self._records().get(str(resolved))
        if record is None:
            return UntrustedExtension(resolved, kind, "not trusted")
        try:
            current = _digest(resolved)
        except OSError as exc:
            return UntrustedExtension(resolved, kind, f"unreadable: {exc}")
        if current != record.sha256:
            return UntrustedExtension(resolved, kind, "changed since it was trusted")
        return None

    def permits(self, path: Path, *, kind: str) -> bool:
        """True when *path* may be imported; logs the reason when it may not."""
        refusal = self.check(path, kind=kind)
        if refusal is None:
            return True
        log.warning(
            "Skipping untrusted %s extension %s (%s); review it, then run `agenthicc trust add %s`",
            refusal.kind,
            refusal.path,
            refusal.reason,
            refusal.path,
        )
        return False

    # ── records ──────────────────────────────────────────────────────────────

    def trust(self, path: Path, *, kind: str) -> TrustRecord:
        """Trust the current content of *path*."""
        resolved = path.expanduser().resolve()
        record = TrustRecord(
            path=str(resolved),
            sha256=_digest(resolved),
            kind=kind,
            trusted_at=datetime.now(timezone.utc).isoformat(),
        )
        records = self._records()
        records[record.path] = record
        self._write(records)
        return record

    def revoke(self, path: Path) -> bool:
        """Forget the record for *path*; return whether one existed."""
        records = self._records()
        removed = records.pop(str(path.expanduser().resolve()), None)
        if removed is not None:
            self._write(records)
        return removed is not None

    def records(self) -> tuple[TrustRecord, ...]:
        """Every trust record, sorted by path."""
        return tuple(record for _path, record in sorted(self._records().items()))

    def _records(self) -> dict[str, TrustRecord]:
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # An unreadable store trusts nothing; it is never treated as empty
            # and overwritten silently, so report it.
            log.error("Ignoring unreadable extension trust store %s: %s", self._store_path, exc)
            return {}
        entries = raw.get("records") if isinstance(raw, dict) else None
        records: dict[str, TrustRecord] = {}
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            fields = {key: entry.get(key) for key in ("path", "sha256", "kind", "trusted_at")}
            if all(isinstance(value, str) for value in fields.values()):
                record = TrustRecord(
                    path=str(fields["path"]),
                    sha256=str(fields["sha256"]),
                    kind=str(fields["kind"]),
                    trusted_at=str(fields["trusted_at"]),
                )
                records[record.path] = record
        return records

    def _write(self, records: dict[str, TrustRecord]) -> None:
        payload = {
            "version": _STORE_VERSION,
            "records": [
                {
                    "path": record.path,
                    "sha256": record.sha256,
                    "kind": record.kind,
                    "trusted_at": record.trusted_at,
                }
                for _path, record in sorted(records.items())
            ],
        }
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".trust-", suffix=".json", dir=self._store_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._store_path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


_CURRENT: contextvars.ContextVar[ExtensionTrust | None] = contextvars.ContextVar(
    "agenthicc_extension_trust", default=None
)


def current_extension_trust() -> ExtensionTrust:
    """The trust policy loaders consult: the user's, unless overridden."""
    return _CURRENT.get() or ExtensionTrust()


@contextmanager
def override_extension_trust(trust: ExtensionTrust) -> Iterator[None]:
    """Install *trust* for loaders running in the current context."""
    token = _CURRENT.set(trust)
    try:
        yield
    finally:
        _CURRENT.reset(token)
