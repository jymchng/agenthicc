"""Mode-aware workspace path resolution and access authorization.

The filesystem boundary is intentionally separate from tool capability policy.
``WorkspaceAccessPolicy`` answers one question: may this exact canonical path
be used for this operation in the current runtime mode?  Tool capability hooks
may then add their own approval requirement for writes, execution, and other
side effects.

The policy is session-scoped and is propagated through a ``ContextVar`` so the
existing ``@tool()`` wrappers can receive the same session context without
changing their public model-facing signatures.  The context is task-local;
no process-wide ``chdir`` or mutable global workspace is used.
"""

from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

if TYPE_CHECKING:
    from agenthicc.tools.approval import ApprovalResponse, ApprovalService

__all__ = [
    "WorkspaceAccessMode",
    "WorkspacePathStatus",
    "ResolvedWorkspacePath",
    "WorkspaceScope",
    "WorkspaceAccessRequest",
    "WorkspaceAccessResult",
    "WorkspaceAccessPolicy",
    "current_workspace_access",
    "set_current_workspace_access",
    "reset_current_workspace_access",
]


class WorkspaceAccessMode(str, Enum):
    """How the workspace boundary is enforced for the current session."""

    SCOPED = "scoped"
    UNRESTRICTED = "unrestricted"


class WorkspacePathStatus(str, Enum):
    """Stable path classification values used in tool results and events."""

    IN_SCOPE = "in_scope"
    OUTSIDE_SCOPE = "outside_workspace"
    NOT_FOUND = "not_found"
    INVALID = "invalid_path"
    TARGET_CHANGED = "target_changed"


