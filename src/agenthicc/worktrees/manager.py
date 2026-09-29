"""Safe Git worktree lifecycle primitives.

This module is intentionally synchronous and side-effect explicit.  The
orchestrator may call it from an async session through ``asyncio.to_thread``;
keeping Git operations here makes the invariants easy to test with a real
temporary repository and prevents agent tools from receiving arbitrary Git
filesystem powers.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path

from .model import WorktreeRecord, WorktreeStatus, WorkerResult


class WorktreeError(RuntimeError):
    """Base class for safe worktree operation failures."""


class RepositoryError(WorktreeError):
    """The target is not a usable Git repository."""


class WorktreeDirtyError(WorktreeError):
    """An operation would risk losing uncommitted changes."""


class WorktreeConflictError(WorktreeError):
    """A merge or rebase produced conflicts; the worker is preserved."""


class WorktreeNotFound(WorktreeError):
    """A requested worktree record or path does not exist."""


@dataclass(frozen=True)
class IntegrationResult:
    """Outcome of integrating a worker branch into the coordinator branch."""

    ok: bool
    repository: str
    branch: str
    head_commit: str = ""
    conflict_paths: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class WorktreeInspection:
    """Machine-derived state used for review and completion evidence."""

    record: WorktreeRecord
    result: WorkerResult
    diff: str = ""


_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str, fallback: str) -> str:
    cleaned = _SAFE_PART.sub("-", value.strip()).strip("-.")
    return (cleaned or fallback)[:48]


class WorktreeManager:
    """Create, inspect, integrate, recover, and safely remove worktrees."""

    def __init__(self, repository: Path | str, *, root: Path | str | None = None) -> None:
        self.repository = self._resolve_repository(Path(repository).expanduser())
        if root is None:
            root = Path.home() / ".agenthicc" / "worktrees" / self.repository.name
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _resolve_repository(path: Path) -> Path:
        try:
            raw = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RepositoryError(f"not a Git repository: {path}") from exc
        if not raw:
            raise RepositoryError(f"Git returned no repository root for {path}")
        return Path(raw).resolve()

    def _run(self, args: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd or self.repository),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorktreeError(f"Git command failed to start: {exc}") from exc
        output = (result.stdout + result.stderr).strip()
        if check and result.returncode != 0:
            raise WorktreeError(f"git {' '.join(args)} failed: {output[:1000]}")
        return output

    def head(self, *, cwd: Path | None = None) -> str:
        return self._run(["rev-parse", "HEAD"], cwd=cwd).splitlines()[0].strip()

    def branch(self, *, cwd: Path | None = None) -> str:
        return self._run(["symbolic-ref", "--short", "HEAD"], cwd=cwd).splitlines()[0].strip()

    def is_clean(self, *, cwd: Path | None = None) -> bool:
        output = self._run(["status", "--porcelain"], cwd=cwd, check=True)
        if not output:
            return True
        if (cwd or self.repository).resolve() != self.repository:
            return False
        # A project-local configured worktree root is infrastructure owned by
        # this manager. Ignore only that exact subtree; unrelated untracked
        # files still make the coordinator refuse to start.
        remaining: list[str] = []
        try:
            ignored_root = self.root.relative_to(self.repository).as_posix().rstrip("/") + "/"
        except ValueError:
            ignored_root = ""
        for line in output.splitlines():
            changed_path = line[3:].strip() if len(line) >= 4 else ""
            if ignored_root and (
                changed_path == ignored_root[:-1] or changed_path.startswith(ignored_root)
            ):
                continue
            remaining.append(line)
        return not remaining

    def ensure_clean(self, *, cwd: Path | None = None) -> None:
        if not self.is_clean(cwd=cwd):
            raise WorktreeDirtyError(f"Git worktree is dirty: {cwd or self.repository}")

    def validate_base(self, base_commit: str) -> str:
        if not base_commit or not re.fullmatch(r"[0-9a-fA-F]{7,64}", base_commit):
            raise WorktreeError("base_commit must be a Git object id")
        resolved = self._run(["rev-parse", f"{base_commit}^{{commit}}"]).splitlines()[0]
        return resolved

    def create(
        self,
        *,
        parent_session_id: str,
        task_id: str,
        base_commit: str | None = None,
    ) -> WorktreeRecord:
        """Create one branch/worktree from an immutable clean repository base."""

        self.ensure_clean()
        base = self.validate_base(base_commit or self.head())
        worktree_id = uuid.uuid4().hex[:16]
        branch = (
            f"agenthicc/{_slug(parent_session_id, 'session')}/"
            f"{_slug(task_id, 'task')}-{worktree_id[:8]}"
        )
        path = (self.root / worktree_id).resolve()
        if path.exists():
            raise WorktreeError(f"worktree path already exists: {path}")
        record = WorktreeRecord(
            worktree_id=worktree_id,
            repository=str(self.repository),
            path=str(path),
            branch=branch,
            base_commit=base,
            owner_session_id=parent_session_id,
            task_id=task_id,
        )
        try:
            self._run(["worktree", "add", "-b", branch, str(path), base])
            actual_head = self.head(cwd=path)
            return record.evolve(status=WorktreeStatus.ACTIVE, head_commit=actual_head, dirty=False)
        except Exception:
            # Only remove the exact path and branch allocated by this call.
            if path.exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=str(self.repository),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=str(self.repository),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            raise

    def inspect(self, record: WorktreeRecord) -> WorktreeInspection:
        path = Path(record.path).resolve()
        if not path.is_dir():
            raise WorktreeNotFound(record.worktree_id)
        head = self.head(cwd=path)
        clean = self.is_clean(cwd=path)
        commits = self._run(
            ["log", "--format=%H", f"{record.base_commit}..{head}"], cwd=path
        ).splitlines()
        files = self._run(
            ["diff", "--name-only", f"{record.base_commit}..{head}"], cwd=path
        ).splitlines()
        diff = self._run(["diff", "--no-ext-diff", f"{record.base_commit}..{head}"], cwd=path)
        result = WorkerResult(
            task_id=record.task_id,
            worker_session_id="",
            worktree_id=record.worktree_id,
            base_commit=record.base_commit,
            head_commit=head,
            commits=tuple(line for line in commits if line),
            changed_files=tuple(line for line in files if line),
            clean=clean,
            status="complete" if clean and head != record.base_commit else "incomplete",
        )
        return WorktreeInspection(record.evolve(head_commit=head, dirty=not clean), result, diff)

    def status(self, record: WorktreeRecord) -> WorktreeInspection:
        """Return the same machine-derived inspection under the public status API."""

        return self.inspect(record)

    def validate(self, record: WorktreeRecord) -> WorkerResult:
        """Validate a worker's immutable base and current Git evidence."""

        if Path(record.repository).resolve() != self.repository:
            raise WorktreeError("worktree belongs to a different repository")
        self.validate_base(record.base_commit)
        inspection = self.inspect(record)
        return inspection.result

    def complete(self, record: WorktreeRecord, *, worker_session_id: str = "") -> WorkerResult:
        inspection = self.inspect(record)
        result = inspection.result
        if not result.clean:
            return replace(result, worker_session_id=worker_session_id, status="dirty")
        return replace(result, worker_session_id=worker_session_id)

    def integrate(
        self,
        record: WorktreeRecord,
        *,
        expected_branch: str | None = None,
    ) -> IntegrationResult:
        """Merge a completed worker branch; preserve it on conflict."""

        self.ensure_clean()
        if not Path(record.path).is_dir():
            raise WorktreeNotFound(record.worktree_id)
        current_branch = self.branch()
        if expected_branch is not None and current_branch != expected_branch:
            raise WorktreeError(
                f"coordinator branch changed: expected {expected_branch}, found {current_branch}"
            )
        result = subprocess.run(
            ["git", "merge", "--no-edit", record.branch],
            cwd=str(self.repository),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0:
            return IntegrationResult(
                ok=True,
                repository=str(self.repository),
                branch=record.branch,
                head_commit=self.head(),
                message=(result.stdout or result.stderr).strip(),
            )
        conflicts = self._run(["diff", "--name-only", "--diff-filter=U"], check=False).splitlines()
        # Abort only the coordinator merge.  The worker worktree is untouched.
        self._run(["merge", "--abort"], check=False)
        return IntegrationResult(
            ok=False,
            repository=str(self.repository),
            branch=record.branch,
            conflict_paths=tuple(line for line in conflicts if line),
            message=(result.stdout + result.stderr).strip()[:2000],
        )

    def rebase(self, record: WorktreeRecord, *, onto: str | None = None) -> IntegrationResult:
        """Rebase a worker branch while preserving the worker on conflict."""

        self.ensure_clean(cwd=Path(record.path))
        target = onto or self.branch()
        result = subprocess.run(
            ["git", "rebase", target],
            cwd=record.path,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0:
            return IntegrationResult(
                True,
                str(self.repository),
                record.branch,
                self.head(cwd=Path(record.path)),
                message=result.stdout.strip(),
            )
        conflicts = self._run(
            ["diff", "--name-only", "--diff-filter=U"], cwd=Path(record.path), check=False
        ).splitlines()
        self._run(["rebase", "--abort"], cwd=Path(record.path), check=False)
        return IntegrationResult(
            False,
            str(self.repository),
            record.branch,
            conflict_paths=tuple(conflicts),
            message=(result.stdout + result.stderr).strip()[:2000],
        )

    def remove(self, record: WorktreeRecord, *, force: bool = False) -> WorktreeRecord:
        """Remove only an integrated, clean worktree unless force is explicit."""

        path = Path(record.path).resolve()
        if not force:
            inspection = self.inspect(record)
            if not inspection.result.clean:
                raise WorktreeDirtyError(f"refusing to remove dirty worker {record.worktree_id}")
            if record.status not in {WorktreeStatus.INTEGRATED, WorktreeStatus.REMOVED}:
                raise WorktreeError("worker may be removed only after successful integration")
        if path.exists():
            arguments = ["worktree", "remove"]
            if force:
                arguments.append("--force")
            arguments.append(str(path))
            self._run(arguments)
        self._run(["branch", "-D", record.branch], check=False)
        return record.evolve(status=WorktreeStatus.REMOVED, dirty=False)

    def recover(self) -> list[WorktreeRecord]:
        """Return Git's exact worktree view for reconciliation after a crash."""

        raw = self._run(["worktree", "list", "--porcelain"])
        records: list[WorktreeRecord] = []
        current: dict[str, str] = {}
        for line in [*raw.splitlines(), ""]:
            if line:
                key, _, value = line.partition(" ")
                current[key] = value
                continue
            path = current.get("worktree")
            head = current.get("HEAD", "")
            branch = current.get("branch", "").removeprefix("refs/heads/")
            if path and Path(path).resolve() != self.repository:
                records.append(
                    WorktreeRecord(
                        worktree_id=Path(path).name,
                        repository=str(self.repository),
                        path=str(Path(path).resolve()),
                        branch=branch,
                        base_commit="",
                        owner_session_id="",
                        task_id="",
                        status=WorktreeStatus.ORPHANED,
                        head_commit=head,
                        dirty=not self.is_clean(cwd=Path(path)),
                    )
                )
            current = {}
        return records
