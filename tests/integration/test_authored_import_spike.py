"""WFC-11 spike for isolating generated Python imports on Linux."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="Bubblewrap is Linux-only"),
]


@pytest.fixture
def import_command(tmp_path: Path) -> list[str]:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("Bubblewrap is unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    python = Path(sys._base_executable).resolve()
    prefix = Path(sys.base_prefix).resolve()
    python_mount = [] if prefix.is_relative_to("/usr") else ["--ro-bind", str(prefix), str(prefix)]
    return [
        bwrap,
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
        *python_mount,
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(workspace),
        "/workspace",
        "--chdir",
        "/workspace",
        "--",
        str(python),
        "-B",
        "-S",
        "-u",
        "-c",
        (
            "import resource; "
            "resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096)); "
            "import candidate"
        ),
    ]


def test_generated_import_uses_read_only_workspace_and_private_network(
    tmp_path: Path, import_command: list[str]
) -> None:
    workspace = tmp_path / "workspace"
    candidate = workspace / "candidate.py"
    candidate.write_text(
        """
import errno
import socket
from pathlib import Path

try:
    Path("write_attempt.txt").write_text("unexpected")
except OSError as error:
    assert error.errno == errno.EROFS, error
else:
    raise AssertionError("the workspace mount allowed a write")

assert [name for _, name in socket.if_nameindex()] == ["lo"]
print(Path("/proc/self/ns/net").readlink())
""",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    with output.open("wb") as stream:
        result = subprocess.run(
            import_command, stdout=stream, stderr=subprocess.STDOUT, timeout=5, check=False
        )
    assert result.returncode == 0, output.read_text()
    assert output.read_text().strip().startswith("net:[")
    assert output.read_text().strip() != str(Path("/proc/self/ns/net").readlink())
    assert not (workspace / "write_attempt.txt").exists()


def test_generated_import_output_is_bounded(tmp_path: Path, import_command: list[str]) -> None:
    (tmp_path / "workspace" / "candidate.py").write_text(
        "while True: print('x' * 512)\n", encoding="utf-8"
    )
    output = tmp_path / "output"
    with output.open("wb") as stream:
        result = subprocess.run(
            import_command, stdout=stream, stderr=subprocess.STDOUT, timeout=5, check=False
        )
    assert result.returncode != 0
    assert output.stat().st_size == 4096


def test_generated_import_has_wall_time_limit(tmp_path: Path, import_command: list[str]) -> None:
    (tmp_path / "workspace" / "candidate.py").write_text(
        "import time\ntime.sleep(60)\n", encoding="utf-8"
    )
    output = tmp_path / "output"
    with output.open("wb") as stream, pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            import_command, stdout=stream, stderr=subprocess.STDOUT, timeout=1, check=False
        )