@dataclass(frozen=True, slots=True)
class ResolvedWorkspacePath:
    """Canonical path identity returned before an operation is authorized."""

    requested: str
    absolute: Path
    root: Path | None
    root_id: str | None
    display: str
    operation: str
    exists: bool | None
    status: WorkspacePathStatus

    @property
    def in_scope(self) -> bool:
        return self.status is WorkspacePathStatus.IN_SCOPE


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """Immutable primary and explicitly configured filesystem roots."""

    primary_root: Path
    allowed_roots: tuple[Path, ...]
    scope_id: str

    @classmethod
    def create(
        cls,
        primary_root: str | Path,
        allowed_paths: Iterable[str | Path] = (),
    ) -> "WorkspaceScope":
        """Build a canonical scope, resolving relative paths from *primary_root*.

        The primary root is always included.  Explicit roots that do not exist
        yet are retained: a later write can still be authorized based on its
        canonical parent, while an operation will return the appropriate OS or
        not-found result.
        """

        primary = _realpath(Path(primary_root))
        candidates: list[Path] = [primary]
        for raw in allowed_paths:
            value = Path(raw)
            if not value.is_absolute():
                value = primary / value
            candidates.append(_realpath(value))

        unique = {str(path): path for path in candidates}
        roots = tuple(sorted(unique.values(), key=lambda path: (-len(path.parts), str(path))))
        digest = hashlib.sha256("\0".join(str(path) for path in roots).encode()).hexdigest()[:16]
        return cls(primary_root=primary, allowed_roots=roots, scope_id=digest)

    def resolve(
        self,
        requested: str | Path,
        *,
        base: Path | None = None,
        operation: str = "read",
        probe_exists: bool = True,
    ) -> ResolvedWorkspacePath:
        """Canonicalize *requested* and classify it against all allowed roots."""

        requested_text = os.fspath(requested)
        if "\x00" in requested_text:
            raise ValueError("path contains a NUL character")
        raw = Path(requested_text)
        anchor = base or self.primary_root
        candidate = raw if raw.is_absolute() else anchor / raw
        absolute = _realpath(candidate)
        containing = next((root for root in self.allowed_roots if _contains(root, absolute)), None)
        if containing is None:
            status = WorkspacePathStatus.OUTSIDE_SCOPE
            root_id = None
            display = str(absolute)
        else:
            status = WorkspacePathStatus.IN_SCOPE
            root_id = _root_id(containing)
            display = _display_path(absolute, containing)
        # Canonicalization is required to make the scope decision.  Existence
        # is separate metadata: Safe outside-workspace preflights pass
        # ``probe_exists=False`` so an approval request cannot stat the target
        # before the user grants access.  In-scope callers retain the useful
        # existence result even during a preflight.
        exists = absolute.exists() if containing is not None or probe_exists else None
        return ResolvedWorkspacePath(
            requested=requested_text,
            absolute=absolute,
            root=containing,
            root_id=root_id,
            display=display,
            operation=operation,
            exists=exists,
            status=status,
        )

    def resolve_pattern(
        self,
        requested: str | Path,
        *,
        base: Path | None = None,
        operation: str = "search",
        probe_exists: bool = True,
    ) -> ResolvedWorkspacePath:
        """Resolve a glob by classifying its non-wildcard parent anchor."""

        text = os.fspath(requested)
        wildcard_index = min((i for i, char in enumerate(text) if char in "*?["), default=-1)
        if wildcard_index < 0:
            return self.resolve(
                text,
                base=base,
                operation=operation,
                probe_exists=probe_exists,
            )
        prefix = text[:wildcard_index]
        # Keep a literal directory prefix as the anchor.  Taking
        # ``Path(prefix).parent`` unconditionally turns ``../outside/*.py``
        # into ``..`` and can incorrectly classify the pattern as in-scope.
        # For a wildcard in a filename (``src/*.py`` is handled by the
        # trailing separator branch; ``src/test*.py`` is not), the parent is
        # the correct literal anchor.
        has_separator = bool(
            prefix
            and (prefix.endswith(os.sep) or (os.altsep is not None and prefix.endswith(os.altsep)))
        )
        if has_separator:
            anchor_text = prefix.rstrip("/\\")
            parent = Path(anchor_text or Path(prefix).anchor or ".")
        else:
            parent = Path(prefix).parent if prefix else Path(".")
        return self.resolve(
            parent,
            base=base,
            operation=operation,
            probe_exists=probe_exists,
        )

    def revalidate(
        self,
        resolved: ResolvedWorkspacePath,
        *,
        pattern: bool = False,
        probe_exists: bool = True,
    ) -> ResolvedWorkspacePath:
        """Re-resolve an approved request and reject canonical-target changes."""

        current = (
            self.resolve_pattern(
                resolved.requested,
                operation=resolved.operation,
                probe_exists=probe_exists,
            )
            if pattern
            else self.resolve(
                resolved.requested,
                operation=resolved.operation,
                probe_exists=probe_exists,
            )
        )
        if current.absolute != resolved.absolute:
            return ResolvedWorkspacePath(
                requested=resolved.requested,
                absolute=current.absolute,
                root=current.root,
                root_id=current.root_id,
                display=current.display,
                operation=resolved.operation,
                exists=current.exists,
                status=WorkspacePathStatus.TARGET_CHANGED,
            )
        return current


@dataclass(frozen=True, slots=True)
class WorkspaceAccessRequest:
    """One exact outside-workspace access included in an approval request."""

    requested: str
    canonical: Path
    display: str
    operation: str
    tool_name: str
    status: str = WorkspacePathStatus.OUTSIDE_SCOPE.value
    exists: bool | None = None
    workspace_root: Path | None = None
    mode: str = "Safe"
    capabilities: frozenset[str] = frozenset()

    @property
    def grant_key(self) -> tuple[str, str]:
        return (str(self.canonical), self.operation)


