"""What the TUI decides before the first prompt (TUX-03).

:func:`prepare_first_run` gathers, in one blocking call that the session runs
on a worker thread after the first frame:

- the installed version, and from ``~/.agenthicc/state.json`` whether this is
  the first run on the machine and whether the user just upgraded;
- the git branch of the working directory;
- the pre-flight findings (:mod:`agenthicc.runners.preflight`);
- the composer's suggestion (:mod:`agenthicc.tui.suggestions`);
- whether to ask "Work in <directory>?", and what the user already answered.

It reads the disk and writes only the state file; it prints nothing and makes
no network request. Rendering belongs to :mod:`agenthicc.tui.welcome`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig
    from agenthicc.plugins.trust import ExtensionTrust
    from agenthicc.runners.preflight import PreflightIssue
    from agenthicc.tui.keymap import Keymap
    from agenthicc.tui.suggestions import Suggestion

__all__ = ["FirstRun", "prepare_first_run", "workspace_needs_decision"]


@dataclass(frozen=True, slots=True)
class FirstRun:
    """Everything the start-up header, notices and dialogs are drawn from."""

    version: str
    #: No version was recorded before: the first run on this machine.
    first_run: bool
    #: The user ran an older version before this one.
    whats_new: bool
    branch: str
    issues: tuple[PreflightIssue, ...]
    suggestion: Suggestion
    #: This session's place in the tip rotation (it advances once per session).
    tip_index: int
    #: Ask "Work in <directory>?" before the first prompt.
    ask_workspace: bool
    #: The user answered "Yes, in Plan mode" for this directory before.
    plan_mode: bool


def workspace_needs_decision(
    cwd: Path, *, home: Path, trust: ExtensionTrust, session_id: str
) -> bool:
    """Whether "Work in <directory>?" should be asked.

    It is asked once per directory. A git repository is known by being one;
    any other directory is known by a project configuration file, or by an
    earlier session recorded for it. The home directory is always asked about,
    once, because a session there can reach everything the user owns.
    """
    from agenthicc.tui.runtime.session_log import sessions_for_cwd
    from agenthicc.tui.statusline import repository_root

    if trust.workspace_decision(cwd) is not None:
        return False
    if cwd.resolve() == home.resolve():
        return True
    if repository_root(cwd) is not None:
        return False
    if (cwd / ".agenthicc" / "agenthicc.toml").is_file():
        return False
    return not sessions_for_cwd(str(cwd), exclude=session_id)


def prepare_first_run(
    *,
    config: AgenthiccConfig,
    env: Mapping[str, str],
    cwd: Path,
    mode: str,
    session_id: str,
    keymap: Keymap,
    trust: ExtensionTrust | None = None,
    home: Path | None = None,
    state_file: Path | None = None,
) -> FirstRun:
    """Gather the first-run facts for a session in *cwd*; record that it started."""
    from agenthicc.cli.version import package_version
    from agenthicc.plugins.trust import current_extension_trust
    from agenthicc.runners.preflight import check_session
    from agenthicc.tui.statusline import read_git_branch
    from agenthicc.tui.suggestions import static_suggestion
    from agenthicc.user_state import load_user_state, save_user_state

    version = package_version()
    state = load_user_state(state_file)
    save_user_state(state.started(version), state_file)
    policy = trust if trust is not None else current_extension_trust()
    decision = policy.workspace_decision(cwd)
    return FirstRun(
        version=version,
        first_run=state.first_run,
        whats_new=state.upgraded_to(version),
        branch=read_git_branch(cwd),
        issues=check_session(config, env, cwd=cwd, mode=mode),
        suggestion=static_suggestion(
            cwd, session_id=session_id, tip_index=state.tip_index, keymap=keymap
        ),
        tip_index=state.tip_index,
        ask_workspace=workspace_needs_decision(
            cwd,
            home=home if home is not None else Path.home(),
            trust=policy,
            session_id=session_id,
        ),
        plan_mode=decision is not None and decision.decision == "plan",
    )
