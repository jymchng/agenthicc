"""Validate a staged skill set with the same parser and command resolver as reload."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from agenthicc.plugins.authored import _source_bytes
from agenthicc.plugins.authored_types import content_digest
from agenthicc.skills.loader import _extract_body, _split_frontmatter, validate_skill_dir
from agenthicc.workflows.authoring.verdict import Finding, ValidationReport
from agenthicc.workflows.create_skills.plan import MAX_BODY_BYTES, MAX_FILE_BYTES

if TYPE_CHECKING:
    from agenthicc.skills.loader import SkillDef
    from agenthicc.workflows.create_skills.plan import SkillPlan

_ADVISORIES = {
    "skill.safety_override": re.compile(
        r"\b(?:ignore|disable|bypass)\b[^.\n]{0,60}"
        r"\b(?:approvals?|sandbox(?:ing)?|capability (?:limit|ceiling))\b",
        re.I,
    ),
    "skill.data_exfiltration": re.compile(
        r"\bexfiltrat\w*\b|\b(?:send|upload)\b[^.\n]{0,60}\b(?:credentials?|tokens?|secrets?)\b",
        re.I,
    ),
    "skill.remote_execution": re.compile(
        r"\b(?:fetch|download)\b[^.\n]{0,60}\b(?:and run|and execute|into a shell)\b",
        re.I,
    ),
}


def _read_skill_files(root: Path, plan: SkillPlan) -> tuple[dict[str, bytes], list[Finding]]:
    directory = root / plan.slug
    expected = {"SKILL.md", *plan.support_files}
    findings: list[Finding] = []
    try:
        if directory.is_symlink() or directory.resolve() != directory:
            raise ValueError("Unsafe skill directory.")
        actual = {path.name for path in directory.iterdir()}
    except (OSError, ValueError):
        actual = set()
    if expected != actual:
        findings.append(
            Finding(
                "skill.inventory",
                "error",
                plan.slug,
                None,
                "The staged skill files must match the plan exactly.",
            )
        )
    sources: dict[str, bytes] = {}
    for relative in sorted(actual & expected):
        path = f"{plan.slug}/{relative}"
        try:
            contents = _source_bytes(directory / relative)
            if len(contents) > MAX_FILE_BYTES:
                raise ValueError("File exceeds 64 KiB.")
            contents.decode("utf-8")
        except (ValueError, OSError):
            findings.append(
                Finding(
                    "skill.file", "error", path, None, "Expected a bounded regular Markdown file."
                )
            )
            continue
        sources[f".agenthicc/skills/{path}"] = contents
    return sources, findings


def _plan_findings(skill: SkillDef, plan: SkillPlan, source: str) -> list[Finding]:
    import yaml

    path = f"{plan.slug}/SKILL.md"
    findings: list[Finding] = []
    header, body = _split_frontmatter(source)
    raw = yaml.safe_load(header) if header is not None else {}
    if isinstance(raw, dict):
        for key in ("suggestedTopics", "suggested_topics", "topics"):
            topics = raw.get(key)
            if isinstance(topics, list) and any(
                isinstance(topic, str) and not topic.strip() for topic in topics
            ):
                findings.append(
                    Finding(
                        "skill.topics", "error", path, None, "Suggested topics must be nonempty."
                    )
                )
    if len(body.encode("utf-8")) > MAX_BODY_BYTES or not body.strip():
        findings.append(
            Finding(
                "skill.body",
                "error",
                path,
                None,
                "The skill body must contain up to 32 KiB of guidance.",
            )
        )
    expected = {
        "name": plan.name,
        "description": plan.description,
        "suggested topics": plan.when_to_use,
        "tools": plan.tools,
        "disabled tools": plan.disabled_tools,
        "aliases": plan.aliases,
        "argument hint": plan.argument_hints,
        "model": plan.model,
    }
    observed = {
        "name": skill.name,
        "description": skill.description,
        "suggested topics": tuple(skill.suggested_topics),
        "tools": tuple(skill.tools),
        "disabled tools": tuple(skill.disabled_tools),
        "aliases": skill.aliases,
        "argument hint": skill.argument_hint,
        "model": skill.model,
    }
    for field, value in expected.items():
        if observed[field] != value:
            findings.append(
                Finding(
                    "skill.plan_mismatch",
                    "error",
                    path,
                    None,
                    f"The {field} must match the accepted plan.",
                )
            )
    return findings


def validate_skill_set(
    root: Path, plans: tuple[SkillPlan, ...]
) -> tuple[dict[str, ValidationReport], dict[str, SkillDef], dict[str, tuple[str, ...]]]:
    """Scan the actual Markdown files and validate each whole skill."""
    reports: dict[str, ValidationReport] = {}
    accepted: dict[str, SkillDef] = {}
    inventories: dict[str, tuple[str, ...]] = {}
    unexpected = {entry.name for entry in root.iterdir()} - {plan.slug for plan in plans}
    for plan in plans:
        sources, findings = _read_skill_files(root, plan)
        main = f".agenthicc/skills/{plan.slug}/SKILL.md"
        if main in sources:
            with TemporaryDirectory() as directory:
                snapshot = Path(directory) / plan.slug
                snapshot.mkdir()
                for relative, contents in sources.items():
                    (snapshot / Path(relative).name).write_bytes(contents)
                validation = validate_skill_dir(snapshot, source="project")
            findings.extend(
                Finding(
                    f"skill.{item.code}",
                    "error" if item.severity == "error" else "warning",
                    f"{plan.slug}/SKILL.md",
                    None,
                    item.message.replace(str(snapshot), plan.slug),
                )
                for item in validation.diagnostics
                if item.severity != "info"
            )
            if validation.ok and validation.skill is not None:
                findings.extend(
                    _plan_findings(validation.skill, plan, sources[main].decode("utf-8"))
                )
                accepted[plan.slug] = replace(validation.skill, path=root / plan.slug)
        if unexpected:
            findings.append(
                Finding(
                    "skill.inventory",
                    "error",
                    plan.slug,
                    None,
                    "Staging contains an unplanned skill.",
                )
            )
        for relative, contents in sources.items():
            body = _extract_body(contents.decode("utf-8"))
            for code, pattern in _ADVISORIES.items():
                if pattern.search(body):
                    findings.append(
                        Finding(
                            code,
                            "warning",
                            relative,
                            None,
                            "Review instructions that could override the session's security policy.",
                        )
                    )
        reports[plan.slug] = ValidationReport(content_digest(sources), tuple(findings))
        inventories[plan.slug] = tuple(sorted(sources))
    return reports, accepted, inventories
