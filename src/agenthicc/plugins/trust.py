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

The same file remembers, per directory, the answer to the first-run question
"Work in <directory>?" (TUX-03): ``allow``, or ``plan`` (work there, but start
in Plan mode). That is a decision about a *directory*, not about extension
code, so it is stored apart from the extension records and does not change
what may be imported.

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

from agenthicc.runners.process_lease import InterProcessLock, read_json_object

__all__ = [
    "ExtensionTrust",
    "TrustRecord",
    "UntrustedExtension",
    "WorkspaceDecision",
    "current_extension_trust",
    "override_extension_trust",
]

log = logging.getLogger(__name__)

_STORE_VERSION = 1


def _user_root() -> Path:
    return Path.home() / ".agenthicc"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _authored_state_directory(workspace: Path, trust_path: Path) -> Path:
    identity = hashlib.sha256(os.fsencode(workspace)).hexdigest()
    return trust_path.parent / "authored" / identity


@dataclass(frozen=True)
class TrustRecord:
    """One trusted extension file, bound to the content that was reviewed."""

    path: str
    sha256: str
    kind: str
    trusted_at: str
    artifact_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "path": self.path,
            "sha256": self.sha256,
            "kind": self.kind,
            "trusted_at": self.trusted_at,
        }
        if self.artifact_digest is not None:
            data["artifact_digest"] = self.artifact_digest
        return data

    @classmethod
    def from_payload(cls, raw: object) -> TrustRecord:
        if not isinstance(raw, dict):
            raise ValueError("Invalid trust record.")
        path = raw.get("path")
        digest = raw.get("sha256")
        kind = raw.get("kind")
        recorded_at = raw.get("trusted_at")
        artifact = raw.get("artifact_digest")
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not isinstance(kind, str)
            or not isinstance(recorded_at, str)
            or (artifact is not None and not isinstance(artifact, str))
        ):
            raise ValueError("Invalid trust record.")
        return cls(path, digest, kind, recorded_at, artifact)


@dataclass(frozen=True)
class WorkspaceDecision:
    """The user's answer to "Work in this directory?" (TUX-03)."""

    path: str
    #: ``"allow"`` or ``"plan"`` (start in Plan mode).
    decision: str
    decided_at: str


