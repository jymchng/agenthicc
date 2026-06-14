"""CommandModals — Textual ModalScreen replacements for SlashCommandHandler (PRD-55 Phase 5).

Each class is a :class:`~textual.screen.ModalScreen` that the user dismisses with
Escape or the ``q`` key.  All modals share common CSS: 80% wide, 80% tall,
``$surface`` background, thick ``$primary`` border.

Usage::

    from agenthicc.tui.widgets.command_modals import AgentStatusModal
    app.push_screen(AgentStatusModal(model))

"""
from __future__ import annotations

import os
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, RichLog, Static

from agenthicc.tui.transcript import TranscriptModel

__all__ = [
    "AgentStatusModal",
    "HelpModal",
    "HistoryModal",
    "ModelsModal",
    "SkillsModal",
]

# ── shared CSS ───────────────────────────────────────────────────────────────

_MODAL_CSS = """
AgentStatusModal, HistoryModal, ModelsModal, HelpModal, SkillsModal {
    align: center middle;
}

AgentStatusModal > .modal-body,
HistoryModal > .modal-body,
ModelsModal > .modal-body,
HelpModal > .modal-body,
SkillsModal > .modal-body {
    width: 80%;
    height: 80%;
    background: $surface;
    border: thick $primary;
    padding: 0 1;
}

AgentStatusModal > .modal-body > Static.modal-title,
HistoryModal > .modal-body > Static.modal-title,
ModelsModal > .modal-body > Static.modal-title,
HelpModal > .modal-body > Static.modal-title,
SkillsModal > .modal-body > Static.modal-title {
    width: 100%;
    background: $primary;
    color: $text;
    padding: 0 1;
}

AgentStatusModal > .modal-body > Static.modal-footer,
HistoryModal > .modal-body > Static.modal-footer,
ModelsModal > .modal-body > Static.modal-footer,
HelpModal > .modal-body > Static.modal-footer,
SkillsModal > .modal-body > Static.modal-footer {
    width: 100%;
    dock: bottom;
}
"""


# ── AgentStatusModal ──────────────────────────────────────────────────────────


