"""TUX-03 §2.1, §2.4: the facts the start-up header is drawn from, and the first-run question."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.plugins.trust import ExtensionTrust
from agenthicc.runners.first_run import FirstRun, prepare_first_run, workspace_needs_decision
from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.keymap import Keymap
from agenthicc import user_state
from agenthicc.user_state import UserState, load_user_state, save_user_state

pytestmark = pytest.mark.unit

_KEYMAP = Keymap.build(DEFAULT_ACTIONS)


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory of its own, with the session index and state file inside it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    sessions = home / ".agenthicc" / "sessions"
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", sessions)
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSION_INDEX", sessions / "index.json")
    monkeypatch.setattr("agenthicc.cli.version.package_version", lambda: "0.5.0")
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return path


def _prepare(
    project: Path, home: Path, *, session_id: str = "current", **extra: object
) -> FirstRun:
    return prepare_first_run(
        config=AgenthiccConfig(),
        env={"PATH": "", "ANTHROPIC_API_KEY": "sk"},
        cwd=project,
        mode="Safe",
        session_id=session_id,
        keymap=_KEYMAP,
        trust=ExtensionTrust(store_path=home / ".agenthicc" / "trust.json"),
        home=home,
        **extra,  # type: ignore[arg-type]
    )


# ── the state file ────────────────────────────────────────────────────────────


def test_the_first_run_on_a_machine_is_known_and_records_the_version(
    machine: Path, project: Path
) -> None:
    first = _prepare(project, machine)

    assert first.first_run
    assert not first.whats_new
    assert first.version == "0.5.0"
    state = load_user_state()
    assert (state.version, state.release_notes_seen, state.tip_index) == ("0.5.0", "0.5.0", 1)
    assert json.loads(user_state.state_path().read_text()) == {
        "release_notes_seen": "0.5.0",
        "tip_index": 1,
        "version": "0.5.0",
    }


def test_the_mascot_run_never_comes_back(machine: Path, project: Path) -> None:
    assert _prepare(project, machine).first_run
    assert not _prepare(project, machine).first_run
    assert not _prepare(project, machine).first_run


def test_whats_new_appears_once_after_an_upgrade_and_not_on_the_next_start(
    machine: Path, project: Path
) -> None:
    save_user_state(UserState(version="0.4.0", release_notes_seen="0.4.0", tip_index=7))

    upgraded = _prepare(project, machine)
    again = _prepare(project, machine)

    assert (upgraded.first_run, upgraded.whats_new) == (False, True)
    assert (again.first_run, again.whats_new) == (False, False)
    # The notes were not read, so /release-notes still lists everything since 0.4.0.
    assert load_user_state().release_notes_seen == "0.4.0"


def test_a_downgrade_also_says_what_changed(machine: Path, project: Path) -> None:
    save_user_state(UserState(version="0.9.0", release_notes_seen="0.9.0"))

    assert _prepare(project, machine).whats_new


def test_an_unreadable_state_file_reads_as_a_fresh_machine(machine: Path, project: Path) -> None:
    state = user_state.state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json", encoding="utf-8")

    assert _prepare(project, machine).first_run
    assert load_user_state().version == "0.5.0"  # and it is repaired


def test_a_home_that_cannot_be_written_does_not_stop_the_session(
    machine: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only home")

    monkeypatch.setattr("agenthicc.user_state.atomic_replace", refuse)

    assert _prepare(project, machine).first_run


# ── the facts ─────────────────────────────────────────────────────────────────


def test_the_branch_and_the_preflight_findings_are_gathered(machine: Path, project: Path) -> None:
    first = _prepare(project, machine)

    assert first.branch == "main"
    assert [issue.check.value for issue in first.issues] == ["tools"]  # no ripgrep on PATH ""


def test_the_tip_rotates_by_one_per_session(machine: Path, project: Path) -> None:
    (project / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")

    tips = [_prepare(project, machine).suggestion.text for _ in range(3)]

    assert len(set(tips)) == 3
    assert [_prepare(project, machine).tip_index for _ in range(2)] == [3, 4]


# ── "Work in <directory>?" ────────────────────────────────────────────────────


def _needs(directory: Path, home: Path, *, session_id: str = "current") -> bool:
    return workspace_needs_decision(
        directory,
        home=home,
        trust=ExtensionTrust(store_path=home / ".agenthicc" / "trust.json"),
        session_id=session_id,
    )


def test_an_unknown_directory_that_is_not_a_repository_is_asked_about(
    machine: Path, tmp_path: Path
) -> None:
    downloads = machine / "Downloads"
    downloads.mkdir()

    assert _needs(downloads, machine)


def test_a_repository_is_known_by_being_one(machine: Path, project: Path) -> None:
    assert not _needs(project, machine)


def test_the_home_directory_is_always_asked_about_once(machine: Path) -> None:
    assert _needs(machine, machine)
    ExtensionTrust(store_path=machine / ".agenthicc" / "trust.json").remember_workspace(
        machine, "allow"
    )
    assert not _needs(machine, machine)


def test_the_answer_is_stored_per_directory(machine: Path) -> None:
    store = ExtensionTrust(store_path=machine / ".agenthicc" / "trust.json")
    one, two = machine / "one", machine / "two"
    one.mkdir()
    two.mkdir()

    store.remember_workspace(one, "plan")

    assert not _needs(one, machine)
    assert _needs(two, machine)
    decision = store.workspace_decision(one)
    assert decision is not None and decision.decision == "plan"
    assert store.workspace_decision(two) is None


def test_a_project_configuration_file_makes_a_directory_known(machine: Path) -> None:
    directory = machine / "configured"
    (directory / ".agenthicc").mkdir(parents=True)
    assert _needs(directory, machine)

    (directory / ".agenthicc" / "agenthicc.toml").write_text("", encoding="utf-8")

    assert not _needs(directory, machine)


def test_an_earlier_session_makes_a_directory_known_but_the_current_one_does_not(
    machine: Path,
) -> None:
    from agenthicc.tui.runtime.session_log import register_session

    directory = machine / "worked-in"
    directory.mkdir()
    register_session("current", str(directory), "m")
    assert _needs(directory, machine, session_id="current")

    register_session("earlier", str(directory), "m")

    assert not _needs(directory, machine, session_id="current")


def test_a_plan_answer_is_reported_so_the_session_starts_in_plan_mode(
    machine: Path, project: Path
) -> None:
    ExtensionTrust(store_path=machine / ".agenthicc" / "trust.json").remember_workspace(
        project, "plan"
    )

    assert _prepare(project, machine).plan_mode


# ── the trust store ───────────────────────────────────────────────────────────


def test_workspace_answers_and_extension_records_share_one_file_without_loss(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "trust.json"
    store = ExtensionTrust(store_path=store_path, trusted_roots=())
    extension = tmp_path / "tool.py"
    extension.write_text("x = 1\n", encoding="utf-8")
    directory = tmp_path / "work"
    directory.mkdir()

    store.remember_workspace(directory, "allow")
    store.trust(extension, kind="tool")  # rewrites the file's records
    assert store.workspace_decision(directory) is not None
    assert store.check(extension, kind="tool") is None

    store.revoke(extension)
    assert store.workspace_decision(directory) is not None
    document = json.loads(store_path.read_text(encoding="utf-8"))
    assert document["records"] == []
    assert [entry["path"] for entry in document["workspaces"]] == [str(directory.resolve())]


def test_an_unknown_decision_is_refused(tmp_path: Path) -> None:
    store = ExtensionTrust(store_path=tmp_path / "trust.json")

    with pytest.raises(ValueError, match="allow, plan"):
        store.remember_workspace(tmp_path, "yolo")
