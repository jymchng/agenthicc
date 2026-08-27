"""Authoritative phase topology for the reconstruct-site workflow.

The historical reconstruct-site runner kept its enum, dispatch tables, phase
metadata, and re-entry map separately.  This module is deliberately free of
runner imports so it can be used by startup/UI code without importing browser
or provider integrations.  The compatibility runner consumes the plan for
dispatch, progress, model routing, profiles, and re-entry validation.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum

__all__ = [
    "PHASE_PLAN_VERSION",
    "PhasePlanError",
    "ReconstructProfile",
    "ReconstructPhaseDefinition",
    "ReconstructPhasePlan",
    "ActiveReconstructPlan",
    "RECONSTRUCT_PHASE_PLAN",
]

PHASE_PLAN_VERSION = "reconstruct-site.v2"


class PhasePlanError(ValueError):
    """Raised when a reconstruct phase graph or profile is invalid."""


class ReconstructProfile(StrEnum):
    """Supported reconstruction scopes."""

    STATIC = "static"
    APPLICATION = "application"
    PRODUCTION = "production"
    CUSTOM = "custom"


@dataclasses.dataclass(frozen=True, slots=True)
class ReconstructPhaseDefinition:
    """Immutable metadata for one executable phase."""

    name: str
    handler: str
    max_turns: int
    model_key: str
    agent_type: str = "auto"
    mode_override: str = "Yolo"
    required_capabilities: frozenset[str] = frozenset({"memory"})
    next_phase: str | None = None
    retry_phase: str | None = None
    allowed_reentry_targets: frozenset[str] = frozenset()
    artifact_kinds: tuple[str, ...] = ()
    profiles: frozenset[ReconstructProfile] = frozenset(
        {
            ReconstructProfile.STATIC,
            ReconstructProfile.APPLICATION,
            ReconstructProfile.PRODUCTION,
        }
    )
    reentry_allowed: bool = True
    dynamic: bool = False

    @property
    def state_name(self) -> str:
        """Return the canonical enum spelling used by the phase runner."""
        return self.name.upper()

    @property
    def state(self) -> str:
        """Return the plan-facing state identifier without importing the enum."""
        return self.state_name


@dataclasses.dataclass(frozen=True, slots=True)
class SkippedPhase:
    """A phase excluded from the active profile with an actionable reason."""

    name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"phase": self.name, "reason": self.reason}


@dataclasses.dataclass(frozen=True, slots=True)
class ActiveReconstructPlan:
    """Validated ordered phase graph for one profile."""

    profile: ReconstructProfile
    definitions: tuple[ReconstructPhaseDefinition, ...]
    skipped: tuple[SkippedPhase, ...]
    version: str = PHASE_PLAN_VERSION

    @property
    def total_phases(self) -> int:
        return len(self.definitions)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.definitions)

    @property
    def index(self) -> Mapping[str, int]:
        return {item.name: position for position, item in enumerate(self.definitions)}

    def definition(self, name: str) -> ReconstructPhaseDefinition:
        normalized = _normalise_name(name)
        for item in self.definitions:
            if item.name == normalized:
                return item
        raise PhasePlanError(f"unknown phase {name!r} for profile {self.profile.value!r}")

    def next_name(self, name: str) -> str | None:
        position = self.index.get(_normalise_name(name))
        if position is None or position + 1 >= len(self.definitions):
            return None
        return self.definitions[position + 1].name

    def resolve_reentry(self, target: str) -> ReconstructPhaseDefinition:
        """Validate an agent-selected re-entry target; never fall back."""
        definition = self.definition(target)
        if not definition.reentry_allowed:
            raise PhasePlanError(f"phase {definition.name!r} cannot be used as a re-entry target")
        if (
            definition.allowed_reentry_targets
            and definition.name not in definition.allowed_reentry_targets
        ):
            raise PhasePlanError(
                f"phase {definition.name!r} is not permitted by the active re-entry graph"
            )
        return definition

    def invalidated_kinds(self, target: str) -> frozenset[str]:
        """Return artifact kinds owned by *target* and downstream phases."""
        position = self.index.get(_normalise_name(target))
        if position is None:
            raise PhasePlanError(f"unknown re-entry target {target!r}")
        kinds: set[str] = set()
        for item in self.definitions[position:]:
            kinds.update(item.artifact_kinds)
        return frozenset(kinds)


class ReconstructPhasePlan:
    """Validated immutable source from which active profile plans are built."""

    def __init__(self, definitions: Sequence[ReconstructPhaseDefinition]) -> None:
        self._definitions = tuple(definitions)
        self.validate()

    @property
    def definitions(self) -> tuple[ReconstructPhaseDefinition, ...]:
        return self._definitions

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self._definitions)

    def validate(self, runner_type: type[object] | None = None) -> None:
        names: set[str] = set()
        for item in self._definitions:
            if not item.name or item.name != _normalise_name(item.name):
                raise PhasePlanError(f"invalid phase name {item.name!r}")
            if item.name in names:
                raise PhasePlanError(f"duplicate phase {item.name!r}")
            names.add(item.name)
            if not item.handler.startswith("_"):
                raise PhasePlanError(f"phase {item.name!r} handler must be private")
            if item.max_turns < 1:
                raise PhasePlanError(f"phase {item.name!r} max_turns must be positive")
            if not item.profiles:
                raise PhasePlanError(f"phase {item.name!r} has no profile")
            if not item.agent_type.strip():
                raise PhasePlanError(f"phase {item.name!r} has no agent type")
            if not item.mode_override.strip():
                raise PhasePlanError(f"phase {item.name!r} has no mode override")
            if not item.required_capabilities:
                raise PhasePlanError(f"phase {item.name!r} has no capability declaration")
            if runner_type is not None and not callable(getattr(runner_type, item.handler, None)):
                raise PhasePlanError(f"phase {item.name!r} handler {item.handler!r} is missing")
        if not self._definitions:
            raise PhasePlanError("reconstruct phase plan must not be empty")
        if self._definitions[0].name != "init":
            raise PhasePlanError("reconstruct phase plan must start with init")
        if self._definitions[-1].name != "final_validation":
            raise PhasePlanError("reconstruct phase plan must end with final_validation")
        for item in self._definitions:
            if item.next_phase is not None and _normalise_name(item.next_phase) not in names:
                raise PhasePlanError(
                    f"phase {item.name!r} has an unknown next phase {item.next_phase!r}"
                )
            if item.retry_phase is not None and _normalise_name(item.retry_phase) not in names:
                raise PhasePlanError(
                    f"phase {item.name!r} has an unknown retry phase {item.retry_phase!r}"
                )
            if item.allowed_reentry_targets and not set(item.allowed_reentry_targets).issubset(
                names
            ):
                raise PhasePlanError(f"phase {item.name!r} has an unknown re-entry target")

    def active(
        self,
        profile: ReconstructProfile | str,
        custom_phases: Iterable[str] = (),
    ) -> ActiveReconstructPlan:
        selected_profile = _coerce_profile(profile)
        if selected_profile is ReconstructProfile.CUSTOM:
            selected = tuple(_normalise_name(name) for name in custom_phases if str(name).strip())
            if not selected:
                raise PhasePlanError("custom profile requires at least one phase")
            unknown = sorted(set(selected).difference(self.names))
            if unknown:
                raise PhasePlanError(
                    f"custom profile contains unknown phases: {', '.join(unknown)}"
                )
            if selected[0] != "init" or selected[-1] != "final_validation":
                raise PhasePlanError(
                    "custom profile must include init and final_validation at its boundaries"
                )
            if len(set(selected)) != len(selected):
                raise PhasePlanError("custom profile contains duplicate phases")
            canonical_selected = tuple(
                item.name for item in self._definitions if item.name in selected
            )
            if selected != canonical_selected:
                raise PhasePlanError(
                    "custom profile phases must follow the authoritative plan order"
                )
            selected_names = set(selected)
        else:
            selected_names = {
                item.name for item in self._definitions if selected_profile in item.profiles
            }

        active = tuple(item for item in self._definitions if item.name in selected_names)
        if not active or active[0].name != "init" or active[-1].name != "final_validation":
            raise PhasePlanError(
                f"profile {selected_profile.value!r} does not form a complete graph"
            )
        skipped = tuple(
            SkippedPhase(
                item.name,
                _skip_reason(item.name, selected_profile),
            )
            for item in self._definitions
            if item.name not in selected_names
        )
        active_names = tuple(item.name for item in active)
        allowed_reentry = frozenset(item.name for item in active if item.reentry_allowed)
        active = tuple(
            dataclasses.replace(
                item,
                next_phase=(active_names[index + 1] if index + 1 < len(active_names) else None),
                retry_phase=item.name,
                allowed_reentry_targets=allowed_reentry,
            )
            for index, item in enumerate(active)
        )
        return ActiveReconstructPlan(selected_profile, active, skipped)

    def validate_phase_specs(self, specs: Iterable[object]) -> None:
        """Check registry metadata against this plan at import time.

        Phase prompts remain owned by ``PhaseSpec`` for backward compatibility,
        but their executable names, order, turn budgets, mode, and canonical
        edges must not drift from the plan that drives dispatch, progress, and
        resume.
        """
        materialized = tuple(specs)
        names = tuple(str(getattr(spec, "name", "")).strip().lower() for spec in materialized)
        if names != self.names:
            raise PhasePlanError(
                "reconstruct PhaseSpec registry does not match the authoritative plan: "
                f"expected {self.names!r}, got {names!r}"
            )
        for index, (spec, definition) in enumerate(zip(materialized, self._definitions)):
            if getattr(spec, "max_turns", None) != definition.max_turns:
                raise PhasePlanError(
                    f"phase {definition.name!r} max_turns differs between plan and registry"
                )
            if getattr(spec, "mode_override", None) != definition.mode_override:
                raise PhasePlanError(
                    f"phase {definition.name!r} mode differs between plan and registry"
                )
            expected_next = self.names[index + 1] if index + 1 < len(self.names) else None
            if getattr(spec, "next", None) != expected_next:
                raise PhasePlanError(
                    f"phase {definition.name!r} next edge differs between plan and registry"
                )
            if getattr(spec, "on_reject", None) != definition.name:
                raise PhasePlanError(
                    f"phase {definition.name!r} retry edge differs between plan and registry"
                )


def _normalise_name(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


def _coerce_profile(value: ReconstructProfile | str) -> ReconstructProfile:
    try:
        return (
            value
            if isinstance(value, ReconstructProfile)
            else ReconstructProfile(_normalise_name(value))
        )
    except ValueError as exc:
        raise PhasePlanError(f"unknown reconstruct profile {value!r}") from exc


def _skip_reason(name: str, profile: ReconstructProfile) -> str:
    if profile is ReconstructProfile.STATIC:
        return f"{name} is outside the static reconstruction scope"
    if profile is ReconstructProfile.APPLICATION:
        return f"{name} requires production infrastructure scope"
    return f"{name} was not selected by the custom phase graph"


def _phase(
    name: str,
    max_turns: int,
    *,
    model_key: str | None = None,
    agent_type: str = "auto",
    mode_override: str = "Yolo",
    capabilities: Iterable[str] | None = None,
    artifacts: Iterable[str] = (),
    profiles: Iterable[ReconstructProfile] | None = None,
    reentry_allowed: bool = True,
    dynamic: bool = False,
) -> ReconstructPhaseDefinition:
    return ReconstructPhaseDefinition(
        name=name,
        handler=f"_{name}",
        max_turns=max_turns,
        model_key=model_key or name,
        agent_type=agent_type,
        mode_override=mode_override,
        required_capabilities=frozenset(
            str(item)
            for item in (
                capabilities
                if capabilities is not None
                else _CAPABILITIES_BY_PHASE.get(name, ("memory", "read"))
            )
        ),
        artifact_kinds=tuple(artifacts),
        profiles=frozenset(
            profiles
            or {
                ReconstructProfile.STATIC,
                ReconstructProfile.APPLICATION,
                ReconstructProfile.PRODUCTION,
            }
        ),
        reentry_allowed=reentry_allowed,
        dynamic=dynamic,
    )


_INFRA = (ReconstructProfile.PRODUCTION,)
_APPLICATION = (ReconstructProfile.APPLICATION, ReconstructProfile.PRODUCTION)
_CAPABILITIES_BY_PHASE: dict[str, tuple[str, ...]] = {
    "init": ("memory", "read", "network"),
    "recon": ("memory", "read", "network"),
    "visual_research": ("memory", "read", "network", "screenshot"),
    "interaction_analysis": ("memory", "read", "network"),
    "content_assets": ("memory", "read", "network"),
    "architecture": ("memory", "read"),
    "design_system": ("memory", "read"),
    "bootstrap": ("memory", "read", "write", "execute"),
    "global_shell": ("memory", "read", "write", "execute"),
    "component_system": ("memory", "read", "write", "execute"),
    "page": ("memory", "read", "write", "execute", "network"),
    "data_layer": ("memory", "read", "write", "execute", "network"),
    "responsive_pass": ("memory", "read", "write", "execute"),
    "visual_validation": ("memory", "read", "network", "screenshot"),
    "interaction_validation": ("memory", "read", "write", "execute", "network"),
    "accessibility": ("memory", "read", "execute"),
    "performance": ("memory", "read", "execute", "network"),
    "fidelity_pass": ("memory", "read", "write", "execute", "network", "screenshot"),
    "final_validation": ("memory", "read", "execute", "network", "screenshot"),
}

RECONSTRUCT_PHASE_PLAN = ReconstructPhasePlan(
    (
        _phase("init", 10, artifacts=("initial_state",)),
        _phase("recon", 35, artifacts=("route_inventory",)),
        _phase("visual_research", 30, artifacts=("visual_spec", "screenshot")),
        _phase("interaction_analysis", 30, artifacts=("interaction_inventory",)),
        _phase("content_assets", 25, artifacts=("asset_inventory",)),
        _phase("architecture", 25, artifacts=("architecture",)),
        _phase("design_system", 25, artifacts=("design_system",)),
        _phase("bootstrap", 25, artifacts=("bootstrap",)),
        _phase("global_shell", 30, artifacts=("global_shell",)),
        _phase("component_system", 30, artifacts=("component_system",)),
        _phase("page", 40, artifacts=("page",), dynamic=True),
        _phase("data_layer", 30, artifacts=("data_layer",), profiles=_APPLICATION),
        _phase("responsive_pass", 30, artifacts=("responsive",)),
        _phase("visual_validation", 35, artifacts=("visual_validation", "screenshot")),
        _phase("interaction_validation", 35, artifacts=("interaction_validation",)),
        _phase("accessibility", 30, artifacts=("accessibility",)),
        _phase("performance", 30, artifacts=("performance",)),
        _phase("fidelity_pass", 30, artifacts=("fidelity_pass",)),
        _phase("sqlite_db", 35, artifacts=("sqlite_db",), profiles=_INFRA),
        _phase("verify_sqlite", 30, artifacts=("verify_sqlite",), profiles=_INFRA),
        _phase("prisma", 35, artifacts=("prisma",), profiles=_INFRA),
        _phase("verify_prisma", 30, artifacts=("verify_prisma",), profiles=_INFRA),
        _phase("tanstack_query", 35, artifacts=("tanstack_query",), profiles=_INFRA),
        _phase("verify_tanstack", 30, artifacts=("verify_tanstack",), profiles=_INFRA),
        _phase("env_config", 30, artifacts=("env_config",), profiles=_INFRA),
        _phase("verify_env", 30, artifacts=("verify_env",), profiles=_INFRA),
        _phase("docker", 35, artifacts=("docker",), profiles=_INFRA),
        _phase("verify_docker", 30, artifacts=("verify_docker",), profiles=_INFRA),
        _phase("netlify", 30, artifacts=("netlify",), profiles=_INFRA),
        _phase("verify_netlify", 30, artifacts=("verify_netlify",), profiles=_INFRA),
        _phase("caddy", 30, artifacts=("caddy",), profiles=_INFRA),
        _phase("verify_caddy", 30, artifacts=("verify_caddy",), profiles=_INFRA),
        _phase("package_commands", 30, artifacts=("package_commands",), profiles=_INFRA),
        _phase("verify_package", 30, artifacts=("verify_package",), profiles=_INFRA),
        _phase("scripts", 35, artifacts=("scripts",), profiles=_INFRA),
        _phase("verify_scripts", 30, artifacts=("verify_scripts",), profiles=_INFRA),
        _phase("docs", 40, artifacts=("docs",), profiles=_INFRA),
        _phase("verify_docs", 30, artifacts=("verify_docs",), profiles=_INFRA),
        _phase("final_validation", 35, artifacts=("final_validation",)),
    )
)
