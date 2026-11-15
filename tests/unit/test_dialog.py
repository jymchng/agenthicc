"""TUX-07: one Dialog for approvals, plans and questions (acceptance criteria 1–9)."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import astuple, dataclass, field
from pathlib import Path

import pytest
from rich.console import Console, Group, RenderableType

from agenthicc.tools.approval import (
    ApprovalRequest,
    ApprovalRequester,
    ApprovalResponse,
    InteractiveApprovals,
    approval_reason,
    denial_payload,
)
from agenthicc.tools.workspace_access import WorkspaceAccessRequest, WorkspaceScope
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.theme import ASCII_GLYPHS, BUILTIN_THEMES, UNICODE_GLYPHS, Glyphs, use_theme
from agenthicc.tui.workspace.dialog import Dialog, DialogSettings, dialog_width
from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
from agenthicc.tui.workspace.overlays.approval_body import request_announcement
from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BOX = set("╭╮╰╯│─")


@dataclass
class RecordingApprovals:
    """An :class:`InteractiveApprovals` fake that records every response."""

    responses: list[dict[str, object]] = field(default_factory=list)

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(allowed=False)

    def reset_turn_memory(self) -> None:
        return None

    def respond(
        self,
        allowed: bool,
        *,
        remember: bool = False,
        remember_all: bool = False,
        message: str = "",
        mode: str | None = None,
        scope_grant: str | None = None,
        request_id: str | None = None,
        outcome: str | None = None,
    ) -> bool:
        self.responses.append(
            {
                "allowed": allowed,
                "remember": remember,
                "remember_all": remember_all,
                "message": message,
                "mode": mode,
                "scope_grant": scope_grant,
            }
        )
        return True

    def respond_for_request(
        self,
        request_id: str,
        allowed: bool,
        *,
        remember: bool = False,
        remember_all: bool = False,
        message: str = "",
        mode: str | None = None,
        scope_grant: str | None = None,
        outcome: str | None = None,
    ) -> bool:
        return self.respond(allowed, message=message, outcome=outcome)


def _terminal(monkeypatch: pytest.MonkeyPatch, cols: int, rows: int = 40) -> None:
    monkeypatch.setattr(
        "agenthicc.tui.workspace.dialog.shutil.get_terminal_size",
        lambda _fallback=(80, 24): os.terminal_size((cols, rows)),
    )


def _text(overlay: object, cols: int = 80) -> str:
    console = Console(record=True, width=cols, color_system=None, highlight=False)
    console.print(overlay.render())  # type: ignore[attr-defined]
    return console.export_text()


def _request(
    tool: str = "run_bash",
    args: dict[str, object] | None = None,
    *,
    capabilities: frozenset[str] = frozenset({"execute"}),
    kind: str = "tool",
    reason: str = "Safe mode asks before running commands.",
    **extra: object,
) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name=tool,
        tool_use_id="call-1",
        tool_input=args if args is not None else {"command": "ls"},
        capabilities=capabilities,
        event=asyncio.Event(),
        kind=kind,
        reason=reason,
        **extra,  # type: ignore[arg-type]
    )


def _approval(
    req: ApprovalRequest, settings: DialogSettings | None = None
) -> tuple[ApprovalOverlay, RecordingApprovals, list[bool]]:
    service = RecordingApprovals()
    closed: list[bool] = []
    overlay = ApprovalOverlay(
        req, service, lambda: closed.append(True), settings=settings or DialogSettings(mode="Safe")
    )
    overlay.on_mount()
    return overlay, service, closed


# ── 1. no more fixed widths; every decision overlay is a Dialog ─────────────


def test_no_fixed_66_column_frames_remain() -> None:
    found = subprocess.run(
        ["grep", "-rnE", r"min\(cols, 66\)", str(_ROOT / "src")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert found.stdout == ""


@pytest.mark.parametrize(("cols", "width"), [(50, 50), (64, 60), (80, 76), (124, 120), (200, 120)])
def test_width_is_clamped_to_the_terminal(cols: int, width: int) -> None:
    assert dialog_width(cols) == width


def test_every_decision_overlay_renders_through_dialog() -> None:
    service = RecordingApprovals()
    plan = _request("Plan Review", {"plan": "do it"}, capabilities=frozenset(), kind="plan_review")
    questions = _request(
        "ask_user",
        {"questions": [{"id": "a", "text": "Why?", "options": ["x"]}]},
        capabilities=frozenset(),
        kind="questions",
    )
    for overlay in (
        ApprovalOverlay(_request(), service, lambda: None),
        PlanApprovalOverlay(plan, service, lambda: None),
        QuestionsOverlay(questions, service, lambda: None),
    ):
        assert isinstance(overlay.dialog, Dialog)
        assert isinstance(overlay.render(), Group)


def test_wide_terminals_centre_the_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    _terminal(monkeypatch, 200)
    overlay, _service, _closed = _approval(_request())
    top = _text(overlay, 200).splitlines()[0]
    assert top.startswith(" " * 40 + "╭")
    assert len(top.rstrip()) == 40 + 120


# ── 2. commands are shown whole ─────────────────────────────────────────────


@pytest.mark.parametrize("cols", [80, 120])
def test_a_300_character_command_is_shown_in_full(
    monkeypatch: pytest.MonkeyPatch, cols: int
) -> None:
    _terminal(monkeypatch, cols)
    words = [f"step{i:03d}" for i in range(40)]
    command = " && ".join(f"echo {word}" for word in words)[:300] + " && rm -rf build-tail"
    assert len(command) > 300
    overlay, _service, _closed = _approval(_request(args={"command": command}))
    text = _text(overlay, cols)
    flat = re.sub(r"[\s│]", "", text)
    assert re.sub(r"\s", "", command) in flat
    assert re.sub(r"\s", "", command[-20:]) in flat
    assert all(len(line) <= cols for line in text.splitlines())


def test_a_cd_prefix_is_shown_as_the_directory() -> None:
    overlay, _service, _closed = _approval(
        _request(args={"command": "cd objective-create-a && npm ci && npm test"})
    )
    text = _text(overlay)
    assert "in objective-create-a" in text
    assert "$ npm ci && npm test" in text
    assert "Run a command" in text and "run_bash" not in text


# ── 3. diffs ────────────────────────────────────────────────────────────────


def test_patch_file_shows_a_unified_diff_of_the_real_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _terminal(monkeypatch, 100)
    target = tmp_path / "app.py"
    target.write_text("def main():\n    return 1\n\nprint(main())\n", encoding="utf-8")
    req = _request(
        "patch_file",
        {"path": "app.py", "old_content": "return 1", "new_content": "return 2"},
        capabilities=frozenset({"write"}),
        reason="Safe mode asks before changing files.",
    )
    opened: list[tuple[str, str]] = []

    def open_details(title: str, renderables: Sequence[RenderableType]) -> None:
        console = Console(record=True, width=100, color_system=None, highlight=False)
        for renderable in renderables:
            console.print(renderable)
        opened.append((title, console.export_text()))

    settings = DialogSettings(
        mode="Safe", workspace=WorkspaceScope.create(tmp_path), open_details=open_details
    )
    overlay, _service, _closed = _approval(req, settings)
    text = _text(overlay, 100)
    assert "Edit a file" in text
    assert "app.py  +1 -1" in text
    assert "return 1" in text and "return 2" in text
    # TUX-09: Ctrl+T hands the full diff to the transcript view; the dialog itself
    # does not change, so Esc in the view returns to exactly this.
    overlay.handle_key(Key.CTRL_T, "")
    assert [title for title, _text_ in opened] == ["Edit a file"]
    details = opened[0][1]
    assert "--- a/app.py" in details and "+++ b/app.py" in details
    assert "-    return 1" in details and "+    return 2" in details
    assert _text(overlay, 100) == text


def test_a_new_file_is_marked_and_bounded(tmp_path: Path) -> None:
    content = "\n".join(f"line {i}" for i in range(100))
    req = _request(
        "write_file", {"path": "new.txt", "content": content}, capabilities=frozenset({"write"})
    )
    overlay, _service, _closed = _approval(
        req, DialogSettings(workspace=WorkspaceScope.create(tmp_path))
    )
    text = _text(overlay, 100)
    assert "Create a file" in text
    assert "new file" in text and "+100 -0" in text


def test_delete_shows_the_size_and_network_the_rule(tmp_path: Path) -> None:
    (tmp_path / "old.log").write_text("x" * 2048, encoding="utf-8")
    req = _request("delete_file", {"path": "old.log"}, capabilities=frozenset({"write"}))
    overlay, _service, _closed = _approval(
        req, DialogSettings(workspace=WorkspaceScope.create(tmp_path))
    )
    text = _text(overlay)
    assert "Delete a file" in text and "old.log  2.0 KB" in text

    req = _request(
        "web_fetch",
        {"url": "https://example.com/docs/page"},
        capabilities=frozenset({"network"}),
    )
    overlay, _service, _closed = _approval(req)
    text = _text(overlay)
    assert "GET example.com/docs/page" in text
    assert "network_allow_list" in text


def test_scope_approvals_never_show_raw_arguments(tmp_path: Path) -> None:
    access = WorkspaceAccessRequest(
        requested="/etc/secret",
        canonical=Path("/etc/secret"),
        display="/etc/secret",
        operation="write",
        tool_name="write_file",
        workspace_root=tmp_path,
    )
    req = _request(
        "write_file",
        {"path": "/etc/secret", "content": "TOKEN=abc123"},
        capabilities=frozenset({"write"}),
        workspace_access=(access,),
    )
    overlay, service, _closed = _approval(req)
    text = _text(overlay)
    assert "write: /etc/secret" in text and "TOKEN" not in text
    overlay.handle_key(Key.CHAR, "3")
    assert service.responses[-1]["scope_grant"] == "target_session"


# ── 4. the reason ───────────────────────────────────────────────────────────


def test_every_approval_names_its_reason() -> None:
    assert approval_reason("Safe", frozenset({"execute"})) == (
        "Safe mode asks before running commands."
    )
    assert approval_reason("Safe", frozenset({"write", "network"})) == (
        "Safe mode asks before changing files and using the network."
    )
    overlay, _service, _closed = _approval(_request())
    assert "Safe mode asks before running commands." in _text(overlay)


# ── 5. number keys and aliases ──────────────────────────────────────────────


def test_two_allows_for_the_turn_and_esc_denies() -> None:
    overlay, service, closed = _approval(_request())
    assert isinstance(service, InteractiveApprovals)
    overlay.handle_key(Key.CHAR, "2")
    assert service.responses[-1]["allowed"] is True
    assert service.responses[-1]["remember"] is True
    assert closed == [True]

    overlay, service, closed = _approval(_request())
    overlay.handle_key(Key.ESC, "")
    assert service.responses[-1]["allowed"] is False
    assert closed == [True]


@pytest.mark.parametrize(
    ("key", "allowed", "remember", "remember_all"),
    [
        ("y", True, False, False),
        ("a", True, True, False),
        ("A", True, False, True),
        ("n", False, False, False),
    ],
)
def test_letter_aliases_still_work(
    key: str, allowed: bool, remember: bool, remember_all: bool
) -> None:
    overlay, service, _closed = _approval(_request())
    overlay.handle_key(Key.CHAR, key)
    response = service.responses[-1]
    assert (response["allowed"], response["remember"], response["remember_all"]) == (
        allowed,
        remember,
        remember_all,
    )


def test_the_options_name_the_grant() -> None:
    overlay, _service, _closed = _approval(_request())
    labels = [option.label for option in overlay.dialog.options]
    assert labels == [
        "Yes",
        "Yes, and allow commands for the rest of this turn",
        "Yes, and allow commands this session",
        "No, and tell the agent what to do instead",
    ]


# ── 6. "No, and tell the agent" (unit half; the integration test runs the gate) ──


def test_no_and_tell_the_agent_opens_the_embedded_composer() -> None:
    from agenthicc.tui.input.embedded import EmbeddedComposer

    overlay, service, closed = _approval(_request())
    overlay.handle_key(Key.CHAR, "4")
    assert isinstance(overlay._composer, EmbeddedComposer)
    assert "Tell the agent what to do instead" in _text(overlay)
    for ch in "use pnpm instead":
        overlay.handle_key(Key.CHAR, ch)
    overlay.handle_key(Key.ENTER, "")
    assert service.responses[-1]["allowed"] is False
    assert service.responses[-1]["message"] == "use pnpm instead"
    assert closed == [True]


@pytest.mark.asyncio
async def test_an_in_dialog_composer_repaints_after_each_key() -> None:
    """In a running loop the composer dispatches keys in a task, after the host repainted."""
    repaints: list[str] = []
    service = RecordingApprovals()
    settings = DialogSettings(mode="Safe", redraw=lambda: repaints.append("redraw"))
    plan = _request(
        "Plan Review", {"plan": "1. a"}, capabilities=frozenset(), kind="plan_review", reason=""
    )
    for overlay, choose in (
        (ApprovalOverlay(_request(), service, lambda: None, settings=settings), "4"),
        (PlanApprovalOverlay(plan, service, lambda: None, settings=settings), "3"),
    ):
        overlay.on_mount()
        overlay.handle_key(Key.CHAR, choose)
        repaints.clear()
        overlay.handle_key(Key.CHAR, "x")
        composer = overlay._composer
        assert composer is not None
        await composer.settle()
        assert repaints, type(overlay).__name__
        assert composer.text == "x"
        overlay.on_unmount()
    assert service.responses == []


def test_a_denial_payload_carries_the_instructions() -> None:
    payload = denial_payload("run_bash", "use pnpm instead")
    assert payload["error_code"] == "approval_denied"
    assert "use pnpm instead" in str(payload["error"])
    assert denial_payload("run_bash", "")["error"] == "User denied permission to run 'run_bash'."


# ── 7. plan review height (more in test_plan_approval_overlay.py) ───────────


def test_short_body_shrinks_the_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    _terminal(monkeypatch, 80, 60)
    overlay, _service, _closed = _approval(_request())
    rendered = overlay.render()
    assert isinstance(rendered, Group)
    assert len(rendered.renderables) <= 13


# ── 8. no emoji: only TUX-01 glyphs ─────────────────────────────────────────


def _glyph_chars(glyph_set: Glyphs) -> set[str]:
    chars: set[str] = set()
    for value in astuple(glyph_set):
        for part in value if isinstance(value, tuple) else (value,):
            chars.update(part)
    return chars


@pytest.mark.parametrize("glyph_set", [UNICODE_GLYPHS, ASCII_GLYPHS])
def test_dialogs_draw_only_theme_glyphs(tmp_path: Path, glyph_set: Glyphs) -> None:
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(glyph_set))
    try:
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        scope = WorkspaceScope.create(tmp_path)
        service = RecordingApprovals()
        overlays: list[object] = [
            ApprovalOverlay(_request(), service, lambda: None),
            ApprovalOverlay(
                _request(
                    "patch_file",
                    {"path": "a.py", "old_content": "x = 1", "new_content": "x = 2"},
                    capabilities=frozenset({"write"}),
                ),
                service,
                lambda: None,
                settings=DialogSettings(workspace=scope),
            ),
            PlanApprovalOverlay(
                _request(
                    "Plan Review",
                    {"plan": "1. a\n2. b"},
                    kind="plan_review",
                    mode_options=("Safe", "Yolo"),
                ),
                service,
                lambda: None,
            ),
            QuestionsOverlay(
                _request(
                    "ask_user",
                    {
                        "questions": [
                            {"id": "a", "text": "Which?", "options": ["x", "y"]},
                            {"id": "b", "text": "Why?", "options": ["z"]},
                        ]
                    },
                    kind="questions",
                    timeout_s=240.0,
                ),
                service,
                lambda: None,
            ),
        ]
        allowed = _glyph_chars(glyph_set)
        for overlay in overlays:
            text = _text(overlay, 100)
            stray = {ch for ch in text if ord(ch) > 0x7F and ch not in allowed}
            assert stray == set(), (type(overlay).__name__, stray)
        assert "remaining" not in _text(overlays[3], 100)
        assert "Answer within 4 min." in _text(overlays[3], 100)
    finally:
        use_theme(BUILTIN_THEMES["dark"])


# ── 9. screen readers ──────────────────────────────────────────────────────


_SENTENCE = (
    "Run a command: cd objective-create-a && npm ci && npm test. "
    "Safe mode asks before running commands. 4 options."
)


def test_opening_an_approval_announces_one_sentence() -> None:
    req = _request(args={"command": "cd objective-create-a && npm ci && npm test"})
    assert request_announcement(req) == _SENTENCE
    overlay, _service, _closed = _approval(req)
    assert overlay.dialog.announcement() == _SENTENCE


def test_the_announcer_says_it_when_the_dialog_opens() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.workspace.announcer import Announcer

    said: list[str] = []

    class _Appender:
        def announce(self, text: str) -> None:
            said.append(text)

    state = AppState.create()
    announcer = Announcer(state, _Appender())  # type: ignore[arg-type]
    announcer.start()
    try:
        state.pending_approval.set(
            _request(args={"command": "cd objective-create-a && npm ci && npm test"})
        )
    finally:
        announcer.stop()
    assert _SENTENCE in said


def test_screen_reader_dialogs_draw_no_box() -> None:
    req = _request(args={"command": "cd objective-create-a && npm ci && npm test"})
    overlay, service, _closed = _approval(req, DialogSettings(mode="Safe", plain=True))
    text = _text(overlay)
    assert not (_BOX & set(text))
    assert "Run a command (Safe)" in text
    assert "1. Yes (selected)" in text
    overlay.handle_key(Key.CHAR, "1")
    assert service.responses[-1]["allowed"] is True


def test_a_workers_request_names_it_in_the_badge() -> None:
    requester = ApprovalRequester("reviewer #2", "reviewer", "pool", "task-1")
    opened: list[ApprovalRequester] = []
    service = RecordingApprovals()
    overlay = ApprovalOverlay(
        _request(requester=requester), service, lambda: None, open_requester=opened.append
    )
    assert "reviewer #2 · o open transcript" in _text(overlay)
    overlay.handle_key(Key.CHAR, "o")
    assert opened == [requester] and service.responses == []
