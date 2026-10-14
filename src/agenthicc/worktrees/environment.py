"""Trusted, reproducible setup commands and named worktree actions (PRD-229).

The manifest is project data, not an implicit permission grant.  Commands run
only after a matching manifest digest was trusted or the current invocation
explicitly approves them.  Worktree-relative paths are resolved after
symlinks and rejected if they escape the assigned worktree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Protocol, cast

import tomllib

MANIFEST_RELATIVE_PATH = Path(".agenthicc") / "environment.toml"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_COMMAND_TIMEOUT_S = 3_600.0
MAX_OUTPUT_BYTES = 128 * 1024
MAX_RECEIPTS_PER_SESSION = 64
MAX_MANIFEST_COMMANDS = 64
MAX_MANIFEST_CACHES = 32
MAX_IDENTIFIER_LENGTH = 64
_SECRET_NAME = re.compile(
    r"(?:secret|token|password|passwd|credential|private.?key|api.?key|auth)", re.I
)
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TERMINAL_CONTROL = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


class EnvironmentError(RuntimeError):
    """Base class for manifest and action errors."""


class EnvironmentManifestError(EnvironmentError):
    """The project environment manifest is invalid or unsafe."""


class EnvironmentTrustRequired(EnvironmentError):
    """A manifest needs explicit trust before setup is allowed to run."""


class _MsvcrtLocking(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


@dataclass(frozen=True, slots=True)
class EnvironmentCommand:
    command_id: str
    command: str
    cwd: str = "."
    timeout_s: float = 600.0
    env: tuple[str, ...] = ()
    secret_env: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    network: str = "may_access_network"


@dataclass(frozen=True, slots=True)
class EnvironmentCache:
    name: str
    env_var: str
    path: str
    sharing: str = "isolated"
    isolation_strategy: str = ""


@dataclass(frozen=True, slots=True)
class EnvironmentManifest:
    repository: Path
    path: Path
    digest: str
    setup: tuple[EnvironmentCommand, ...]
    actions: tuple[EnvironmentCommand, ...]
    caches: tuple[EnvironmentCache, ...]

    def preview(self, *, workspace_root: Path | None = None) -> tuple[str, ...]:
        command_root = (workspace_root or self.repository).resolve()
        lines: list[str] = [
            f"Manifest: {self.path}",
            f"SHA-256: {self.digest}",
        ]
        for title, entries in (("Setup", self.setup), ("Actions", self.actions)):
            for entry in entries:
                lines.extend(
                    (
                        f"{title} {entry.command_id}: {entry.command}",
                        f"  workspace: {resolve_worktree_path(command_root, entry.cwd, label=f'{entry.command_id}.cwd')}",
                        f"  network: {entry.network}; timeout: {entry.timeout_s:g}s",
                    )
                )
        return tuple(lines)

    def action(self, action_id: str) -> EnvironmentCommand:
        for action in self.actions:
            if action.command_id == action_id:
                return action
        raise KeyError(action_id)


@dataclass(frozen=True, slots=True)
class EnvironmentCommandResult:
    command_id: str
    started_at: str
    ended_at: str
    exit_code: int | None
    result: str
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "result": self.result,
            "truncated": self.truncated,
            "error": self.error[:1_000],
        }


@dataclass(frozen=True, slots=True)
class EnvironmentRunReceipt:
    manifest_digest: str
    kind: str
    started_at: str
    ended_at: str
    result: str
    commands: tuple[EnvironmentCommandResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_digest": self.manifest_digest,
            "kind": self.kind,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "result": self.result,
            "commands": [item.to_dict() for item in self.commands],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _inside(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def resolve_worktree_path(root: str | Path, raw: str, *, label: str = "path") -> Path:
    """Resolve a manifest-relative path and reject symlink/traversal escapes."""

    root = Path(root).expanduser().resolve()
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise EnvironmentManifestError(f"{label} must be a non-empty relative path")
    requested = Path(raw)
    if requested.is_absolute():
        raise EnvironmentManifestError(f"{label} must be relative to the worktree")
    resolved = (root / requested).resolve()
    if not _inside(root, resolved):
        raise EnvironmentManifestError(f"{label} escapes the worktree: {raw}")
    return resolved


def _strings(value: object, *, field: str, allow_empty: bool = False) -> tuple[str, ...]:
    if value is None and allow_empty:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise EnvironmentManifestError(f"{field} must be an array of non-empty strings")
    return tuple(value)


def _command(raw: object, *, command_id: str) -> EnvironmentCommand:
    if not isinstance(raw, dict):
        raise EnvironmentManifestError(f"command {command_id!r} must be a table")
    allowed = {
        "id",
        "command",
        "cwd",
        "timeout_s",
        "env",
        "secret_env",
        "expected_outputs",
        "platforms",
        "network",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise EnvironmentManifestError(
            f"command {command_id!r} has unsupported field(s): {', '.join(unknown)}"
        )
    command = raw.get("command")
    cwd = raw.get("cwd", ".")
    timeout = raw.get("timeout_s", 600)
    if not isinstance(command, str) or not command.strip() or len(command) > 16_384:
        raise EnvironmentManifestError(f"command {command_id!r} requires a command string")
    if not isinstance(cwd, str):
        raise EnvironmentManifestError(f"command {command_id!r} cwd must be a string")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
        or timeout > MAX_COMMAND_TIMEOUT_S
    ):
        raise EnvironmentManifestError(
            f"command {command_id!r} timeout_s must be between 0 and {MAX_COMMAND_TIMEOUT_S:g}"
        )
    env = _strings(raw.get("env", []), field=f"{command_id}.env", allow_empty=True)
    secret_env = _strings(
        raw.get("secret_env", []), field=f"{command_id}.secret_env", allow_empty=True
    )
    expected = _strings(
        raw.get("expected_outputs", []),
        field=f"{command_id}.expected_outputs",
        allow_empty=True,
    )
    platforms = _strings(
        raw.get("platforms", []), field=f"{command_id}.platforms", allow_empty=True
    )
    if any(not _SAFE_NAME.fullmatch(name) for name in (*env, *secret_env)):
        raise EnvironmentManifestError(f"command {command_id!r} has an invalid environment name")
    if not set(secret_env).issubset(env):
        raise EnvironmentManifestError(
            f"command {command_id!r} secret_env names must also appear in env"
        )
    network = raw.get("network", "may_access_network")
    if not isinstance(network, str) or network not in {
        "none_declared",
        "may_access_network",
        "requires_network",
    }:
        raise EnvironmentManifestError(
            f"command {command_id!r} network must be none_declared, may_access_network, "
            "or requires_network"
        )
    if Path(cwd).is_absolute():
        raise EnvironmentManifestError(f"{command_id}.cwd must be relative to the worktree")
    return EnvironmentCommand(
        command_id=command_id,
        command=command,
        cwd=cwd,
        timeout_s=float(timeout),
        env=env,
        secret_env=secret_env,
        expected_outputs=expected,
        platforms=platforms,
        network=network,
    )


def load_environment_manifest(repository: str | Path) -> EnvironmentManifest | None:
    """Load and validate the repository's optional environment manifest."""

    root = Path(repository).expanduser().resolve()
    candidate = root / MANIFEST_RELATIVE_PATH
    if not candidate.exists():
        return None
    path = candidate.resolve()
    if not _inside(root, path) or not path.is_file():
        raise EnvironmentManifestError(
            "environment manifest must be a regular file inside repository"
        )
    try:
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > MAX_MANIFEST_BYTES:
            raise EnvironmentManifestError("environment manifest exceeds 256 KiB")
        raw_value = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, EnvironmentManifestError):
            raise
        raise EnvironmentManifestError(f"unable to parse {path}: {exc}") from exc
    if (
        not isinstance(raw_value, dict)
        or type(raw_value.get("version")) is not int
        or raw_value.get("version") != 1
    ):
        raise EnvironmentManifestError("environment manifest requires version = 1")
    unknown_root = sorted(set(raw_value) - {"version", "setup", "actions", "caches"})
    if unknown_root:
        raise EnvironmentManifestError(
            f"unsupported environment manifest field(s): {', '.join(unknown_root)}"
        )
    raw_setup = raw_value.get("setup", [])
    raw_actions = raw_value.get("actions", {})
    raw_caches = raw_value.get("caches", [])
    if not isinstance(raw_setup, list) or not isinstance(raw_actions, dict):
        raise EnvironmentManifestError("setup must be an array of tables and actions a table")
    if not isinstance(raw_caches, list):
        raise EnvironmentManifestError("caches must be an array of tables")
    setup = tuple(
        _command(item, command_id=_command_id(item, f"setup_{index + 1}"))
        for index, item in enumerate(raw_setup)
    )
    actions: list[EnvironmentCommand] = []
    for action_id, value in raw_actions.items():
        if (
            not isinstance(action_id, str)
            or len(action_id) > MAX_IDENTIFIER_LENGTH
            or not _SAFE_NAME.fullmatch(action_id)
        ):
            raise EnvironmentManifestError("action names must be simple identifiers")
        actions.append(_command(value, command_id=action_id))
    if len(setup) + len(actions) > MAX_MANIFEST_COMMANDS:
        raise EnvironmentManifestError(
            f"manifest may contain at most {MAX_MANIFEST_COMMANDS} setup/action commands"
        )
    for spec in (*setup, *actions):
        resolve_worktree_path(root, spec.cwd, label=f"{spec.command_id}.cwd")
        for output in spec.expected_outputs:
            resolve_worktree_path(
                root,
                output,
                label=f"{spec.command_id}.expected_outputs",
            )
    if len({item.command_id for item in setup}) != len(setup):
        raise EnvironmentManifestError("setup command IDs must be unique")
    caches = tuple(_cache(item) for item in raw_caches)
    if len(caches) > MAX_MANIFEST_CACHES:
        raise EnvironmentManifestError(
            f"manifest may contain at most {MAX_MANIFEST_CACHES} cache entries"
        )
    if len({item.name for item in caches}) != len(caches):
        raise EnvironmentManifestError("cache names must be unique")
    if len({item.env_var for item in caches}) != len(caches):
        raise EnvironmentManifestError("cache environment variable names must be unique")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return EnvironmentManifest(root, path, digest, setup, tuple(actions), caches)


