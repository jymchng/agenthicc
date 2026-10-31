"""Offline performance probes and the AGX-14 budget ratchet."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import select
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUDGET_PATH = ROOT / "docs" / "reference" / "performance-budgets.json"
_SCOPE_ORDER = {"local": 0, "ci": 1, "weekly": 2}


@dataclass(frozen=True)
class MetricBudget:
    """One upper-bound budget, optionally ratcheted in both directions."""

    value: float
    tolerance: float
    scope: Literal["local", "ci", "weekly"]
    ratchet: Literal["upper", "two_way"] = "upper"
    gate_scopes: frozenset[str] = frozenset(("local", "ci", "weekly"))


def _is_finite_number(value: float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _read_budgets(path: Path) -> dict[str, MetricBudget]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError(f"{path} must be a version 1 performance budget document")
    metrics = raw.get("metrics")
    if not isinstance(metrics, dict):
        raise TypeError(f"{path} must contain a metrics object")
    result: dict[str, MetricBudget] = {}
    for name, entry in metrics.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise TypeError(f"{path}: every metric must map to a budget object")
        value = entry.get("budget")
        tolerance = entry.get("tolerance")
        scope = entry.get("scope")
        ratchet = entry.get("ratchet", "upper")
        gate_scopes = entry.get("gate_scopes", list(_SCOPE_ORDER))
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not _is_finite_number(value)
            or not isinstance(tolerance, (int, float))
            or isinstance(tolerance, bool)
            or not _is_finite_number(tolerance)
            or value < 0
            or tolerance < 0
            or not isinstance(scope, str)
            or scope not in _SCOPE_ORDER
            or ratchet not in ("upper", "two_way")
            or not isinstance(gate_scopes, list)
            or any(
                not isinstance(gate_scope, str) or gate_scope not in _SCOPE_ORDER
                for gate_scope in gate_scopes
            )
        ):
            raise ValueError(f"{path}: invalid budget entry for {name!r}")
        result[name] = MetricBudget(
            float(value),
            float(tolerance),
            cast("Literal['local', 'ci', 'weekly']", scope),
            ratchet,
            frozenset(gate_scopes),
        )
    return result


def check_budgets(
    measurements: Mapping[str, float], budgets: Mapping[str, MetricBudget], scope: str
) -> list[str]:
    """Return named budget violations for the selected suite scope."""
    problems: list[str] = []
    expected = {
        name for name, entry in budgets.items() if _SCOPE_ORDER[entry.scope] <= _SCOPE_ORDER[scope]
    }
    for name in sorted(expected - measurements.keys()):
        problems.append(f"{name}: probe did not report a measurement")
    for name in sorted(measurements.keys() - expected):
        problems.append(f"{name}: no {scope} budget is defined")
    for name in sorted(measurements.keys() & expected):
        value = measurements[name]
        entry = budgets[name]
        if scope not in entry.gate_scopes:
            continue
        ceiling = entry.value * (1 + entry.tolerance)
        if value > ceiling:
            problems.append(
                f"{name}: over budget (measured={value:.6g}, budget={entry.value:.6g}, "
                f"tolerance={entry.tolerance:.1%})"
            )
        elif entry.ratchet == "two_way" and value < entry.value * (1 - entry.tolerance):
            problems.append(
                f"{name}: budget is stale (measured={value:.6g}, budget={entry.value:.6g}); "
                "lower it with --update"
            )
    return problems


def check_budget_changes(
    baseline: Mapping[str, MetricBudget],
    current: Mapping[str, MetricBudget],
    *,
    increase_approved: bool,
) -> list[str]:
    """Reject deleted budgets and require a label for any weakened gate."""
    problems = [
        f"{name}: budget was removed; restore the metric instead of dropping its gate"
        for name in sorted(baseline.keys() - current.keys())
    ]
    if increase_approved:
        return problems

    for name in sorted(baseline.keys() & current.keys()):
        before = baseline[name]
        after = current[name]
        weakened = (
            after.value > before.value
            or after.tolerance > before.tolerance
            or _SCOPE_ORDER[after.scope] > _SCOPE_ORDER[before.scope]
            or not before.gate_scopes.issubset(after.gate_scopes)
            or (before.ratchet == "two_way" and after.ratchet == "upper")
        )
        if weakened:
            problems.append(f"{name}: budget gate was weakened; add the perf-budget-increase label")
    return problems


def _project_environment(home: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    source = str(ROOT / "src")
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AGENTHICC_CONFIG"):
        environment.pop(key, None)
    return environment


def _run_cli_probe(arguments: Sequence[str], samples: int = 5) -> tuple[float, float]:
    """Return best process CPU seconds and imported module count."""
    code = """\