class AgentStatusModal(ModalScreen):
    """DataTable showing agent turns: agent_id[:8], name, cost, tokens.

    Source: :class:`~agenthicc.tui.transcript.TranscriptModel` passed via
    ``__init__``.

    Keyboard:
        Escape  — dismiss
        q       — dismiss
    """

    CSS = _MODAL_CSS

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def __init__(self, model: TranscriptModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model = model

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical  # noqa: PLC0415
        with Vertical(classes="modal-body"):
            yield Static("[bold]Agent Status[/bold]", classes="modal-title")
            table: DataTable[str] = DataTable()
            table.add_columns("Agent ID", "Name", "Cost", "Tokens")
            for turn in self._model.turns:
                table.add_row(
                    turn.agent_id[:8],
                    turn.agent_name,
                    f"${turn.cost_usd:.4f}" if turn.cost_usd is not None else "$0.0000",
                    str(turn.tokens) if turn.tokens is not None else "0",
                )
            if not self._model.turns:
                table.add_row("—", "(no active agents)", "", "")
            yield table
            yield Static("[dim]press Escape to close[/dim]", classes="modal-footer")

    def action_dismiss(self) -> None:
        self.dismiss()


# ── HistoryModal ──────────────────────────────────────────────────────────────


class HistoryModal(ModalScreen):
    """RichLog showing the last 20 rendered transcript lines.

    Source: :class:`~agenthicc.tui.transcript.TranscriptModel` passed via
    ``__init__``.

    Keyboard:
        Escape  — dismiss
        q       — dismiss
    """

    CSS = _MODAL_CSS

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def __init__(self, model: TranscriptModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model = model

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical  # noqa: PLC0415
        lines = self._model.render()[-20:]
        with Vertical(classes="modal-body"):
            yield Static("[bold]History — last 20 lines[/bold]", classes="modal-title")
            log = RichLog(markup=True, wrap=True)
            yield log
            yield Static("[dim]press Escape to close[/dim]", classes="modal-footer")

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        lines = self._model.render()[-20:]
        if lines:
            for line in lines:
                log.write(line)
        else:
            log.write("[dim](empty)[/dim]")

    def action_dismiss(self) -> None:
        self.dismiss()


# ── ModelsModal ───────────────────────────────────────────────────────────────


class ModelsModal(ModalScreen):
    """DataTable with columns: Provider, Default Model, API Key Env, Status.

    Shows the current active provider with a ``◀`` marker.

    Source: imported from :mod:`agenthicc.config`.

    Keyboard:
        Escape  — dismiss
        q       — dismiss
    """

    CSS = _MODAL_CSS

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical  # noqa: PLC0415
        from agenthicc.config import (  # noqa: PLC0415
            PROVIDER_API_KEY_ENVVAR,
            PROVIDER_DEFAULT_MODELS,
            SUPPORTED_PROVIDERS,
            load_config,
        )

        cfg = load_config()
        current_provider = cfg.execution.provider

        table: DataTable[str] = DataTable()
        table.add_columns("Provider", "Default Model", "API Key Env", "Status")

        for provider in SUPPORTED_PROVIDERS:
            env_var = PROVIDER_API_KEY_ENVVAR.get(provider, "—")
            key_set = (
                "✓ set"
                if (provider == "ollama" or os.environ.get(env_var))
                else "✗ not set"
            )
            active_marker = " ◀" if provider == current_provider else ""
            table.add_row(
                provider + active_marker,
                PROVIDER_DEFAULT_MODELS.get(provider, "—"),
                env_var,
                key_set,
            )

        with Vertical(classes="modal-body"):
            yield Static("[bold]LLM Providers[/bold]", classes="modal-title")
            yield table
            yield Static("[dim]press Escape to close[/dim]", classes="modal-footer")

    def action_dismiss(self) -> None:
        self.dismiss()


# ── HelpModal ─────────────────────────────────────────────────────────────────


class HelpModal(ModalScreen):
    """DataTable with columns: Command, Arguments, Description.

    Each command group is rendered as a separate section (one DataTable per
    group).

    Source: :class:`~agenthicc.commands.UnifiedCommandRegistry` passed via
    ``__init__``, or ``build_builtin_registry()`` if not provided.

    Keyboard:
        Escape  — dismiss
        q       — dismiss
    """

    CSS = _MODAL_CSS

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def __init__(self, registry: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._registry = registry

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical, VerticalScroll  # noqa: PLC0415

        registry = self._registry
        if registry is None:
            from agenthicc.commands import build_builtin_registry  # noqa: PLC0415
            registry = build_builtin_registry()

        with Vertical(classes="modal-body"):
            yield Static("[bold]Help — Slash Commands[/bold]", classes="modal-title")
            with VerticalScroll():
                for group in registry.groups():
                    cmds = registry.commands_for_group(group)
                    if not cmds:
                        continue
                    yield Static(f"[bold cyan]{group}[/bold cyan]")
                    table: DataTable[str] = DataTable()
                    table.add_columns("Command", "Arguments", "Description")
                    for cmd in cmds:
                        table.add_row(
                            cmd.name,
                            cmd.argument_hint or "",
                            cmd.description,
                        )
                    yield table
            yield Static("[dim]press Escape to close[/dim]", classes="modal-footer")

    def action_dismiss(self) -> None:
        self.dismiss()


# ── SkillsModal ───────────────────────────────────────────────────────────────


class SkillsModal(ModalScreen):
    """DataTable with columns: Command, Name, Description.

    Source: skills dict (``{slug: skill_object}``) passed via ``__init__``.

    Keyboard:
        Escape  — dismiss
        q       — dismiss
    """

    CSS = _MODAL_CSS

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def __init__(self, skills: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._skills = skills or {}

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical  # noqa: PLC0415

        table: DataTable[str] = DataTable()
        table.add_columns("Command", "Name", "Description")

        if not self._skills:
            table.add_row("—", "(no skills found)", "")
        else:
            for slug, skill in sorted(self._skills.items()):
                desc = getattr(skill, "description", "") or "—"
                table.add_row(
                    f"/{slug}",
                    getattr(skill, "name", slug),
                    desc[:80],
                )

        with Vertical(classes="modal-body"):
            yield Static("[bold]Available Skills[/bold]", classes="modal-title")
            yield table
            yield Static("[dim]press Escape to close[/dim]", classes="modal-footer")

    def action_dismiss(self) -> None:
        self.dismiss()
