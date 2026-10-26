"""The interactive TUI draws its first frame without the agent runtime (AGX-02).

The TUI is started in a pseudo-terminal. A probe snapshots ``sys.modules`` when
the first frame finishes — before the post-frame runtime task can start — and
exits once the prompt accepts input. Module counts are exact on every host, so
they are the regression signal; CPU and wall-time budgets belong to the AGX-14
benchmark job.
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
from pathlib import Path

import pytest

from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal"),
]

_PROBE = """
import json, os, sys
out = sys.argv[1]
from agenthicc.runners import startup
original = startup.StartupCoordinator.finish
snapshot = {}
def finish(self, name, *args, **kwargs):
    report = original(self, name, *args, **kwargs)
    if name == "first_frame":
        snapshot["modules"] = sorted(sys.modules)
    if name == "time_to_interactive_input":
        snapshot["state"] = report.state.value
        snapshot["milestones"] = self.milestones().to_dict()
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        os._exit(0)
    return report
startup.StartupCoordinator.finish = finish
sys.argv = ["agenthicc"]
import runpy
runpy.run_module("agenthicc", run_name="__main__", alter_sys=True)
"""

# Loaded only after the first frame: the agent runtime, provider HTTP stacks,
# the account keyring, optional integrations and Rich's emoji table.
_DENIED = (
    "lauren_ai",
    "lauren",
    "pydantic",
    "anyio",
    "httpx",
    "httpcore",
    "aiohttp",
    "keyring",
    "secretstorage",
    "agenthicc.session_service.transport",
    "agenthicc.tools.mcp",
    "agenthicc.tools.playwright",
    "agenthicc.tools.cloakbrowser",
    "agenthicc.memory.journaled",
    "agenthicc.memory.vector",
    "agenthicc.runners.agent_turn",
    "rich._emoji_codes",
)

#: asyncio and its transports alone account for about 90 of these modules.
_FIRST_FRAME_MODULE_BUDGET = 370


def _run_tui_probe(tmp_path: Path) -> dict[str, object]:
    import fcntl
    import pty
    import struct
    import termios

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir(parents=True)
    out = tmp_path / "first-frame.json"
    environment = dict(os.environ)
    environment.update(
        HOME=str(home),
        PYTHONPATH=str(Path(__file__).parents[2] / "src"),
        TERM="xterm-256color",
    )
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AGENTHICC_CONFIG"):
        environment.pop(name, None)

    pid, descriptor = pty.fork()
    if pid == 0:  # pragma: no cover - replaced by the probe interpreter
        os.chdir(project)
        os.execve(sys.executable, [sys.executable, "-c", _PROBE, str(out)], environment)
    fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S
    status = 0
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.2)
            if ready:
                try:
                    if not os.read(descriptor, 65536):
                        break
                except OSError:
                    break
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                break
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail("the TUI never accepted input")
    finally:
        os.close(descriptor)
    assert out.exists(), f"probe exited with status {status} before the first prompt"
    result: dict[str, object] = json.loads(out.read_text(encoding="utf-8"))
    return result


@cli_test_timeout(1)
def test_first_frame_loads_no_agent_runtime_and_stays_within_budget(tmp_path: Path) -> None:
    result = _run_tui_probe(tmp_path)

    modules = result["modules"]
    assert isinstance(modules, list)
    denied = sorted(
        name for name in modules if any(name == d or name.startswith(f"{d}.") for d in _DENIED)
    )
    assert not denied, denied
    assert len(modules) <= _FIRST_FRAME_MODULE_BUDGET, len(modules)

    assert result["state"] == "ready"
    milestones = result["milestones"]
    assert isinstance(milestones, dict)
    assert milestones["modules_at_first_frame"] == len(modules)
    first_frame = milestones["process_to_first_frame_s"]
    assert isinstance(first_frame, float) and first_frame > 0