import contextlib, io, json, runpy, sys, time
sys.argv = ["agenthicc", *json.loads(sys.argv[1])]
started = time.process_time()
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            runpy.run_module("agenthicc", run_name="__main__", alter_sys=True)
        except SystemExit as error:
            if error.code not in (None, 0):
                raise
except BaseException:
    raise
print(json.dumps({"cpu_s": time.process_time() - started, "modules": len(sys.modules)}))
"""
    cpu_samples: list[float] = []
    modules = 0
    for _ in range(samples):
        with tempfile.TemporaryDirectory(prefix="agenthicc-perf-cli-") as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            result = subprocess.run(
                [sys.executable, "-c", code, json.dumps(list(arguments))],
                cwd=temporary,
                env=_project_environment(home),
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        if result.returncode != 0:
            raise RuntimeError(f"CLI probe {list(arguments)!r} failed: {result.stderr[-1000:]}")
        lines = result.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError(f"CLI probe {list(arguments)!r} produced no measurement")
        payload: object = json.loads(lines[-1])
        if not isinstance(payload, dict):
            raise TypeError("CLI probe returned a non-object")
        cpu = payload.get("cpu_s")
        module_count = payload.get("modules")
        if not isinstance(cpu, (int, float)) or not isinstance(module_count, int):
            raise TypeError("CLI probe returned invalid CPU or module metrics")
        cpu_samples.append(float(cpu))
        modules = module_count
    return min(cpu_samples), float(modules)


_TUI_PROBE = r"""\
import json, os, runpy, sys, time
out = sys.argv[1]
started_cpu = time.process_time()
started_wall = time.perf_counter()
from agenthicc.runners import startup
original = startup.StartupCoordinator.finish
snapshot = {}
def finish(self, name, *args, **kwargs):
    report = original(self, name, *args, **kwargs)
    if name == "first_frame":
        snapshot["modules"] = len(sys.modules)
        snapshot["first_frame_cpu_s"] = time.process_time() - started_cpu
        snapshot["first_frame_wall_s"] = time.perf_counter() - started_wall
    if name == "time_to_interactive_input":
        snapshot["state"] = report.state.value
        snapshot["prompt_cpu_s"] = time.process_time() - started_cpu
        snapshot["prompt_wall_s"] = time.perf_counter() - started_wall
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        os._exit(0)
    return report
