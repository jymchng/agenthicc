"""Download and install user-defined skills (CLI support)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from agenthicc.skills.loader import MAX_SKILL_NAME_LENGTH, _parse_skill, canonical_skill_name

_MAX_SKILL_BYTES = 1_048_576
_GIT_CLONE_TIMEOUT_SECONDS = 300
_MAX_DISCOVERY_DEPTH = 5
_SKILL_DISCOVERY_SKIP_DIRS = frozenset({".git", "node_modules", "dist", "build", "__pycache__"})
_KNOWN_SKILL_CONTAINERS = frozenset(
    {
        ".agents/skills",
        ".claude/skills",
        ".cline/skills",
        ".codex/skills",
        ".github/skills",
        ".opencode/skills",
        ".openhands/skills",
        ".roo/skills",
        ".windsurf/skills",
    }
)


class SkillInstallError(ValueError):
    """Raised when a skill source or installation target is invalid."""


@dataclass(frozen=True)
class SkillInstallResult:
    """Description of a successfully installed skill."""

    slug: str
    path: Path
    scope: str


@dataclass(frozen=True)
class _GitRepositorySource:
    """Normalized Git repository source and optional checkout subpath."""

    url: str
    revision: str | None = None
    subpath: str | None = None


@dataclass(frozen=True)
class _DiscoveredSkill:
    """A validated skill directory found in a repository source."""

    path: Path
    slug: str
    display_name: str


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


def _github_repository_source(source: str) -> _GitRepositorySource | None:
    """Parse a GitHub repository URL, tree URL, or ``owner/repo`` shorthand."""
    parsed = urlparse(source)
    if parsed.scheme:
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
            "github.com",
            "www.github.com",
        }:
            return None
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            return None
        owner, repository = parts[:2]
        repository = repository.removesuffix(".git")
        if not owner or not repository:
            return None
        url = f"https://github.com/{owner}/{repository}.git"
        if len(parts) == 2:
            return _GitRepositorySource(url, parsed.fragment or None)
        if len(parts) >= 4 and parts[2] == "tree":
            revision = parts[3]
            subpath = "/".join(parts[4:]) or None
            return _GitRepositorySource(url, revision, subpath)
        return None

    if source.count("/") == 1 and not source.startswith((".", "/")):
        owner, repository = source.split("/", 1)
        if owner and repository and not any(char in source for char in "\\:"):
            repository = repository.removesuffix(".git")
            if repository:
                return _GitRepositorySource(f"https://github.com/{owner}/{repository}.git")
    return None


def _repository_source(source: str) -> _GitRepositorySource | None:
    """Return a repository source for GitHub URLs and local repository roots."""
    github_source = _github_repository_source(source)
    if github_source is not None:
        return github_source

    candidate = Path(source).expanduser()
    if candidate.is_dir() and not (candidate / "SKILL.md").is_file():
        return _GitRepositorySource(str(candidate.resolve()))
    return None


async def _clone_repository(source: _GitRepositorySource) -> tuple[Path, Path]:
    """Clone one repository into a disposable directory and return its root."""
    if not source.url.startswith("https://") and not Path(source.url).is_dir():
        raise SkillInstallError("remote skill sources must use HTTPS")

    temporary_root = Path(tempfile.mkdtemp(prefix="agenthicc-skills-source-"))
    repository_root = temporary_root / "repository"
    if Path(source.url).is_dir():
        try:
            shutil.copytree(source.url, repository_root, symlinks=True)
        except OSError as exc:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise SkillInstallError(f"could not prepare local skill repository: {exc}") from exc
        return temporary_root, repository_root

    command = [
        "git",
        "-c",
        "protocol.ext.allow=never",
        "clone",
        "--depth",
        "1",
        "--no-tags",
    ]
    if source.revision:
        command.extend(["--branch", source.revision])
    command.extend([source.url, str(repository_root)])
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_ALLOW_PROTOCOL"] = "https:http"
    environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_CLONE_TIMEOUT_SECONDS,
            env=environment,
        )
    except FileNotFoundError as exc:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise SkillInstallError("git is required to install a GitHub repository skill") from exc
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise SkillInstallError("skill repository clone timed out") from exc
    except OSError as exc:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise SkillInstallError(f"could not start git clone: {exc}") from exc

    if completed.returncode != 0:
        shutil.rmtree(temporary_root, ignore_errors=True)
        detail = (
            completed.stderr.strip().splitlines()[-1]
            if completed.stderr.strip()
            else "unknown error"
        )
        raise SkillInstallError(f"skill repository clone failed: {detail}")
    return temporary_root, repository_root


def _discovery_priority(path: Path, repository_root: Path) -> tuple[int, int, str]:
    relative = path.relative_to(repository_root).parent
    relative_text = relative.as_posix()
    if relative == Path("."):
        priority = 0
    elif relative_text == "skills" or relative_text.startswith("skills/"):
        priority = 1
    elif any(
        relative_text == container or relative_text.startswith(f"{container}/")
        for container in _KNOWN_SKILL_CONTAINERS
    ):
        priority = 2
    else:
        priority = 3
    return priority, len(relative.parts), relative_text


def _discover_repository_skills(
    repository_root: Path, subpath: str | None = None
) -> list[_DiscoveredSkill]:
    """Find and validate repository skills with deterministic de-duplication."""
    search_root = repository_root
    if subpath:
        search_root = (repository_root / subpath).resolve()
        repository_resolved = repository_root.resolve()
        if search_root != repository_resolved and repository_resolved not in search_root.parents:
            raise SkillInstallError("repository skill path must stay inside the repository")
    if not search_root.is_dir():
        raise SkillInstallError(f"repository skill path does not exist: {subpath}")

    candidates: list[Path] = []
    for current, directories, files in os.walk(search_root, followlinks=False):
        current_path = Path(current)
        relative_parts = current_path.relative_to(search_root).parts
        if len(relative_parts) >= _MAX_DISCOVERY_DEPTH:
            directories[:] = []
        else:
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in _SKILL_DISCOVERY_SKIP_DIRS
                and not (current_path / directory).is_symlink()
            )
        if "SKILL.md" in files and not (current_path / "SKILL.md").is_symlink():
            candidates.append(current_path)

    discovered: list[_DiscoveredSkill] = []
    seen_slugs: set[str] = set()
    for path in sorted(candidates, key=lambda item: _discovery_priority(item, repository_root)):
        parsed = _parse_skill(path)
        if parsed is None or parsed.slug in seen_slugs:
            continue
        seen_slugs.add(parsed.slug)
        discovered.append(_DiscoveredSkill(path, parsed.slug, parsed.name))
    return discovered


def _select_repository_skills(
    skills: Iterable[_DiscoveredSkill], requested: tuple[str, ...]
) -> list[_DiscoveredSkill]:
    available = list(skills)
    if not requested:
        return available
    wanted = {canonical_skill_name(name) for name in requested}
    selected = [
        skill
        for skill in available
        if skill.slug in wanted or canonical_skill_name(skill.display_name) in wanted
    ]
    if not selected:
        names = ", ".join(skill.slug for skill in available)
        raise SkillInstallError(
            f"no matching skills found for: {', '.join(requested)}; available skills: {names}"
        )
    return selected


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


def _install_content(
    content: str,
    *,
    inferred_name: str,
    name: str,
    root: Path,
    global_scope: bool,
) -> SkillInstallResult:
    """Install one fetched SKILL.md using the legacy single-file path."""
    slug = _normalise_install_name(name, inferred_name)
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


def _copy_skill_tree(source: Path, destination: Path) -> None:
    """Copy a skill directory while rejecting symlinked content."""
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            if (current_path / directory).is_symlink():
                raise SkillInstallError(
                    f"skill contains unsupported symlink: {current_path / directory}"
                )
        for filename in files:
            source_file = current_path / filename
            if source_file.is_symlink():
                raise SkillInstallError(f"skill contains unsupported symlink: {source_file}")
            try:
                shutil.copy2(source_file, target_dir / filename)
            except OSError as exc:
                raise SkillInstallError(f"could not copy skill file {source_file}: {exc}") from exc


def _install_directory(
    source: _DiscoveredSkill,
    *,
    name: str,
    root: Path,
    global_scope: bool,
) -> SkillInstallResult:
    """Stage and install a complete repository skill directory."""
    slug = _normalise_install_name(name, source.slug)
    target = root / slug
    if target.exists() or target.is_symlink():
        raise SkillInstallError(f"skill already exists: {target}")

    staging: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{slug}-install-", dir=root))
        _copy_skill_tree(source.path, staging)
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

    return SkillInstallResult(slug=slug, path=target, scope="global" if global_scope else "project")


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

    root = _skill_root(
        global_scope=global_scope,
        project_dir=project_dir,
        user_dir=user_dir,
    )
    content, inferred_name = await _read_source(source)
    result = _install_content(
        content,
        inferred_name=inferred_name,
        name=name,
        root=root,
        global_scope=global_scope,
    )
    return result


async def install_skills(
    source: str,
    *,
    global_scope: bool = False,
    project_scope: bool = False,
    name: str = "",
    skill_names: tuple[str, ...] = (),
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> tuple[SkillInstallResult, ...]:
    """Install one direct skill or all selected skills from a repository source.

    Repository sources follow the same useful defaults as ``npx skills add``:
    conventional skill directories are discovered, duplicate copies are
    collapsed by canonical name, and all discovered skills are installed when
    no ``skill_names`` filter is supplied.
    """
    if global_scope and project_scope:
        raise SkillInstallError("choose only one target: --global or --project")
    source = source.strip()
    if not source:
        raise SkillInstallError("a skill URL or local path is required")

    repository = _repository_source(source)
    if repository is None:
        return (
            await install_skill(
                source,
                global_scope=global_scope,
                project_scope=project_scope,
                name=name,
                project_dir=project_dir,
                user_dir=user_dir,
            ),
        )

    temporary_root, repository_root = await _clone_repository(repository)
    try:
        discovered = _discover_repository_skills(repository_root, repository.subpath)
        if not discovered:
            raise SkillInstallError(
                "repository does not contain a valid SKILL.md; pass a direct skill URL or "
                "a repository path containing SKILL.md"
            )
        selected = _select_repository_skills(discovered, skill_names)
        if name and len(selected) != 1:
            raise SkillInstallError("--name can only be used when installing one repository skill")
        root = _skill_root(
            global_scope=global_scope,
            project_dir=project_dir,
            user_dir=user_dir,
        )
        planned_names = [_normalise_install_name(name, skill.slug) for skill in selected]
        for slug in planned_names:
            target = root / slug
            if target.exists() or target.is_symlink():
                raise SkillInstallError(f"skill already exists: {target}")
        results: list[SkillInstallResult] = []
        for skill, planned_name in zip(selected, planned_names, strict=True):
            results.append(
                _install_directory(
                    skill,
                    name=planned_name,
                    root=root,
                    global_scope=global_scope,
                )
            )
        return tuple(results)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
