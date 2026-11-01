"""Run-owned canonical workflow package transactions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest

from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
from agenthicc.workflows.create_workflow.authoring import (
    AuthoringError,
    AuthoringLease,
    PENDING_MARKER_NAME,
    begin_authoring,
    canonical_workflow_path,
    finalize_authoring,
    inspect_existing_target,
    prepare_authoring,
    restore_authoring,
    scan_committed_package,
    scan_package,
)
from agenthicc.workflows.create_workflow.generation_tools import make_generation_writer_tools

pytestmark = pytest.mark.unit


def _active_lease(root: Path, *, name: str = "demo", run_id: str = "run-1") -> AuthoringLease:
    snapshot = inspect_existing_target(root, run_id=run_id, workflow_name=name)
    lease = prepare_authoring(
        root,
        run_id=run_id,
        workflow_name=name,
        expected_original_fingerprint=snapshot.fingerprint,
    )
    return begin_authoring(lease)


async def _call(tools: list[Callable[..., object]], name: str, *args: object) -> object:
    tool = next(tool for tool in tools if getattr(tool, "__name__", "") == name)
    return await cast(Callable[..., Awaitable[object]], tool)(*args)


def test_replacement_restores_original_trust_records_exactly(tmp_path: Path) -> None:
    trust = ExtensionTrust(
        store_path=tmp_path / "home" / ".agenthicc" / "trust.json",
        trusted_roots=(),
    )
    with override_extension_trust(trust):
        target = canonical_workflow_path(tmp_path, "demo")
        target.mkdir(parents=True)
        original_source = "from agenthicc.workflows.plugin import WorkflowPlugin\n"
        original = target / "runner.py"
        original.write_text(original_source, encoding="utf-8")
        original_record = trust.trust(original, kind="workflow")

        snapshot = inspect_existing_target(
            tmp_path,
            run_id="replace-run",
            workflow_name="demo",
        )
        lease = prepare_authoring(
            tmp_path,
            run_id="replace-run",
            workflow_name="demo",
            expected_original_fingerprint=snapshot.fingerprint,
            replace_existing=True,
        )
        assert lease.original_trust_records[0].trusted_at == original_record.trusted_at
        assert lease.to_dict()["original_trust_records"] == [
            {
                "path": original_record.path,
                "sha256": original_record.sha256,
                "kind": original_record.kind,
                "trusted_at": original_record.trusted_at,
            }
        ]

        begin_authoring(lease)
        assert trust.records() == ()
        (target / "runner.py").write_text("candidate = True\n", encoding="utf-8")
        trust.trust(target / "runner.py", kind="workflow")

        assert restore_authoring(lease) == "restored"
        assert original.read_text(encoding="utf-8") == original_source
        assert trust.records() == (original_record,)
        assert trust.check(original, kind="workflow") is None


def test_authoring_preflight_requires_unchanged_original_and_replacement_approval(
    tmp_path: Path,
) -> None:
    target = canonical_workflow_path(tmp_path, "demo")
    target.mkdir(parents=True)
    original = target / "runner.py"
    original.write_text("original = True\n", encoding="utf-8")
    snapshot = inspect_existing_target(tmp_path, run_id="run-1", workflow_name="demo")

    with pytest.raises(AuthoringError, match="explicit replacement approval"):
        prepare_authoring(
            tmp_path,
            run_id="run-1",
            workflow_name="demo",
            expected_original_fingerprint=snapshot.fingerprint,
        )
    assert original.read_text(encoding="utf-8") == "original = True\n"
    assert not (target / PENDING_MARKER_NAME).exists()

    with pytest.raises(AuthoringError, match="changed after replacement preflight"):
        prepare_authoring(
            tmp_path,
            run_id="run-1",
            workflow_name="demo",
            expected_original_fingerprint="0" * 64,
            replace_existing=True,
        )
    assert original.read_text(encoding="utf-8") == "original = True\n"


def test_same_name_run_cannot_claim_an_active_package(tmp_path: Path) -> None:
    lease = _active_lease(tmp_path)
    target = Path(lease.target_path)
    (target / "runner.py").write_text("owned by run one\n", encoding="utf-8")

    with pytest.raises(AuthoringError, match="pending marker"):
        _active_lease(tmp_path, run_id="run-2")

    assert (target / "runner.py").read_text(encoding="utf-8") == "owned by run one\n"


@pytest.mark.asyncio
async def test_generation_writers_allow_only_package_relative_paths(tmp_path: Path) -> None:
    lease = _active_lease(tmp_path)
    tools = make_generation_writer_tools(lease)
    target = Path(lease.target_path)

    invalid_paths = (
        "../outside.py",
        str(tmp_path / "outside.py"),
        r"..\\outside.py",
        PENDING_MARKER_NAME,
    )
    for path in invalid_paths:
        result = await _call(tools, "write_file", path, "unsafe = True\n")
        assert isinstance(result, dict) and result["ok"] is False

    outside = tmp_path / "outside.py"
    assert not outside.exists()
    created = await _call(tools, "make_directory", "helpers")
    assert isinstance(created, dict) and created["ok"] is True
    result = await _call(tools, "write_file", "helpers/tool.py", "VALUE = 1\n")
    assert isinstance(result, dict) and result["ok"] is True
    appended = await _call(tools, "append_file", "helpers/tool.py", "OTHER = 2\n")
    assert isinstance(appended, dict) and appended["ok"] is True
    assert (target / "helpers" / "tool.py").read_text(encoding="utf-8") == (
        "VALUE = 1\nOTHER = 2\n"
    )


@pytest.mark.asyncio
async def test_generation_writer_rejects_symlinked_package_file(tmp_path: Path) -> None:
    lease = _active_lease(tmp_path)
    tools = make_generation_writer_tools(lease)
    outside = tmp_path / "outside.py"
    outside.write_text("original = True\n", encoding="utf-8")
    try:
        (Path(lease.target_path) / "escape.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    result = await _call(tools, "write_file", "escape.py", "modified = True\n")

    assert isinstance(result, dict) and result["ok"] is False
    assert outside.read_text(encoding="utf-8") == "original = True\n"


def test_finalize_records_exact_trust_then_removes_pending_marker(tmp_path: Path) -> None:
    trust = ExtensionTrust(store_path=tmp_path / "home" / ".agenthicc" / "trust.json")
    with override_extension_trust(trust):
        lease = _active_lease(tmp_path)
        target = Path(lease.target_path)
        source = "from agenthicc.workflows.plugin import WorkflowPlugin\n"
        (target / "runner.py").write_text(source, encoding="utf-8")
        manifest = scan_package(lease)

        finalize_authoring(lease, expected_fingerprint=manifest.fingerprint)

        assert not (target / PENDING_MARKER_NAME).exists()
        assert trust.check(target / "runner.py", kind="workflow") is None
        committed = scan_committed_package(lease, expected_fingerprint=manifest.fingerprint)
        assert committed.fingerprint == manifest.fingerprint


def test_finalize_rejects_mutated_package_and_keeps_it_pending(tmp_path: Path) -> None:
    trust = ExtensionTrust(store_path=tmp_path / "home" / ".agenthicc" / "trust.json")
    with override_extension_trust(trust):
        lease = _active_lease(tmp_path)
        target = Path(lease.target_path)
        runner = target / "runner.py"
        runner.write_text("before = True\n", encoding="utf-8")
        manifest = scan_package(lease)
        runner.write_text("after = True\n", encoding="utf-8")

        with pytest.raises(AuthoringError, match="changed after validation approval"):
            finalize_authoring(lease, expected_fingerprint=manifest.fingerprint)

        assert (target / PENDING_MARKER_NAME).exists()
        assert trust.records() == ()


def test_restore_discards_first_generation_candidate(tmp_path: Path) -> None:
    lease = _active_lease(tmp_path)
    target = Path(lease.target_path)
    (target / "runner.py").write_text("candidate = True\n", encoding="utf-8")

    assert restore_authoring(lease) == "discarded"
    assert not target.exists()