startup.StartupCoordinator.finish = finish
sys.argv = ["agenthicc"]
runpy.run_module("agenthicc", run_name="__main__", alter_sys=True)
"""


def _run_tui_probe(timeout_s: float = 120.0) -> dict[str, float]:
    if os.name != "posix":
        raise RuntimeError("the first-prompt probe requires a POSIX pseudo-terminal")
    import fcntl
    import pty
    import struct
    import termios

    with tempfile.TemporaryDirectory(prefix="agenthicc-perf-tui-") as temporary:
        root = Path(temporary)
        home = root / "home"
        project = root / "project"
        home.mkdir()
        project.mkdir()
        output_path = root / "first-prompt.json"
        environment = _project_environment(home)
        environment["TERM"] = "xterm-256color"
        pid, descriptor = pty.fork()
        if pid == 0:  # pragma: no cover - child is replaced by exec
            os.chdir(project)
            os.execve(
                sys.executable, [sys.executable, "-c", _TUI_PROBE, str(output_path)], environment
            )
        fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        deadline = time.monotonic() + timeout_s
        status = 0
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([descriptor], [], [], 0.2)
                if ready:
                    try:
                        os.read(descriptor, 65536)
                    except OSError:
                        pass
                finished, status = os.waitpid(pid, os.WNOHANG)
                if finished:
                    break
            else:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
                raise TimeoutError("the TUI did not reach its first interactive prompt")
        finally:
            os.close(descriptor)
        if not output_path.exists():
            raise RuntimeError(f"TUI probe exited before its first prompt (status={status})")
        raw: object = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("TUI probe returned a non-object")
        result: dict[str, float] = {}
        for source, target in (
            ("modules", "tui.first_prompt.modules"),
            ("first_frame_cpu_s", "tui.first_frame.cpu_s"),
            ("first_frame_wall_s", "tui.first_frame.wall_s"),
            ("prompt_cpu_s", "tui.first_prompt.cpu_s"),
            ("prompt_wall_s", "tui.first_prompt.wall_s"),
        ):
            value = raw.get(source)
            if not isinstance(value, (int, float)):
                raise TypeError(f"TUI probe omitted {source}")
            result[target] = float(value)
        if raw.get("state") != "ready":
            raise RuntimeError(f"TUI did not become ready: {raw.get('state')!r}")
        return result


def _percentile(values: Sequence[float], fraction: float = 0.95) -> float:
    if not values:
        raise ValueError("a performance sample must not be empty")
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5))
    return ordered[index]


def _render_probe(samples: int, idle_seconds: float) -> dict[str, float]:
    from io import StringIO
    from unittest.mock import patch

    from rich.console import Console
    from rich.text import Text

    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.workspace import Workspace

    state = AppState.create()
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120, height=40)
    workspace = Workspace(state, console)
    workspace.start()

    def type_prompt(prompt: str) -> tuple[list[float], list[float], int]:
        buffer = list(prompt)
        state.input.buf.set(buffer)
        state.input.cursor.set(len(buffer))
        cpu: list[float] = []
        wall: list[float] = []
        with patch.object(Text, "from_markup", wraps=Text.from_markup) as parser:
            for index in range(samples):
                buffer.append("abcdefgh"[index % 8])
                cpu_started = time.thread_time()
                wall_started = time.perf_counter()
                state.input.buf.set(list(buffer))
                state.input.cursor.set(len(buffer))
                cpu.append((time.thread_time() - cpu_started) * 1_000)
                wall.append((time.perf_counter() - wall_started) * 1_000)
            markup_parses = parser.call_count
        return cpu, wall, markup_parses

    try:
        short_cpu, short_wall, markup_parses = type_prompt("y " * 5_000)
        long_lines = "\\n".join(f"line {index:03d} of a long prompt" for index in range(200))
        lines_cpu, lines_wall, _ = type_prompt(long_lines)
        state.conversation.begin_turn("agent", "perf-turn")
        workspace._flush_redraw()
        spinner_bytes: list[float] = []
        spinner_cpu: list[float] = []
        spinner_wall: list[float] = []
        for _ in range(samples):
            output.seek(0)
            output.truncate(0)
            state.conversation.frame.set(state.conversation.frame() + 1)
            cpu_started = time.thread_time()
            wall_started = time.perf_counter()
            workspace._flush_redraw()
            spinner_cpu.append((time.thread_time() - cpu_started) * 1_000)
            spinner_wall.append((time.perf_counter() - wall_started) * 1_000)
            spinner_bytes.append(float(len(output.getvalue().encode())))
    finally:
        workspace.stop()

    idle = asyncio.run(_idle_tui_probe(idle_seconds))
    return {
        "render.composer_10000.cpu_p95_ms": _percentile(short_cpu),
        "render.composer_10000.wall_p95_ms": _percentile(short_wall),
        "render.composer_200_lines.cpu_p95_ms": _percentile(lines_cpu),
        "render.composer_200_lines.wall_p95_ms": _percentile(lines_wall),
        "render.composer_10000.markup_parses_per_frame": markup_parses / (2 * samples),
        "render.spinner_frame.cpu_p95_ms": _percentile(spinner_cpu),
        "render.spinner_frame.wall_p95_ms": _percentile(spinner_wall),
        "render.spinner_frame.bytes_p50": _percentile(spinner_bytes, 0.5),
        "tui.idle.cpu_fraction": idle["cpu_fraction"],
        "tui.idle.frames_per_minute": idle["frames_per_minute"],
    }


async def _idle_tui_probe(seconds: float) -> dict[str, float]:
    import asyncio
    from io import StringIO

    from rich.console import Console

    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.workspace import Workspace

    state = AppState.create()
    workspace = Workspace(
        state, Console(file=StringIO(), force_terminal=True, width=120, height=40)
    )
    workspace.start()
    try:
        await asyncio.sleep(0.2)
        stats = workspace.frame_stats()
        if stats is None:
            raise RuntimeError("workspace did not publish frame statistics")
        before = stats.frames
        cpu_started = time.process_time()
        wall_started = time.perf_counter()
        deadline = wall_started + seconds
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.1)
            state.conversation.tick()
        elapsed = time.perf_counter() - wall_started
        frames = stats.frames - before
        return {
            "cpu_fraction": (time.process_time() - cpu_started) / elapsed,
            "frames_per_minute": frames * 60 / elapsed,
        }
    finally:
        workspace.stop()


def _environment_report() -> dict[str, object]:
    try:
        load: list[float] | None = [float(value) for value in os.getloadavg()]
    except (AttributeError, OSError):
        load = None
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_model": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "load_average": load,
    }


def collect_local_metrics(samples: int = 5, idle_seconds: float = 2.0) -> dict[str, float]:
    """Collect fixture-free CLI, startup and render metrics without network access."""
    help_cpu, help_modules = _run_cli_probe(("--help",), samples)
    agents_cpu, agents_modules = _run_cli_probe(("agents", "--json"), samples)
    metrics = {
        "cli.help.cpu_s": help_cpu,
        "cli.help.modules": help_modules,
        "cli.agents_json.cpu_s": agents_cpu,
        "cli.agents_json.modules": agents_modules,
    }
    if os.name == "posix":
        metrics.update(_run_tui_probe())
    metrics.update(_render_probe(max(1, samples), idle_seconds))
    return metrics


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return cast("dict[str, object]", value)


def _number_field(value: Mapping[str, object], name: str) -> float:
    metric = value.get(name)
    if not isinstance(metric, (int, float)) or isinstance(metric, bool):
        raise TypeError(f"probe omitted numeric metric {name!r}")
    return float(metric)


def _run_json_script(script: str, arguments: Sequence[str], timeout_s: float) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agenthicc-perf-probe-") as temporary:
        home = Path(temporary) / "home"
        home.mkdir()
        result = subprocess.run(
            [sys.executable, str(ROOT / script), *arguments],
            cwd=ROOT,
            env=_project_environment(home),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    if result.returncode != 0:
        raise RuntimeError(f"{script} failed: {result.stderr[-2000:]}")
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{script} did not emit valid JSON: {exc}") from exc
    return _json_object(payload, script)


def _run_agents_profile(
    sessions: int, events: int, samples: int, idle_seconds: float
) -> dict[str, object]:
    return _run_json_script(
        "scripts/benchmark_agents_manager.py",
        (
            "--sessions",
            str(sessions),
            "--events",
            str(events),
            "--samples",
            str(samples),
            "--idle-seconds",
            str(idle_seconds),
        ),
        timeout_s=1_800.0,
    )


def _profile_metrics(profile: Mapping[str, object], sessions: int) -> dict[str, float]:
    measurements = _json_object(profile.get("measurements"), "agents profile measurements")
    first_page = _json_object(
        measurements.get("fresh_process_first_page"), "fresh process first page"
    )
    search = _json_object(measurements.get("search"), "agents search")
    live = _json_object(measurements.get("live_screen"), "agents live screen")
    change = _json_object(live.get("change_to_visible"), "change-to-visible profile")
    return {
        f"agents.first_page_{sessions // 1_000}k.cpu_p95_ms": _number_field(
            first_page, "cpu_p95_ms"
        ),
        f"agents.search_{sessions // 1_000}k.cpu_p95_ms": _number_field(search, "cpu_p95_ms"),
        f"agents.idle_{sessions // 1_000}k.cpu_fraction": _number_field(live, "idle_cpu_fraction"),
        f"agents.idle_{sessions // 1_000}k.events_appended": _number_field(
            live, "idle_appended_events"
        ),
        f"agents.change_visible_{sessions // 1_000}k.wall_p95_ms": _number_field(change, "p95_ms"),
    }


def _collect_ci_profiles(
    samples: int, idle_seconds: float
) -> tuple[dict[str, float], dict[str, object]]:
    profile = _run_agents_profile(1_000, 50_000, samples, max(30.0, idle_seconds))
    measurements = _profile_metrics(profile, 1_000)
    replay = _run_json_script("scripts/perf_goal_flow_replay.py", (), timeout_s=1_800.0)
    steps = _number_field(replay, "steps")
    if steps != 510:
        raise RuntimeError(f"goal-flow replay reported {steps:g} steps instead of 510")
    measurements.update(
        {
            "turn.replay_goal_flow.cpu_s": _number_field(replay, "cpu_s"),
            "turn.replay_goal_flow.deepcopies_per_step": _number_field(
                replay, "deepcopies_per_step"
            ),
            "turn.replay_goal_flow.full_validations_per_step": _number_field(
                replay, "full_validations_per_step"
            ),
        }
    )
    return measurements, {"agents_1k": profile, "goal_flow_replay": replay}


def _collect_weekly_profiles(
    samples: int, idle_seconds: float
) -> tuple[dict[str, float], dict[str, object]]:
    profile = _run_agents_profile(10_000, 1_000_000, samples, max(30.0, idle_seconds))
    return _profile_metrics(profile, 10_000), {"agents_10k": profile}


def collect_scope_metrics(
    scope: Literal["local", "ci", "weekly"], samples: int, idle_seconds: float
) -> tuple[dict[str, float], dict[str, object]]:
    """Collect all probes required through *scope* and retain raw profiles."""
    measurements = collect_local_metrics(samples, idle_seconds)
    profiles: dict[str, object] = {}
    if scope in ("ci", "weekly"):
        ci_metrics, ci_profiles = _collect_ci_profiles(samples, idle_seconds)
        measurements.update(ci_metrics)
        profiles.update(ci_profiles)
    if scope == "weekly":
        weekly_metrics, weekly_profiles = _collect_weekly_profiles(samples, idle_seconds)
        measurements.update(weekly_metrics)
        profiles.update(weekly_profiles)
    return measurements, profiles


def _format_report(
    measurements: Mapping[str, float],
    budgets: Mapping[str, MetricBudget],
    environment: Mapping[str, object],
    scope: str,
) -> str:
    lines = ["agenthicc performance report", json.dumps(environment, sort_keys=True), ""]
    for name, value in sorted(measurements.items()):
        entry = budgets.get(name)
        if entry is None:
            lines.append(f"{name}: {value:.6g} (no budget)")
            continue
        ceiling = entry.value * (1 + entry.tolerance)
        state = "DIAG" if scope not in entry.gate_scopes else "PASS"
        if scope in entry.gate_scopes:
            if value > ceiling:
                state = "OVER"
            elif entry.ratchet == "two_way" and value < entry.value * (1 - entry.tolerance):
                state = "STALE"
        lines.append(
            f"{state:>4}  {name}: {value:.6g} / {entry.value:.6g} (+{entry.tolerance:.0%})"
        )
    return "\n".join(lines)


def _write_updated_budgets(path: Path, measurements: Mapping[str, float]) -> None:
    budgets = _read_budgets(path)
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("metrics"), dict):
        raise TypeError(f"{path} does not contain a metrics object")
    metrics = cast("dict[str, dict[str, object]]", raw["metrics"])
    for name, value in measurements.items():
        if name not in metrics:
            raise ValueError(f"{name}: add an approved budget entry before updating")
        if budgets[name].ratchet == "two_way":
            if value > budgets[name].value:
                raise ValueError(
                    f"{name}: --update cannot raise a budget; request the perf-budget-increase label"
                )
            metrics[name]["budget"] = round(value, 6)
    path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def report_local() -> None:
    """Run the offline, fixture-free diagnostics and print them beside budgets."""
    budget_path = DEFAULT_BUDGET_PATH
    if not budget_path.is_file():
        installed = (
            Path(sys.prefix)
            / "share"
            / "agenthicc"
            / "docs"
            / "reference"
            / "performance-budgets.json"
        )
        budget_path = installed
    budgets = _read_budgets(budget_path)
    measurements = collect_local_metrics()
    print(_format_report(measurements, budgets, _environment_report(), "local"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and ratchet AGX performance probes.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="fail on over-budget or stale metrics")
    action.add_argument(
        "--update", action="store_true", help="lower exact-count budgets to measurements"
    )
    parser.add_argument("--scope", choices=tuple(_SCOPE_ORDER), default="local")
    parser.add_argument("--budgets", type=Path, default=DEFAULT_BUDGET_PATH)
    parser.add_argument("--report", type=Path, help="write measurements and raw profiles as JSON")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.samples < 1 or args.idle_seconds <= 0:
        parser.error("--samples must be positive and --idle-seconds must be greater than zero")
    try:
        budgets = _read_budgets(args.budgets)
        measurements, profiles = collect_scope_metrics(args.scope, args.samples, args.idle_seconds)
        problems = check_budgets(measurements, budgets, args.scope) if args.check else []
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Performance probes failed: {exc}", file=sys.stderr)
        return 1

    environment = _environment_report()
    print(_format_report(measurements, budgets, environment, args.scope))
    if args.report is not None:
        report = {
            "version": 1,
            "scope": args.scope,
            "environment": environment,
            "measurements": measurements,
            "profiles": profiles,
            "budget_violations": problems,
        }
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"Could not write performance report {args.report}: {exc}", file=sys.stderr)
            return 1
    if args.update:
        try:
            _write_updated_budgets(args.budgets, measurements)
        except (OSError, TypeError, ValueError) as exc:
            print(f"Could not update performance budgets: {exc}", file=sys.stderr)
            return 1
        print(f"Updated measured budgets in {args.budgets}.")
        return 0
    if problems:
        print("Performance budget check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    return 0