def _command_id(raw: object, default: str) -> str:
    if not isinstance(raw, dict):
        raise EnvironmentManifestError(f"{default} must be a table")
    command_id = raw.get("id", default)
    if (
        not isinstance(command_id, str)
        or len(command_id) > MAX_IDENTIFIER_LENGTH
        or not _SAFE_NAME.fullmatch(command_id)
    ):
        raise EnvironmentManifestError(f"invalid setup command id: {command_id!r}")
    return command_id


def _cache(raw: object) -> EnvironmentCache:
    if not isinstance(raw, dict):
        raise EnvironmentManifestError("each cache entry must be a table")
    unknown = sorted(set(raw) - {"name", "env_var", "path", "sharing", "isolation_strategy"})
    if unknown:
        raise EnvironmentManifestError(
            f"cache entry has unsupported field(s): {', '.join(unknown)}"
        )
    name, env_var, path = raw.get("name"), raw.get("env_var"), raw.get("path")
    sharing = raw.get("sharing", "isolated")
    strategy = raw.get("isolation_strategy", "")
    if not all(isinstance(value, str) and value for value in (name, env_var, path)):
        raise EnvironmentManifestError("cache entries require name, env_var, and path strings")
    name, env_var, path = cast(str, name), cast(str, env_var), cast(str, path)
    if (
        len(name) > MAX_IDENTIFIER_LENGTH
        or not _SAFE_NAME.fullmatch(name)
        or not _SAFE_NAME.fullmatch(env_var)
    ):
        raise EnvironmentManifestError(f"cache {name!r} has an invalid name or env_var")
    if Path(path).is_absolute() or "\x00" in path:
        raise EnvironmentManifestError(f"cache {name!r} path must be relative")
    if not isinstance(sharing, str) or not isinstance(strategy, str):
        raise EnvironmentManifestError(f"cache {name!r} sharing fields must be strings")
    if sharing not in {"isolated", "immutable_shared", "mutable_shared"}:
        raise EnvironmentManifestError(f"cache {name!r} has an unsupported sharing policy")
    if sharing == "mutable_shared" and strategy != "exclusive_lock":
        raise EnvironmentManifestError(
            f"mutable shared cache {name!r} requires isolation_strategy = 'exclusive_lock'"
        )
    if sharing != "mutable_shared" and strategy:
        raise EnvironmentManifestError(
            f"cache {name!r} isolation_strategy is only valid for mutable_shared caches"
        )
    return EnvironmentCache(name, env_var, path, sharing, strategy)


