"""Download and install user-defined skills (CLI support)."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from agenthicc.skills.loader import MAX_SKILL_NAME_LENGTH, _parse_skill, canonical_skill_name

_MAX_SKILL_BYTES = 1_048_576


class SkillInstallError(ValueError):
    """Raised when a skill source or installation target is invalid."""


@dataclass(frozen=True)
class SkillInstallResult:
    """Description of a successfully installed skill."""

    slug: str
    path: Path
    scope: str


def _skill_root(
    *,
    global_scope: bool,
    project_dir: Path | None,
    user_dir: Path | None,
) -> Path:
    if global_scope:
        return (user_dir or Path.home() / ".agenthicc") / "skills"
    return (project_dir or Path.cwd() / ".agenthicc") / "skills"


def _decode_skill(content: bytes, source: str) -> str:
    if len(content) > _MAX_SKILL_BYTES:
        raise SkillInstallError(
            f"skill source is too large ({len(content)} bytes; maximum is {_MAX_SKILL_BYTES})"
        )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillInstallError(f"skill source is not valid UTF-8: {source}") from exc


def _local_source(source: str) -> tuple[str, str]:
    candidate = Path(source).expanduser()
    if not candidate.exists():
        raise SkillInstallError(f"skill source does not exist: {source}")
    skill_file = candidate / "SKILL.md" if candidate.is_dir() else candidate
    if not skill_file.is_file():
        raise SkillInstallError(f"skill source does not contain a readable SKILL.md: {source}")
    try:
        content = _decode_skill(skill_file.read_bytes(), str(skill_file))
    except OSError as exc:
        raise SkillInstallError(f"could not read skill source: {exc}") from exc
    inferred = candidate.name if candidate.is_dir() else skill_file.parent.name
    return content, inferred


def _github_raw_url(source: str) -> str:
    """Convert common GitHub blob/tree links into a raw SKILL.md URL."""
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if host not in {"github.com", "www.github.com"}:
        if parsed.path.lower().endswith("/skill.md"):
            return source
        raise SkillInstallError("HTTPS skill URLs must point directly to SKILL.md")

    if len(parts) >= 5 and parts[2] in {"blob", "tree"}:
        owner, repository, mode, revision = parts[:4]
        remainder = parts[4:]
        if not remainder:
            raise SkillInstallError("GitHub skill URL must identify a skill directory")
        if mode == "tree":
            remainder.append("SKILL.md")
        elif remainder[-1].lower() != "skill.md":
            raise SkillInstallError("GitHub blob URL must point to SKILL.md")
        return "https://raw.githubusercontent.com/" + "/".join(
            [owner, repository, revision, *remainder]
        )

    raise SkillInstallError(
        "GitHub skill URLs must use /tree/<revision>/<skill> or /blob/<revision>/<skill>/SKILL.md"
    )


def _remote_name(source: str) -> str:
    parsed = urlparse(source)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parts and parts[-1].lower() == "skill.md":
        return parts[-2] if len(parts) > 1 else ""
    return parts[-1] if parts else ""


async def _remote_source(source: str) -> tuple[str, str]:
    parsed = urlparse(source)
    if parsed.scheme != "https":
        raise SkillInstallError("remote skill sources must use HTTPS")
    url = _github_raw_url(source)

    from agenthicc.tools.http import agenthicc_http_client, is_network_error  # noqa: PLC0415

    try:
        async with agenthicc_http_client(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"Accept": "text/markdown, text/plain"})
            response.raise_for_status()
            content = _decode_skill(response.content, source)
    except SkillInstallError:
        raise
    except Exception as exc:  # noqa: BLE001
        if is_network_error(exc):
            raise SkillInstallError("skill download failed due to a network error") from exc
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int):
            raise SkillInstallError(f"skill download failed with HTTP status {status}") from exc
        raise SkillInstallError(f"skill download failed: {type(exc).__name__}") from exc
    return content, _remote_name(source)


async def _read_source(source: str) -> tuple[str, str]:
    parsed = urlparse(source)
    if parsed.scheme:
        return await _remote_source(source)
    return _local_source(source)


def _normalise_install_name(name: str, inferred: str) -> str:
    raw = name.strip() or inferred.strip()
    slug = canonical_skill_name(raw)
    if not slug:
        raise SkillInstallError("could not determine a skill name; pass --name NAME")
    if len(slug) > MAX_SKILL_NAME_LENGTH:
        raise SkillInstallError(
            f"skill name is too long (maximum is {MAX_SKILL_NAME_LENGTH} characters)"
        )
    return slug


async def install_skill(
    source: str,
    *,
    global_scope: bool = False,
    project_scope: bool = False,
    name: str = "",
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> SkillInstallResult:
    """Fetch, validate, and atomically install one skill directory.

    ``source`` may be a local skill directory/file or an HTTPS URL pointing to
    ``SKILL.md``. GitHub ``blob`` and ``tree`` URLs are normalized to raw file
    URLs. Project scope is the default; ``global_scope`` selects the user
    directory. Existing skill directories are never overwritten.
    """
    if global_scope and project_scope:
        raise SkillInstallError("choose only one target: --global or --project")
    source = source.strip()
    if not source:
        raise SkillInstallError("a skill URL or local path is required")

    content, inferred_name = await _read_source(source)
    slug = _normalise_install_name(name, inferred_name)
    root = _skill_root(
        global_scope=global_scope,
        project_dir=project_dir,
        user_dir=user_dir,
    )
    target = root / slug
    if target.exists() or target.is_symlink():
        raise SkillInstallError(f"skill already exists: {target}")

    staging: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{slug}-install-", dir=root))
        (staging / "SKILL.md").write_text(content, encoding="utf-8")
        if _parse_skill(staging) is None:
            raise SkillInstallError("downloaded SKILL.md failed skill metadata validation")
        os.replace(staging, target)
    except SkillInstallError:
        raise
    except OSError as exc:
        raise SkillInstallError(f"could not install skill at {target}: {exc}") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return SkillInstallResult(
        slug=slug,
        path=target,
        scope="global" if global_scope else "project",
    )
