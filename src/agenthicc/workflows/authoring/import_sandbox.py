"""Inspect generated Python in an isolated, bounded subprocess."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

_OUTPUT_BYTES = 128 * 1024
_STARTED = b"AGENTHICC_IMPORT_STARTED"
_RESULT = b"AGENTHICC_IMPORT_RESULT "
_CHILD = r"""
import importlib
import json
import resource
import sys

limit, module_name, function, arguments, *paths = sys.argv[1:]
resource.setrlimit(resource.RLIMIT_FSIZE, (int(limit), int(limit)))
sys.path.extend(paths)
sys.argv[:] = [sys.argv[0]]
print("AGENTHICC_IMPORT_STARTED", flush=True)
module = importlib.import_module(module_name)
result = vars(module)[function](json.loads(arguments))
print("\nAGENTHICC_IMPORT_RESULT " + json.dumps(result), flush=True)
"""


@dataclass(frozen=True, slots=True)
class ImportCheck:
    status: Literal["passed", "failed", "static_only"]
    result: dict[str, object] | None = None
    reason: str = ""


def inspect_generated_python(
    workspace: Path,
    check: Callable[[dict[str, object]], dict[str, object]],
    arguments: dict[str, object],
    *,
    timeout: float = 12.0,
) -> ImportCheck:
    """Invoke a runtime-owned check inside a read-only Linux workspace."""
    bubblewrap = shutil.which("bwrap") if sys.platform == "linux" else None
    if bubblewrap is None:
        return ImportCheck("static_only", reason="Bubblewrap is unavailable.")
    command = _command(
        bubblewrap,
        workspace.resolve(),
        check.__module__,
        check.__name__,
        arguments,
    )
    with tempfile.TemporaryFile() as output:
        try:
            finished = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={},
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ImportCheck("failed", reason="Isolated Python inspection timed out.")
        except OSError:
            return ImportCheck("static_only", reason="Bubblewrap could not be started.")
        output.seek(0)
        lines = output.read(_OUTPUT_BYTES).splitlines()
    if not lines or lines[0] != _STARTED:
        if lines and lines[0].startswith(b"bwrap:"):
            return ImportCheck("static_only", reason="Bubblewrap could not isolate the workspace.")
        return ImportCheck("failed", reason="The isolated Python runtime could not start.")
    if finished.returncode or not lines[-1].startswith(_RESULT):
        return ImportCheck("failed", reason="Isolated Python inspection exited without a result.")
    try:
        payload: object = json.loads(lines[-1][len(_RESULT) :])
    except (ValueError, UnicodeError):
        return ImportCheck("failed", reason="Isolated Python inspection returned invalid JSON.")
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        return ImportCheck(
            "failed", reason="Isolated Python inspection returned an invalid result."
        )
    return ImportCheck("passed", payload)


def _command(
    bubblewrap: str,
    workspace: Path,
    module_name: str,
    function: str,
    arguments: dict[str, object],
) -> list[str]:
    python = Path(sys.executable).resolve()
    prefix = Path(sys.base_prefix).resolve()
    package = Path(__file__).resolve().parents[2]
    argv = [
        bubblewrap,
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind-try",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--setenv",
        "HOME",
        "/tmp",
    ]
    if not prefix.is_relative_to("/usr"):
        argv.extend(("--ro-bind", str(prefix), str(prefix)))
    if not (python.is_relative_to(prefix) or python.is_relative_to("/usr")):
        argv.extend(("--ro-bind", str(python), str(python)))
    import_paths = ["/runtime/app"]
    argv.extend(("--ro-bind", str(package), "/runtime/app/agenthicc"))
    for source in dict.fromkeys(sysconfig.get_path(kind) for kind in ("purelib", "platlib")):
        if Path(source).is_dir():
            target = f"/runtime/deps{len(import_paths)}"
            argv.extend(("--ro-bind", source, target))
            import_paths.append(target)
    argv.extend(
        (
            "--ro-bind",
            str(workspace),
            "/workspace",
            "--chdir",
            "/workspace",
            "--",
            str(python),
            "-I",
            "-S",
            "-B",
            "-u",
            "-c",
            _CHILD,
            str(_OUTPUT_BYTES),
            module_name,
            function,
            json.dumps(arguments),
            *import_paths,
            "/workspace",
        )
    )
    return argv