@dataclass(frozen=True, slots=True)
class WorkspaceAccessResult:
    """Result returned by a policy preflight."""

    allowed: bool
    decisions: tuple[ResolvedWorkspacePath, ...] = ()
    approval_handled: bool = False
    code: str = "allowed"
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return the bounded diagnostic projection stored in tool events."""

        return {
            "allowed": self.allowed,
            "code": self.code,
            "error": self.error,
            "approval_handled": self.approval_handled,
            "decisions": [
                {
                    "requested": item.requested,
                    "canonical": str(item.absolute),
                    "display": item.display,
                    "operation": item.operation,
                    "root_id": item.root_id,
                    "status": item.status.value,
                    "exists": item.exists,
                }
                for item in self.decisions
            ],
        }


@dataclass(frozen=True, slots=True)
class _CurrentToolGrant:
    policy: object
    tool_name: str
    grants: frozenset[tuple[str, str]]
    requested: frozenset[tuple[str, str]]


_CURRENT_GRANT: ContextVar[_CurrentToolGrant | None] = ContextVar(
    "agenthicc_current_workspace_grant", default=None
)
_CURRENT_POLICY: ContextVar["WorkspaceAccessPolicy | None"] = ContextVar(
    "agenthicc_workspace_access_policy", default=None
)


def current_workspace_access() -> "WorkspaceAccessPolicy | None":
    """Return the session policy inherited by the current asyncio task."""

    return _CURRENT_POLICY.get()


def set_current_workspace_access(
    policy: "WorkspaceAccessPolicy",
) -> Token["WorkspaceAccessPolicy | None"]:
    """Bind *policy* to the current task and return its reset token."""

    return _CURRENT_POLICY.set(policy)


def reset_current_workspace_access(
    token: Token["WorkspaceAccessPolicy | None"],
) -> None:
    """Restore the prior task-local policy binding."""

    _CURRENT_POLICY.reset(token)


class WorkspaceAccessPolicy:
    """Authorize exact workspace operations according to the live runtime mode."""

    _PATH_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
        "read_file": (("path", "read"),),
        "read_lines": (("path", "read"),),
        "get_file_info": (("path", "metadata"),),
        "file_exists": (("path", "metadata"),),
        "write_file": (("path", "write"),),
        "append_file": (("path", "write"),),
        "patch_file": (("path", "write"),),
        "truncate_file": (("path", "write"),),
        "touch_file": (("path", "write"),),
        "make_directory": (("path", "write"),),
        "delete_file": (("path", "delete"),),
        "list_directory": (("path", "list"),),
        "search_files": (("path", "search"),),
        "grep_files": (("path", "search"),),
        "grep_file": (("path", "search"),),
        "apply_diff": (("path", "write"),),
        "checksum_file": (("path", "read"),),
        "move_file": (("source", "write"), ("destination", "write")),
        "copy_file": (("source", "read"), ("destination", "write")),
        "run_bash": (("cwd", "execute_cwd"),),
        "shell": (("cwd", "execute_cwd"),),
        "run_command": (("cwd", "execute_cwd"),),
        "run_python": (("cwd", "execute_cwd"),),
        "run_python_expr": (("cwd", "execute_cwd"),),
        "run_tests": (("cwd", "execute_cwd"), ("path", "execute_path")),
        "mention_read": (("path", "read"),),
        "mention_list": (("path", "list"),),
        "mention_search": (("path", "search"),),
        # Git commands always inspect or mutate the repository named by their
        # execution root, even when the individual command has no path
        # argument.  The adapter supplies ``__workspace_root`` internally;
        # it is deliberately not part of the model-facing tool schema.
        "git_status": (("__workspace_root", "git_root"),),
        "git_diff": (("__workspace_root", "git_root"), ("path", "read")),
        "git_log": (("__workspace_root", "git_root"), ("path", "read")),
        "git_show": (("__workspace_root", "git_root"),),
        "git_add": (("__workspace_root", "git_root"), ("paths", "write")),
        "git_commit": (("__workspace_root", "git_root"),),
        "git_checkout": (("__workspace_root", "git_root"),),
        "git_branch": (("__workspace_root", "git_root"),),
        "git_stash": (("__workspace_root", "git_root"),),
        "git_blame": (("__workspace_root", "git_root"), ("path", "read")),
        "git_grep": (("__workspace_root", "git_root"),),
    }

    def __init__(
        self,
        scope: WorkspaceScope,
        *,
        mode_provider: Callable[[], object] | None = None,
        approval_service: "ApprovalService | None" = None,
    ) -> None:
        self.scope = scope
        self._mode_provider = mode_provider
        self._approval_service = approval_service
        self._turn_grants: set[tuple[str, str]] = set()
        self._session_grants: set[tuple[str, str]] = set()

    def set_approval_service(self, service: "ApprovalService | None") -> None:
        """Replace the session approval adapter when the client changes.

        Headless entry points swap the interactive adapter for a deterministic
        fail-closed adapter after session construction; keeping this mutation
        at the policy boundary avoids stale references and UI waits.
        """

        self._approval_service = service

    @property
    def mode_name(self) -> str:
        value = self._mode_provider() if self._mode_provider is not None else "Safe"
        try:
            name = object.__getattribute__(value, "name")
        except AttributeError:
            name = value
        return name if isinstance(name, str) else "Safe"

    @property
    def mode(self) -> WorkspaceAccessMode:
        return (
            WorkspaceAccessMode.UNRESTRICTED
            if self.mode_name.casefold() == "yolo"
            else WorkspaceAccessMode.SCOPED
        )

    def requests_for_tool(
        self,
        tool_name: str,
        tool_input: Mapping[str, object],
    ) -> tuple[tuple[str, str], ...]:
        """Return declared path arguments for a built-in tool.

        Empty cwd/path defaults are deliberately omitted because they resolve
        to the primary root.  Arbitrary shell command strings are not parsed;
        command tools must declare path arguments if they need path-specific
        policy beyond their working directory.
        """

        fields = self._PATH_FIELDS.get(tool_name, ())
        values: list[tuple[str, str]] = []
        for field, operation in fields:
            value = tool_input.get(field)
            if isinstance(value, str) and value:
                values.append((value, operation))
            elif field == "path" and tool_name == "run_tests" and value is None:
                values.append(("tests/", operation))
        if tool_name.startswith("batch_"):
            if tool_name == "batch_read" or tool_name == "batch_delete":
                raw = tool_input.get("paths")
                if isinstance(raw, list):
                    values.extend(
                        (item, "read" if tool_name == "batch_read" else "delete")
                        for item in raw
                        if isinstance(item, str)
                    )
            elif tool_name in {"batch_write"}:
                raw = tool_input.get("files")
                if isinstance(raw, list):
                    values.extend(
                        (item["path"], "write")
                        for item in raw
                        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
                    )
            elif tool_name in {"batch_move", "batch_copy"}:
                raw = tool_input.get("moves") or tool_input.get("copies")
                if isinstance(raw, list):
                    source_operation = "write" if tool_name == "batch_move" else "read"
                    for item in raw:
                        if isinstance(item, Mapping):
                            if isinstance(item.get("source"), str):
                                values.append((item["source"], source_operation))
                            if isinstance(item.get("destination"), str):
                                values.append((item["destination"], "write"))
        if tool_name == "git_add":
            raw = tool_input.get("paths")
            if isinstance(raw, list):
                values.extend((item, "write") for item in raw if isinstance(item, str))
        if tool_name in {"list_directory", "search_files", "grep_files"}:
            pattern = tool_input.get("pattern")
            path = tool_input.get("path", ".")
            if isinstance(pattern, str) and isinstance(path, str) and _pattern_can_escape(pattern):
                operation = "list" if tool_name == "list_directory" else "search"
                combined = _combine_pattern_path(path, pattern)
                values.append((combined, operation))
        return tuple(values)

    async def authorize_tool(
        self,
        tool_name: str,
        tool_input: Mapping[str, object],
        capabilities: frozenset[str] = frozenset(),
        *,
        bind_current: bool = True,
    ) -> WorkspaceAccessResult:
        """Preflight all declared paths for one tool call.

        Safe combines outside-workspace requests and capability reasons into a
        single approval request.  The concrete adapter calls :meth:`authorize`
        again immediately before I/O; a task-local one-call grant prevents a
        duplicate prompt while retaining final target revalidation.
        """

        fields = self.requests_for_tool(tool_name, tool_input)
        if not fields:
            return WorkspaceAccessResult(allowed=True)
        resolved: list[ResolvedWorkspacePath] = []
        for requested, operation in fields:
            try:
                item = (
                    self.scope.resolve_pattern(
                        requested,
                        operation=operation,
                        probe_exists=False,
                    )
                    if any(char in requested for char in "*?[")
                    else self.scope.resolve(
                        requested,
                        operation=operation,
                        probe_exists=False,
                    )
                )
            except (OSError, ValueError) as exc:
                return WorkspaceAccessResult(
                    allowed=False,
                    code=WorkspacePathStatus.INVALID.value,
                    error=f"invalid_path: {exc}",
                )
            resolved.append(item)
        outside = [item for item in resolved if not item.in_scope]
        if not outside:
            return WorkspaceAccessResult(allowed=True, decisions=tuple(resolved))
        if self.mode_name.casefold() == "plan":
            return self._denied(
                resolved, "outside_workspace", "Plan mode cannot access outside the workspace"
            )
        if self.mode is WorkspaceAccessMode.UNRESTRICTED:
            return WorkspaceAccessResult(
                allowed=True, decisions=tuple(resolved), code="yolo_bypass"
            )

        access_requests = tuple(
            WorkspaceAccessRequest(
                requested=item.requested,
                canonical=item.absolute,
                display=item.display,
                operation=item.operation,
                tool_name=tool_name,
                exists=item.exists,
                workspace_root=self.scope.primary_root,
                mode=self.mode_name,
                capabilities=capabilities,
            )
            for item in outside
        )
        keys = {request.grant_key for request in access_requests}
        current = _CURRENT_GRANT.get()
        if (
            current is not None
            and current.policy is self
            and current.tool_name == tool_name
            and keys <= current.grants
        ):
            return WorkspaceAccessResult(
                allowed=True,
                decisions=tuple(resolved),
                approval_handled=True,
                code="scope_grant",
            )
        if keys <= self._session_grants or keys <= self._turn_grants:
            if bind_current:
                self._bind_current_grant(
                    tool_name,
                    keys,
                    {(item.requested, item.operation) for item in access_requests},
                )
            return WorkspaceAccessResult(
                allowed=True,
                decisions=tuple(resolved),
                approval_handled=True,
                code="scope_grant",
            )

        if self._approval_service is None:
            return self._denied(
                resolved,
                "approval_unavailable",
                "outside_workspace access requires an interactive approval service",
            )

        import asyncio  # noqa: PLC0415
        from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415

        req = ApprovalRequest(
            tool_name=tool_name,
            tool_use_id="",
            tool_input=dict(tool_input),
            capabilities=capabilities,
            event=asyncio.Event(),
            workspace_access=access_requests,
        )
        response = await self._await_scope_approval(req)
        if not response.allowed:
            if self.mode_name.casefold() == "plan":
                return self._denied(
                    resolved,
                    "outside_workspace",
                    "Plan mode cannot access outside the workspace",
                )
            return self._denied(
                resolved, "approval_denied", "outside_workspace approval was denied"
            )

        if self.mode is WorkspaceAccessMode.UNRESTRICTED:
            return WorkspaceAccessResult(
                allowed=True,
                decisions=tuple(resolved),
                approval_handled=True,
                code="yolo_bypass",
            )
        grant = response.scope_grant
        if grant == "target_session":
            self._session_grants.update(keys)
        elif grant == "target_turn":
            self._turn_grants.update(keys)
        if bind_current:
            self._bind_current_grant(
                tool_name,
                keys,
                {(item.requested, item.operation) for item in access_requests},
            )
        return WorkspaceAccessResult(
            allowed=True,
            decisions=tuple(resolved),
            approval_handled=True,
            code="approved",
        )

    async def _await_scope_approval(self, request: object) -> "ApprovalResponse":
        """Await approval while observing a live runtime-mode signal.

        ``AppState.active_mode`` is a subscribable signal.  If a pending Safe
        request is switched to Plan or Yolo, cancel the old approval task and
        let the new mode decide the access immediately. Plain callable mode
        providers retain the ordinary await path because they have no change
        notification contract.
        """

        import asyncio  # noqa: PLC0415
        from contextlib import suppress  # noqa: PLC0415

        provider = self._mode_provider
        subscribe = None
        if provider is not None:
            try:
                subscribe = object.__getattribute__(provider, "subscribe")
            except AttributeError:
                subscribe = None
        if not callable(subscribe):
            service = self._approval_service
            if service is None:
                raise RuntimeError("workspace approval service is unavailable")
            return await service.request_approval(request)

        changed = asyncio.Event()
        unsubscribe = subscribe(lambda: changed.set())
        approval_task = asyncio.create_task(self._request_approval(request))
        mode_task = asyncio.create_task(changed.wait())
        try:
            done, _ = await asyncio.wait(
                {approval_task, mode_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if mode_task in done and self.mode_name.casefold() in {"plan", "yolo"}:
                approval_task.cancel()
                with suppress(asyncio.CancelledError):
                    await approval_task
                from agenthicc.tools.approval import ApprovalResponse  # noqa: PLC0415

                is_yolo = self.mode_name.casefold() == "yolo"
                return ApprovalResponse(
                    allowed=is_yolo,
                    message="workspace approval re-evaluated after mode change",
                    scope_grant="target_once" if is_yolo else None,
                )
            return await approval_task
        finally:
            mode_task.cancel()
            with suppress(asyncio.CancelledError):
                await mode_task
            unsubscribe()

    async def _request_approval(self, request: object) -> "ApprovalResponse":
        service = self._approval_service
        if service is None:
            raise RuntimeError("workspace approval service is unavailable")
        return await service.request_approval(request)

    async def authorize(
        self,
        requested: str | Path,
        *,
        operation: str,
        tool_name: str,
        capabilities: frozenset[str] = frozenset(),
        pattern: bool = False,
        tool_input: Mapping[str, object] | None = None,
    ) -> Path:
        """Authorize one concrete adapter access and return its canonical path."""

        current = self._consume_current(tool_name, requested, operation, pattern=pattern)
        if current is False:
            raise PermissionError("target_changed: canonical target changed after approval")
        if current:
            try:
                resolved = (
                    self.scope.resolve_pattern(requested, operation=operation)
                    if pattern
                    else self.scope.resolve(requested, operation=operation)
                )
            except (OSError, ValueError) as exc:
                raise PermissionError(f"invalid_path: {exc}") from exc
            try:
                revalidated = self.scope.revalidate(resolved, pattern=pattern)
            except (OSError, ValueError) as exc:
                raise PermissionError(f"invalid_path: {exc}") from exc
            if revalidated.status is WorkspacePathStatus.TARGET_CHANGED:
                self.clear_current_grant(tool_name)
                raise PermissionError("target_changed: canonical target changed after approval")
            if not revalidated.in_scope and self.mode_name.casefold() == "plan":
                self.clear_current_grant(tool_name)
                raise PermissionError(
                    "outside_workspace: Plan mode cannot access outside the workspace"
                )
            return revalidated.absolute

        try:
            resolved = (
                self.scope.resolve_pattern(
                    requested,
                    operation=operation,
                    probe_exists=False,
                )
                if pattern
                else self.scope.resolve(
                    requested,
                    operation=operation,
                    probe_exists=False,
                )
            )
        except (OSError, ValueError) as exc:
            raise PermissionError(f"invalid_path: {exc}") from exc
        result = await self.authorize_tool(
            tool_name,
            tool_input or {"path": os.fspath(requested)},
            capabilities,
        )
        if not result.allowed:
            raise PermissionError(f"{result.code}: {result.error}")
        try:
            revalidated = self.scope.revalidate(resolved, pattern=pattern)
        except (OSError, ValueError) as exc:
            raise PermissionError(f"invalid_path: {exc}") from exc
        if revalidated.status is WorkspacePathStatus.TARGET_CHANGED:
            self.clear_current_grant(tool_name)
            raise PermissionError("target_changed: canonical target changed after approval")
        if not revalidated.in_scope and self.mode_name.casefold() == "plan":
            self.clear_current_grant(tool_name)
            raise PermissionError(
                "outside_workspace: Plan mode cannot access the outside workspace"
            )
        if (
            not revalidated.in_scope
            and self.mode is WorkspaceAccessMode.SCOPED
            and result.code == "yolo_bypass"
        ):
            # Yolo may have been active for preflight but changed to Safe
            # before the adapter reached I/O. Re-enter the live policy rather
            # than allowing a stale unrestricted decision through.
            result = await self.authorize_tool(
                tool_name,
                tool_input or {"path": os.fspath(requested)},
                capabilities,
            )
            if not result.allowed:
                raise PermissionError(f"{result.code}: {result.error}")
            try:
                revalidated = self.scope.revalidate(resolved, pattern=pattern)
            except (OSError, ValueError) as exc:
                raise PermissionError(f"invalid_path: {exc}") from exc
            if revalidated.status is WorkspacePathStatus.TARGET_CHANGED:
                self.clear_current_grant(tool_name)
                raise PermissionError("target_changed: canonical target changed after approval")
        # ``authorize_tool`` binds the approval to the adapter hand-off.  A
        # direct adapter call must consume that one-call grant before it
        # returns; otherwise a target-once response could leak into the next
        # invocation in the same task.
        post_grant = self._consume_current(tool_name, requested, operation, pattern=pattern)
        if post_grant is False:
            raise PermissionError("target_changed: canonical target changed after approval")
        if not revalidated.in_scope and self.mode_name.casefold() == "plan":
            self.clear_current_grant(tool_name)
            raise PermissionError(
                "outside_workspace: Plan mode cannot access the outside workspace"
            )
        return revalidated.absolute

    def reset_turn_memory(self) -> None:
        """Clear temporary outside-target grants at the start of a new turn."""

        self._turn_grants.clear()
        current = _CURRENT_GRANT.get()
        if current is not None and current.policy is self:
            _CURRENT_GRANT.set(None)

    def clear_current_grant(self, tool_name: str | None = None) -> None:
        """Discard the adapter hand-off grant after direct tool execution."""

        current = _CURRENT_GRANT.get()
        if current is not None and (tool_name is None or current.tool_name == tool_name):
            _CURRENT_GRANT.set(None)

    def _bind_current_grant(
        self,
        tool_name: str,
        keys: set[tuple[str, str]],
        requested: set[tuple[str, str]],
    ) -> None:
        _CURRENT_GRANT.set(
            _CurrentToolGrant(self, tool_name, frozenset(keys), frozenset(requested))
        )

    def _consume_current(
        self,
        tool_name: str,
        requested: str | Path,
        operation: str,
        *,
        pattern: bool = False,
    ) -> bool | None:
        current = _CURRENT_GRANT.get()
        if current is None or current.policy is not self or current.tool_name != tool_name:
            return None
        try:
            canonical = (
                self.scope.resolve_pattern(requested, operation=operation).absolute
                if pattern
                else self.scope.resolve(requested, operation=operation).absolute
            )
        except (OSError, ValueError):
            return False
        key = (str(canonical), operation)
        if key not in current.grants:
            # A multi-target operation may carry a grant for one outside
            # target while its other target is already in scope. Preserve the
            # hand-off for that legitimate in-scope target; only an unmatched
            # outside target indicates that the approved canonical identity
            # changed between preflight and I/O.
            if (os.fspath(requested), operation) in current.requested:
                _CURRENT_GRANT.set(None)
                return False
            return None
        remaining = current.grants - {key}
        _CURRENT_GRANT.set(
            _CurrentToolGrant(
                current.policy,
                current.tool_name,
                frozenset(remaining),
                current.requested,
            )
            if remaining
            else None
        )
        return True

    @staticmethod
    def _denied(
        decisions: list[ResolvedWorkspacePath], code: str, message: str
    ) -> WorkspaceAccessResult:
        return WorkspaceAccessResult(
            allowed=False,
            decisions=tuple(decisions),
            code=code,
            error=message,
        )


def _realpath(path: Path) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def _contains(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _root_id(root: Path) -> str:
    # Basenames are not unique when a session allows sibling repositories
    # such as ``/work/app`` and ``/other/app``.  Keep the identifier stable and
    # non-sensitive while the full root remains available only to the local
    # approval UI/diagnostic boundary.
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return f"root-{digest}"


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) or "."
    except ValueError:
        return str(path)


def _pattern_can_escape(pattern: str) -> bool:
    """Return whether a glob pattern can leave its caller-provided root."""

    path = Path(pattern)
    return path.is_absolute() or ".." in path.parts


def _combine_pattern_path(path: str, pattern: str) -> str:
    """Build the path whose literal prefix is the glob's authorization anchor."""

    pattern_path = Path(pattern)
    if pattern_path.is_absolute():
        return str(pattern_path)
    return str(Path(path) / pattern_path)
