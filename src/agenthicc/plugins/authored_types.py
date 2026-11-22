"""Runtime-owned descriptions and digests of staged extensions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal, TypeVar

if TYPE_CHECKING:
    from collections.abc import Mapping
    from agenthicc.workflows.authoring.verdict import ValidationReport

ArtifactKind = Literal["tool", "skill", "workflow", "command"]
ArtifactState = Literal["staged", "loaded", "load_failed", "rejected"]
ArtifactValidation = Literal["passed", "static_only"]
AuthoredPolicy = Literal["auto", "stage_only"]

_ARTIFACT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,127}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_Option = TypeVar("_Option", bound=str)
_KINDS: tuple[ArtifactKind, ...] = ("tool", "skill", "workflow", "command")
_STATES: tuple[ArtifactState, ...] = ("staged", "loaded", "load_failed", "rejected")
_VALIDATION_STATES: tuple[ArtifactValidation, ...] = ("passed", "static_only")


def _name(value: str, kind: ArtifactKind) -> str:
    if not _ARTIFACT_NAME.fullmatch(value):
        raise ValueError(f"Invalid authored {kind} name.")
    return value


def _run_id(value: str) -> str:
    if not _RUN_ID.fullmatch(value):
        raise ValueError("Invalid authoring run identifier.")
    return value


def _digest(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("Expected an authored artifact SHA-256 digest.")
    return value


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or value != path.as_posix()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or any(char in value for char in '\\:\u0000<>"|?*')
        or any(part.endswith((".", " ")) for part in path.parts)
    ):
        raise ValueError("Expected a canonical workspace-relative artifact path.")
    return path


def content_digest(files: Mapping[str, bytes]) -> str:
    """Hash the exact logical paths and byte sequences of an artifact."""
    digest = hashlib.sha256()
    for name, source in sorted(files.items()):
        path_bytes = _relative(name).as_posix().encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    """One approved extension and its publication state."""

    kind: ArtifactKind
    name: str
    files: tuple[str, ...]
    digest: str
    state: ArtifactState
    declared_capabilities: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    validation: ArtifactValidation = "passed"
    supersedes: str | None = None

    def __post_init__(self) -> None:
        _name(self.name, self.kind)
        _digest(self.digest)
        if self.supersedes is not None:
            _digest(self.supersedes)
        if not self.files or self.files != tuple(sorted(set(self.files))):
            raise ValueError("Authored artifact files must be a nonempty sorted set.")
        expected = (".agenthicc", f"{self.kind}s")
        for filename in self.files:
            path = _relative(filename)
            if path.parts[:2] != expected or len(path.parts) == 2:
                raise ValueError(f"Authored {self.kind} file is outside its live directory.")

    def to_dict(self) -> dict[str, object]:
        """Serialize this artifact for provenance and runtime events."""
        return {
            "kind": self.kind,
            "name": self.name,
            "files": list(self.files),
            "digest": self.digest,
            "state": self.state,
            "declared_capabilities": list(self.declared_capabilities),
            "network_hosts": list(self.network_hosts),
            "dependencies": list(self.dependencies),
            "advisories": list(self.advisories),
            "validation": self.validation,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_payload(cls, value: object) -> PublishedArtifact:
        """Decode a persisted artifact with the normal path and digest checks."""
        if not isinstance(value, dict):
            raise ValueError("Expected an authored artifact object.")
        data: Mapping[object, object] = value
        name = data.get("name")
        digest = data.get("digest")
        supersedes = data.get("supersedes")
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or (supersedes is not None and not isinstance(supersedes, str))
        ):
            raise ValueError("Invalid authored artifact metadata.")
        return cls(
            kind=_option(data.get("kind"), _KINDS),
            name=name,
            files=_strings(data.get("files")),
            digest=digest,
            state=_option(data.get("state"), _STATES),
            declared_capabilities=_strings(data.get("declared_capabilities")),
            network_hosts=_strings(data.get("network_hosts")),
            dependencies=_strings(data.get("dependencies")),
            advisories=_strings(data.get("advisories")),
            validation=_option(data.get("validation"), _VALIDATION_STATES),
            supersedes=supersedes,
        )


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    """An artifact paired with the runner's verdict on its exact bytes."""

    artifact: PublishedArtifact
    verdict: ValidationReport


@dataclass(frozen=True, slots=True)
class AuthoredRun:
    """The complete candidate set and provenance for one authoring run."""

    run_id: str
    session_id: str
    model: str
    candidates: tuple[StagedArtifact, ...]

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        names = {(item.artifact.kind, item.artifact.name) for item in self.candidates}
        if not self.candidates or len(names) != len(self.candidates):
            raise ValueError("An authoring run needs a nonempty set of distinct artifacts.")


def _option(value: object, options: tuple[_Option, ...]) -> _Option:
    for option in options:
        if option == value:
            return option
    raise ValueError("Invalid authored artifact metadata.")


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected a list of authored artifact strings.")
    items = tuple(item for item in value if isinstance(item, str))
    if len(items) != len(value):
        raise ValueError("Expected a list of authored artifact strings.")
    return items
