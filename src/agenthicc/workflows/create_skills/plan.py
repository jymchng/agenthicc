"""Typed plans admitted before skill generation starts."""

from dataclasses import asdict, dataclass
import re
from typing import TYPE_CHECKING, TypedDict

from agenthicc.skills.loader import canonical_skill_name

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_SKILLS = 12
MAX_SUPPORT_FILES = 5
MAX_FILE_BYTES = 64 * 1024
MAX_BODY_BYTES = 32 * 1024
MAX_DESCRIPTION = 300
MAX_TOPIC_WORDS = 12
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class SkillPlanInput(TypedDict):
    slug: str
    name: str
    description: str
    when_to_use: list[str]
    tools: list[str] | None
    disabled_tools: list[str] | None
    argument_hints: str | None
    aliases: list[str] | None
    support_files: list[str] | None
    model: str | None
    model_reason: str | None
    update: bool | None


@dataclass(frozen=True, slots=True)
class SkillPlan:
    slug: str
    name: str
    description: str
    when_to_use: tuple[str, ...]
    tools: tuple[str, ...]
    disabled_tools: tuple[str, ...]
    argument_hints: str
    aliases: tuple[str, ...]
    support_files: tuple[str, ...]
    model: str
    model_reason: str
    update: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SkillPlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise SkillPlanError("invalid_plan", f"{field} must be nonempty text.")
    return value.strip()


def _text_list(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise SkillPlanError("invalid_plan", f"{field} must be a list of text entries.")
    items = tuple(_text(item, field, required=True) for item in value)
    if len(set(items)) != len(items):
        raise SkillPlanError("invalid_plan", f"{field} contains duplicate entries.")
    return items


def _is_slug(value: str) -> bool:
    return _SLUG.fullmatch(value) is not None and canonical_skill_name(value) == value


def decode_skill_plan(raw: "Mapping[object, object]") -> SkillPlan:
    slug = _text(raw.get("slug"), "slug", required=True)
    if not _is_slug(slug):
        raise SkillPlanError("invalid_slug", f"{slug!r} is not a canonical skill slug.")
    description = _text(raw.get("description"), "description", required=True)
    if len(description) > MAX_DESCRIPTION:
        raise SkillPlanError(
            "invalid_description", f"Descriptions may use at most {MAX_DESCRIPTION} characters."
        )
    topics = _text_list(raw.get("when_to_use"), "when_to_use")
    if sum(len(topic.split()) for topic in topics) > MAX_TOPIC_WORDS:
        raise SkillPlanError(
            "invalid_topics", f"Use at most {MAX_TOPIC_WORDS} words across the topics."
        )
    aliases = _text_list(raw.get("aliases"), "aliases")
    if slug in aliases or any(not _is_slug(alias) for alias in aliases):
        raise SkillPlanError("invalid_alias", "Aliases must be distinct canonical slugs.")
    support = _text_list(raw.get("support_files"), "support_files")
    if len(support) > MAX_SUPPORT_FILES or any(
        name.casefold() == "skill.md" or re.fullmatch(r"[a-z0-9][a-z0-9-]*\.md", name) is None
        for name in support
    ):
        raise SkillPlanError("invalid_files", "Plan up to five flat Markdown support files.")
    model = _text(raw.get("model"), "model")
    model_reason = _text(raw.get("model_reason"), "model_reason")
    if model and not model_reason:
        raise SkillPlanError("model_reason_required", "Explain any model override.")
    update = raw.get("update")
    if update is not None and type(update) is not bool:
        raise SkillPlanError("invalid_plan", "update must be a boolean.")
    return SkillPlan(
        slug,
        _text(raw.get("name"), "name", required=True),
        description,
        topics,
        _text_list(raw.get("tools"), "tools"),
        _text_list(raw.get("disabled_tools"), "disabled_tools"),
        _text(raw.get("argument_hints"), "argument_hints"),
        aliases,
        support,
        model,
        model_reason,
        update is True,
    )


def plan_skills(
    raw: object,
    *,
    reserved: frozenset[str],
    owned: frozenset[str],
    allowed_tools: frozenset[str],
) -> tuple[SkillPlan, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SKILLS:
        raise SkillPlanError("skill_limit", f"Plan between one and {MAX_SKILLS} skills.")
    names = set(reserved)
    plans: list[SkillPlan] = []
    for version in raw:
        if not isinstance(version, dict):
            raise SkillPlanError("invalid_plan", "Each skill plan must be an object.")
        plan = decode_skill_plan(version)
        triggers = {plan.slug, *plan.aliases}
        if triggers & names or set(plan.aliases) & owned or (plan.slug in owned) != plan.update:
            raise SkillPlanError(
                "name_collision",
                f"A trigger for {plan.slug!r} is already in use or an update has no authored version.",
            )
        unavailable = (set(plan.tools) | set(plan.disabled_tools)) - allowed_tools
        if unavailable:
            raise SkillPlanError(
                "tool_not_available",
                f"Tools outside the caller's scope: {', '.join(sorted(unavailable))}.",
            )
        names.update(triggers)
        plans.append(plan)
    return tuple(plans)