def _iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(timestamp or time.time(), timezone.utc).isoformat()


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    result = _TERMINAL_CONTROL.sub("", value)
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        result = result.replace(secret, "<redacted>")
    return result


def _safe_environment(
    spec: EnvironmentCommand, cache_vars: Mapping[str, str]
) -> tuple[dict[str, str], tuple[str, ...]]:
    # Inherit ordinary process/tooling variables, but do not expose ambient
    # credentials unless the manifest explicitly names them under secret_env.
    env = {
        key: value
        for key, value in os.environ.items()
        if not _SECRET_NAME.search(key) and isinstance(value, str)
    }
    secrets: list[str] = []
    for name in spec.env:
        value = os.environ.get(name)
        if value is None:
            raise EnvironmentError(f"required environment variable is not set: {name}")
        env[name] = value
        if name in spec.secret_env or _SECRET_NAME.search(name):
            secrets.append(value)
    env.update(cache_vars)
    return env, tuple(secrets)


class EnvironmentTrustStore:
    """User-local trust decisions bound to repository path and exact digest."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or Path.home() / ".agenthicc" / "environment-trust.json")

    def is_trusted(self, manifest: EnvironmentManifest) -> bool:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(value, dict):
            return False
        trusted = value.get("trusted", {})
        if not isinstance(trusted, dict):
            return False
        return trusted.get(_repository_identity(manifest.repository)) == manifest.digest

    def trust(self, manifest: EnvironmentManifest) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with _cache_locks([lock_path]):
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = {}
            trusted = value.get("trusted") if isinstance(value, dict) else None
            entries = dict(trusted) if isinstance(trusted, dict) else {}
            entries[_repository_identity(manifest.repository)] = manifest.digest
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            payload = json.dumps({"version": 1, "trusted": entries}, indent=2, sort_keys=True)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass


class WorktreeEnvironment:
    """Validate and execute setup/actions for one exact worktree."""

    def __init__(
        self,
        repository: str | Path,
        worktree_id: str,
        *,
        trust_store: EnvironmentTrustStore | None = None,
        cache_root: str | Path | None = None,
        output_limit: int = MAX_OUTPUT_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.repository = Path(repository).expanduser().resolve()
        self.worktree_id = worktree_id
        self.trust_store = trust_store or EnvironmentTrustStore()
        self.cache_root = (
            Path(cache_root or Path.home() / ".agenthicc" / "worktree-caches")
            .expanduser()
            .resolve()
        )
        self.output_limit = max(1, min(int(output_limit), MAX_OUTPUT_BYTES))
        self.clock = clock or time.time

    def load(self) -> EnvironmentManifest | None:
        return load_environment_manifest(self.repository)

    def require_trust_or_approval(
        self,
        manifest: EnvironmentManifest,
        *,
        approved: bool,
        workspace_root: Path | None = None,
    ) -> None:
        if approved or self.trust_store.is_trusted(manifest):
            return
        preview = "\n".join(manifest.preview(workspace_root=workspace_root))
        raise EnvironmentTrustRequired(
            "Environment manifest is not trusted at its current digest. Review the commands, "
            "then pass --approve for this run or trust this exact digest.\n" + preview
        )

    def _cache_values(self, manifest: EnvironmentManifest) -> tuple[dict[str, str], list[Path]]:
        values: dict[str, str] = {}
        locks: list[Path] = []
        repo_key = hashlib.sha256(str(self.repository).encode()).hexdigest()[:20]
        for cache in manifest.caches:
            if cache.sharing == "isolated":
                isolated_base = (
                    self.cache_root / "isolated" / repo_key / self.worktree_id
                ).resolve()
                if not _inside(self.cache_root, isolated_base):
                    raise EnvironmentManifestError(
                        f"isolated cache {cache.name!r} escapes the configured cache root"
                    )
                isolated_root = (isolated_base / cache.name).resolve()
                if not _inside(isolated_base, isolated_root):
                    raise EnvironmentManifestError(
                        f"isolated cache {cache.name!r} escapes its worktree cache directory"
                    )
                if Path(cache.path).is_absolute():
                    raise EnvironmentManifestError(
                        f"isolated cache {cache.name!r} path must be relative"
                    )
                path = (isolated_root / cache.path).resolve()
                if not _inside(isolated_root, path):
                    raise EnvironmentManifestError(
                        f"isolated cache {cache.name!r} path escapes its private directory"
                    )
            else:
                base = (self.cache_root / "shared" / repo_key).resolve()
                if not _inside(self.cache_root, base):
                    raise EnvironmentManifestError(
                        f"shared cache {cache.name!r} escapes the configured cache root"
                    )
                path = (base / cache.path).resolve()
                if not _inside(base, path):
                    raise EnvironmentManifestError(
                        f"cache path escapes shared cache root: {cache.path}"
                    )
                if cache.sharing == "mutable_shared":
                    locks.append(path.with_name(path.name + ".agenthicc.lock"))
                if cache.sharing == "immutable_shared" and not path.exists():
                    raise EnvironmentError(
                        f"declared immutable shared cache does not exist: {path}"
                    )
            if _inside(self.repository, path) or _inside(path, self.repository):
                raise EnvironmentManifestError(
                    f"cache {cache.name!r} must not overlap the worker worktree"
                )
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            values[cache.env_var] = str(path)
        return values, locks

    def run_setup(
        self,
        *,
        approved: bool = False,
        cancel_event: threading.Event | None = None,
        expected_manifest_digest: str | None = None,
    ) -> EnvironmentRunReceipt | None:
        manifest = self.load()
        actual_digest = manifest.digest if manifest is not None else ""
        if expected_manifest_digest is not None and actual_digest != expected_manifest_digest:
            raise EnvironmentManifestError(
                "environment manifest changed after it was reviewed; review the current "
                "manifest and retry"
            )
        if manifest is None or not manifest.setup:
            return None
        self.require_trust_or_approval(manifest, approved=approved)
        return self._run(manifest, manifest.setup, kind="setup", cancel_event=cancel_event)

    def run_action(
        self,
        action_id: str,
        *,
        approved: bool = False,
        cancel_event: threading.Event | None = None,
        expected_manifest_digest: str | None = None,
    ) -> EnvironmentRunReceipt:
        manifest = self.load()
        if manifest is None:
            raise EnvironmentError(
                f"No environment manifest found at {self.repository / MANIFEST_RELATIVE_PATH}"
            )
        if expected_manifest_digest is not None and manifest.digest != expected_manifest_digest:
            raise EnvironmentManifestError(
                "environment manifest changed after the action was reviewed; review the "
                "current manifest and retry"
            )
        action = manifest.action(action_id)
        self.require_trust_or_approval(manifest, approved=approved)
        return self._run(
            manifest,
            (action,),
            kind=f"action:{action_id}",
            cancel_event=cancel_event,
        )

    def _run(
        self,
        manifest: EnvironmentManifest,
        commands: tuple[EnvironmentCommand, ...],
        *,
        kind: str,
        cancel_event: threading.Event | None = None,
    ) -> EnvironmentRunReceipt:
        start_wall = self.clock()
        results: list[EnvironmentCommandResult] = []
        overall = "succeeded"
        for spec in commands:
            started = self.clock()
            started_at = _iso(started)
            try:
                if cancel_event is not None and cancel_event.is_set():
                    results.append(
                        EnvironmentCommandResult(
                            spec.command_id,
                            started_at,
                            _iso(self.clock()),
                            None,
                            "cancelled",
                        )
                    )
                    overall = "cancelled"
                    break
                cwd = resolve_worktree_path(
                    self.repository,
                    spec.cwd,
                    label=f"{spec.command_id}.cwd",
                )
                if not cwd.is_dir():
                    raise EnvironmentError(f"command working directory does not exist: {spec.cwd}")
                for output in spec.expected_outputs:
                    resolve_worktree_path(
                        self.repository,
                        output,
                        label=f"{spec.command_id}.expected_outputs",
                    )
                if (
                    spec.platforms
                    and os.name not in spec.platforms
                    and sys_platform() not in spec.platforms
                ):
                    results.append(
                        EnvironmentCommandResult(
                            spec.command_id,
                            started_at,
                            _iso(self.clock()),
                            None,
                            "skipped_platform",
                        )
                    )
                    continue
                cache_vars, locks = self._cache_values(manifest)
                env, secrets = _safe_environment(spec, cache_vars)
                lock_context = _cache_locks(locks, cancel_event)
                with lock_context:
                    result = _run_bounded(
                        spec.command,
                        cwd=cwd,
                        workspace_root=self.repository,
                        env=env,
                        cache_mounts=tuple(
                            (
                                cache.env_var,
                                Path(cache_vars[cache.env_var]),
                                cache.sharing == "immutable_shared",
                            )
                            for cache in manifest.caches
                        ),
                        network=spec.network,
                        timeout_s=spec.timeout_s,
                        output_limit=self.output_limit,
                        cancel_event=cancel_event,
                    )
                stdout_raw = result.get("stdout", "")
                stderr_raw = result.get("stderr", "")
                stdout = _redact(stdout_raw if isinstance(stdout_raw, str) else "", secrets)
                stderr = _redact(stderr_raw if isinstance(stderr_raw, str) else "", secrets)
                code = result["exit_code"]
                error = str(result["error"] or "")
                status = str(result["result"])
                if code == 0 and status == "succeeded":
                    missing = [
                        output
                        for output in spec.expected_outputs
                        if not resolve_worktree_path(
                            self.repository,
                            output,
                            label="expected output",
                        ).exists()
                    ]
                    if missing:
                        status = "missing_expected_output"
                        error = "Expected output(s) not produced: " + ", ".join(missing)
                results.append(
                    EnvironmentCommandResult(
                        spec.command_id,
                        started_at,
                        _iso(self.clock()),
                        code if isinstance(code, int) else None,
                        status,
                        stdout,
                        stderr,
                        bool(result["truncated"]),
                        _redact(error, secrets),
                    )
                )
                if status != "succeeded":
                    overall = status
                    break
            except (EnvironmentError, EnvironmentManifestError, OSError, ValueError) as exc:
                results.append(
                    EnvironmentCommandResult(
                        spec.command_id,
                        started_at,
                        _iso(self.clock()),
                        None,
                        "failed",
                        error=f"{type(exc).__name__}: {exc}"[:1_000],
                    )
                )
                overall = (
                    "cancelled" if cancel_event is not None and cancel_event.is_set() else "failed"
                )
                break
        return EnvironmentRunReceipt(
            manifest.digest, kind, _iso(start_wall), _iso(self.clock()), overall, tuple(results)
        )


def sys_platform() -> str:
    return sys.platform


def _repository_identity(repository: Path) -> str:
    """Use Git's shared common directory so one trust covers its worktrees."""

    try:
        value = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return str(repository.resolve())
    common = Path(value)
    return str((repository / common).resolve() if not common.is_absolute() else common.resolve())


