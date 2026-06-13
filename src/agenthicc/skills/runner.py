"""Skills runtime: context injection, arg substitution, auto-triggering (PRD-23)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.skills.loader import SkillDef

__all__ = [
    "inject_context",
    "substitute_args",
    "load_template",
    "maybe_load_reference",
    "find_matching_skills",
    "process_skill_body",
]

# Matches !`shell command` placeholders
_INJECT_RE = re.compile(r"!`([^`]+)`")


def inject_context(body: str, cwd: Path) -> str:
    """Replace !`shell command` placeholders with their stdout output."""

    def _run(match: re.Match) -> str:
        cmd = match.group(1)
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.stdout.strip()
        except Exception as exc:  # noqa: BLE001
            return f"[context injection failed: {exc}]"

    return _INJECT_RE.sub(_run, body)


def substitute_args(
    body: str,
    args: list[str],
    session_id: str = "",
    effort: str = "medium",
) -> str:
    """Replace {session}, {effort}, and positional {0}, {1}, ... placeholders."""
    body = body.replace("{session}", session_id)
    body = body.replace("{effort}", effort)

    def _replace_index(match: re.Match) -> str:
        idx = int(match.group(1))
        return args[idx] if idx < len(args) else ""

    body = re.sub(r"\{(\d+)\}", _replace_index, body)
    return body


def load_template(skill_dir: Path) -> str:
    """Return a template suffix if skill_dir/template.md exists, else empty string."""
    template_path = skill_dir / "template.md"
    if template_path.exists():
        return "\n\n---\n\n" + template_path.read_text(encoding="utf-8").strip()
    return ""


def maybe_load_reference(body: str, skill_dir: Path) -> str:
    """If body contains {reference}, replace it with the contents of reference.md."""
    if "{reference}" not in body:
        return body
    reference_path = skill_dir / "reference.md"
    if reference_path.exists():
        content = reference_path.read_text(encoding="utf-8")
        return body.replace("{reference}", content)
    return body.replace("{reference}", "[reference.md not found]")


def find_matching_skills(
    user_message: str,
    skills: dict[str, SkillDef],
) -> list[SkillDef]:
    """Return skills whose suggested_topics overlap with words in user_message."""
    words = set(re.findall(r"\w+", user_message.lower()))
    matches: list[SkillDef] = []
    for skill in skills.values():
        if skill.disallow_auto_triggering:
            continue
        for topic in skill.suggested_topics:
            if topic.lower() in words:
                matches.append(skill)
                break
    return matches


def process_skill_body(
    skill: SkillDef,
    args: list[str],
    cwd: Path,
    session_id: str = "",
    effort: str = "medium",
) -> str:
    """Produce the final skill body by running all processing steps in order."""
    body = skill.body
    body = inject_context(body, cwd)
    body = substitute_args(body, args, session_id=session_id, effort=effort)
    body = maybe_load_reference(body, skill.path)
    body += load_template(skill.path)
    return body