WORKSPACE_DECISIONS = ("allow", "plan")


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

    def publication_pending(self, path: Path) -> bool:
        """Keep provisional project files private to the publishing session."""
        resolved = path.resolve()
        for prefix in resolved.parents:
            if prefix.name != ".agenthicc" or prefix.parent in _PUBLISHING.get():
                continue
            pending = _authored_state_directory(prefix.parent, self.store_path) / "pending.json"
            document = read_json_object(pending)
            if document is None:
                continue
            files = document.get("files")
            relative = resolved.relative_to(prefix.parent).as_posix()
            if not isinstance(files, list) or relative in files:
                return True
        return False

    # ── decisions ────────────────────────────────────────────────────────────

    def check(self, path: Path, *, kind: str) -> UntrustedExtension | None:
        """Return why *path* may not be imported, or ``None`` if it may."""
        resolved = path.expanduser().resolve()
        if any(resolved.is_relative_to(root) for root in self._trusted_roots):
            return None
        record = self._records().get(str(resolved))
        if record is None:
            return UntrustedExtension(resolved, kind, "not trusted")
        if record.artifact_digest is not None and self.publication_pending(resolved):
            return UntrustedExtension(resolved, kind, "authored publication pending")
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

    def trust(
        self,
        path: Path,
        *,
        kind: str,
        expected_sha256: str | None = None,
        artifact_digest: str | None = None,
    ) -> TrustRecord:
        """Record a file's digest, checking approved bytes for authored artifacts."""
        resolved = path.expanduser().resolve()
        if expected_sha256 is not None and path.expanduser().absolute() != resolved:
            raise ValueError("Authored trust requires a canonical extension path.")
        if artifact_digest is not None and expected_sha256 is None:
            raise ValueError("Authored trust requires an approved file digest.")
        sha256 = _digest(resolved)
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise ValueError("The extension changed before its trust record was written.")
        record = TrustRecord(
            path=str(resolved),
            sha256=sha256,
            kind=kind,
            trusted_at=datetime.now(timezone.utc).isoformat(),
            artifact_digest=artifact_digest,
        )
        with self._lock():
            records = self._records()
            records[record.path] = record
            self._write(records)
        return record

    def revoke(self, path: Path) -> bool:
        """Forget the record for *path*; return whether one existed."""
        with self._lock():
            records = self._records()
            removed = records.pop(str(path.expanduser().resolve()), None)
            if removed is not None:
                self._write(records)
        return removed is not None

    def restore_records(self, paths: Sequence[Path], records: Sequence[TrustRecord]) -> None:
        """Replace records for *paths* with an exact previously captured snapshot."""
        scoped_paths = {str(path.expanduser().resolve()) for path in paths}
        if not scoped_paths and not records:
            return
        replacements: dict[str, TrustRecord] = {}
        for record in records:
            resolved = str(Path(record.path).expanduser().resolve())
            if resolved not in scoped_paths or record.path != resolved:
                raise ValueError("trust snapshot record is outside the requested path scope")
            replacements[resolved] = record
        with self._lock():
            current = self._records()
            for path in scoped_paths:
                current.pop(path, None)
            current.update(replacements)
            self._write(current)

    def records(self) -> tuple[TrustRecord, ...]:
        """Every trust record, sorted by path."""
        return tuple(record for _path, record in sorted(self._records().items()))

    # ── workspace decisions (TUX-03) ─────────────────────────────────────────

    def workspace_decision(self, directory: Path) -> WorkspaceDecision | None:
        """What the user decided about working in *directory*, if they were asked."""
        return self._workspaces().get(str(directory.expanduser().resolve()))

    def remember_workspace(self, directory: Path, decision: str) -> WorkspaceDecision:
        """Record that the user chose *decision* (``allow`` or ``plan``) for *directory*."""
        if decision not in WORKSPACE_DECISIONS:
            raise ValueError(f"workspace decision must be one of: {', '.join(WORKSPACE_DECISIONS)}")
        record = WorkspaceDecision(
            path=str(directory.expanduser().resolve()),
            decision=decision,
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock():
            workspaces = self._workspaces()
            workspaces[record.path] = record
            self._write(self._records(), workspaces)
        return record

    def _lock(self) -> InterProcessLock:
        return InterProcessLock(self._store_path.with_suffix(".lock"))

    def _workspaces(self) -> dict[str, WorkspaceDecision]:
        entries = self._document().get("workspaces")
        decisions: dict[str, WorkspaceDecision] = {}
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            path, decision, decided_at = (
                entry.get(key) for key in ("path", "decision", "decided_at")
            )
            if (
                isinstance(path, str)
                and isinstance(decided_at, str)
                and isinstance(decision, str)
                and decision in WORKSPACE_DECISIONS
            ):
                decisions[path] = WorkspaceDecision(path, decision, decided_at)
        return decisions

    def _document(self) -> dict[str, object]:
        """The store's JSON object; empty when it is missing or unreadable."""
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # An unreadable store trusts nothing; it is never treated as empty
            # and overwritten silently, so report it.
            log.error("Ignoring unreadable extension trust store %s: %s", self._store_path, exc)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _records(self) -> dict[str, TrustRecord]:
        entries = self._document().get("records")
        records: dict[str, TrustRecord] = {}
        for entry in entries if isinstance(entries, list) else ():
            try:
                record = TrustRecord.from_payload(entry)
            except ValueError:
                continue
            records[record.path] = record
        return records

    def _write(
        self,
        records: dict[str, TrustRecord],
        workspaces: dict[str, WorkspaceDecision] | None = None,
    ) -> None:
        kept = self._workspaces() if workspaces is None else workspaces
        payload: dict[str, object] = {
            "version": _STORE_VERSION,
            "records": [record.to_dict() for _path, record in sorted(records.items())],
            "workspaces": [
                {
                    "path": decision.path,
                    "decision": decision.decision,
                    "decided_at": decision.decided_at,
                }
                for _path, decision in sorted(kept.items())
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
_PUBLISHING: contextvars.ContextVar[frozenset[Path]] = contextvars.ContextVar(
    "agenthicc_publishing_projects", default=frozenset()
)


@contextmanager
def inspect_pending_publication(workspace: Path) -> Iterator[None]:
    """Let the authorized publish step discover its provisional installation."""
    token = _PUBLISHING.set(_PUBLISHING.get() | {workspace.resolve()})
    try:
        yield
    finally:
        _PUBLISHING.reset(token)


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