class _CacheLocks:
    def __init__(
        self,
        paths: list[Path],
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.paths = sorted(paths, key=str)
        self.cancel_event = cancel_event
        self.handles: list[BinaryIO] = []

    def __enter__(self) -> "_CacheLocks":
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            try:
                while True:
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        raise EnvironmentError("cancelled while waiting for a shared cache lock")
                    try:
                        if os.name == "nt":
                            import importlib

                            msvcrt = cast(_MsvcrtLocking, importlib.import_module("msvcrt"))
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
            except BaseException:
                handle.close()
                self.__exit__(None, None, None)
                raise
            self.handles.append(handle)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        for handle in reversed(self.handles):
            try:
                if os.name == "nt":
                    import importlib

                    msvcrt = cast(_MsvcrtLocking, importlib.import_module("msvcrt"))
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self.handles.clear()


def _cache_locks(
    paths: list[Path],
    cancel_event: threading.Event | None = None,
) -> _CacheLocks:
    return _CacheLocks(paths, cancel_event)


def _sandbox_workspace_mountpoint(
    workspace_root: Path,
    cache_mounts: tuple[tuple[str, Path, bool], ...],
) -> Path:
    candidates = (Path("/mnt"), Path("/opt"), Path("/srv"))
    cache_paths = tuple(path.expanduser().resolve() for _, path, _ in cache_mounts)
    for candidate in candidates:
        candidate = candidate.resolve()
        if not candidate.is_dir():
            continue
        if _inside(candidate, workspace_root) or _inside(workspace_root, candidate):
            continue
        if any(_inside(candidate, path) or _inside(path, candidate) for path in cache_paths):
            continue
        return candidate
    raise EnvironmentError("no safe existing mountpoint is available for the worker worktree")


def _run_bounded(
    command: str,
    *,
    cwd: Path,
    workspace_root: Path,
    env: Mapping[str, str],
    cache_mounts: tuple[tuple[str, Path, bool], ...],
    network: str,
    timeout_s: float,
    output_limit: int,
    cancel_event: threading.Event | None = None,
) -> dict[str, object]:
    """Run a command in its own process group while retaining bounded output."""

    if not sys.platform.startswith("linux"):
        return {
            "exit_code": None,
            "result": "sandbox_unavailable",
            "stdout": "",
            "stderr": "",
            "truncated": False,
            "error": "worktree environment commands require Linux bubblewrap isolation",
        }
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return {
            "exit_code": None,
            "result": "sandbox_unavailable",
            "stdout": "",
            "stderr": "",
            "truncated": False,
            "error": "bubblewrap (bwrap) is required to isolate worktree environment commands",
        }
    workspace_mount = _sandbox_workspace_mountpoint(workspace_root, cache_mounts)
    private_tmp = Path(
        tempfile.mkdtemp(
            prefix=".agenthicc-environment-",
            dir=str(workspace_root / ".agenthicc"),
        )
    )
    command_workspace = str(workspace_mount)
    command_tmp = f"{command_workspace}/.agenthicc/{private_tmp.name}"
    command_env = dict(env)
    command_env.update(
        {
            "HOME": command_tmp,
            "TMPDIR": command_tmp,
            "TMP": command_tmp,
            "TEMP": command_tmp,
            "XDG_CACHE_HOME": f"{command_tmp}/.cache",
        }
    )
    shell = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
    argv = [
        bwrap,
        "--die-with-parent",
        "--unshare-user",
        "--disable-userns",
        "--assert-userns-disabled",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        str(workspace_root),
        command_workspace,
    ]
    for index, (env_name, cache_path, readonly) in enumerate(cache_mounts):
        mountpoint = private_tmp / f"cache-{index}"
        mountpoint.mkdir(mode=0o700)
        mount_relative = mountpoint.relative_to(workspace_root)
        mount_in_sandbox = workspace_mount / mount_relative
        command_env[env_name] = str(mount_in_sandbox)
        argv.extend(
            (
                "--ro-bind" if readonly else "--bind",
                str(cache_path.expanduser().resolve()),
                str(mount_in_sandbox),
            )
        )
    argv.extend(("--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var/tmp"))
    sandbox_cwd = workspace_mount / cwd.relative_to(workspace_root)
    if network == "none_declared":
        argv.append("--unshare-net")
    argv.extend(("--chdir", str(sandbox_cwd), "--", shell, "-c", command))
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=command_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        shutil.rmtree(private_tmp, ignore_errors=True)
        return {
            "exit_code": None,
            "result": "sandbox_unavailable",
            "stdout": "",
            "stderr": "",
            "truncated": False,
            "error": f"unable to start isolated command: {type(exc).__name__}: {exc}",
        }
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = threading.Event()

    def drain(name: str, stream: BinaryIO | None) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            buffer = buffers[name]
            remaining = output_limit - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated.set()

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_s
    result = "succeeded"
    error = ""
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                result = "cancelled"
                _terminate_process_group(process)
                break
            if time.monotonic() >= deadline:
                result = "timed_out"
                error = f"command exceeded timeout of {timeout_s:g}s"
                _terminate_process_group(process)
                break
            time.sleep(0.025)
    except BaseException:
        _terminate_process_group(process)
        for reader in readers:
            reader.join(timeout=1.0)
        shutil.rmtree(private_tmp, ignore_errors=True)
        raise
    # A command may launch a background child and let its shell exit. Actions
    # are bounded one-shot operations, not service supervisors: terminate any
    # remaining members of the process group before releasing shared-cache
    # locks or returning control to the caller.
    _terminate_process_group(process)
    try:
        code = process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        try:
            code = process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
    for reader in readers:
        reader.join(timeout=1.0)
    shutil.rmtree(private_tmp, ignore_errors=True)
    if result == "succeeded" and code != 0:
        result = "failed"
        error = f"command exited with status {code}"
    return {
        "exit_code": code,
        "result": result,
        "stdout": bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
        "truncated": truncated.is_set(),
        "error": error,
    }


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=0.5)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    grace_deadline = time.monotonic() + 0.5
    while time.monotonic() < grace_deadline:
        try:
            os.killpg(process.pid, 0)
        except (OSError, ProcessLookupError):
            break
        if process.poll() is None:
            try:
                process.wait(timeout=0.025)
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.025)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
