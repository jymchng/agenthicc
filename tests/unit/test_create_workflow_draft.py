"""Unit coverage for isolated create_workflow drafts and publication."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import agenthicc.workflows.create_workflow.draft as draft_module
from agenthicc.workflows.create_workflow.draft import (
    DraftError,
    build_draft_path,
    publish_draft,
    reset_draft,
    scan_draft,
)

pytestmark = pytest.mark.unit


def _draft(root: Path, run_id: str = "run-1", name: str = "demo") -> Path:
    path = build_draft_path(root, run_id, name)
    path.mkdir(parents=True)
    (path / "runner.py").write_text(
        "from agenthicc.workflows.plugin import WorkflowPlugin\n", encoding="utf-8"
    )
    return path


def test_manifest_is_exact_sorted_and_rejects_missing_entrypoint(tmp_path: Path) -> None:
    path = _draft(tmp_path)
    (path / "tools.py").write_text("TOOLS = []\n", encoding="utf-8")
    manifest = scan_draft(path, root=tmp_path, run_id="run-1", workflow_name="demo")
    assert [item.path for item in manifest.files] == ["runner.py", "tools.py"]
    assert manifest.total_bytes == sum(item.byte_length for item in manifest.files)
    assert manifest.fingerprint

    (path / "runner.py").unlink()
    with pytest.raises(DraftError, match="runner.py"):
        scan_draft(path, root=tmp_path, run_id="run-1", workflow_name="demo")


def test_manifest_rejects_symlinks_and_path_mismatch(tmp_path: Path) -> None:
    path = _draft(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        os.symlink(outside, path / "leak.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(DraftError, match="symlink"):
        scan_draft(path, root=tmp_path, run_id="run-1", workflow_name="demo")

    with pytest.raises(DraftError, match="exactly"):
        scan_draft(path, root=tmp_path, run_id="run-1", workflow_name="other")


def test_reset_draft_clears_only_the_exact_run_owned_tree(tmp_path: Path) -> None:
    path = _draft(tmp_path)
    (path / "stale.py").write_text("stale", encoding="utf-8")
    nested = path / "helpers"
    nested.mkdir()
    (nested / "old.py").write_text("old", encoding="utf-8")

    reset_draft(path, root=tmp_path, run_id="run-1", workflow_name="demo")

    assert path.is_dir()
    assert list(path.iterdir()) == []
    # The exact-path guard must not make an adjacent workflow mutable.
    sibling = build_draft_path(tmp_path, "run-1", "other")
    sibling.mkdir(parents=True)
    (sibling / "keep.py").write_text("keep", encoding="utf-8")
    with pytest.raises(DraftError, match="exactly"):
        reset_draft(sibling, root=tmp_path, run_id="run-1", workflow_name="demo")
    assert (sibling / "keep.py").exists()


def test_publication_replaces_stale_siblings_and_keeps_backup(tmp_path: Path) -> None:
    path = _draft(tmp_path)
    destination = tmp_path / ".agenthicc" / "workflows" / "demo"
    destination.mkdir(parents=True)
    (destination / "stale.py").write_text("stale", encoding="utf-8")
    (destination / "runner.py").write_text("old", encoding="utf-8")
    manifest = scan_draft(path, root=tmp_path, run_id="run-1", workflow_name="demo")

    publication = publish_draft(manifest, root=tmp_path)
    assert Path(publication.published_path) == destination
    assert (destination / "runner.py").exists()
    assert not (destination / "stale.py").exists()
    assert publication.backup_path
    assert (Path(publication.backup_path) / "stale.py").exists()


def test_publication_rejects_manifest_mutation(tmp_path: Path) -> None:
    path = _draft(tmp_path)
    manifest = scan_draft(path, root=tmp_path, run_id="run-1", workflow_name="demo")
    (path / "runner.py").write_text("mutated\n", encoding="utf-8")
    with pytest.raises(DraftError, match="changed"):
        publish_draft(manifest, root=tmp_path)


def test_publication_quarantines_a_stale_legacy_sibling(tmp_path: Path) -> None:
    path = _draft(tmp_path)
    parent = tmp_path / ".agenthicc" / "workflows"
    parent.mkdir(parents=True, exist_ok=True)
    legacy = parent / "demo.py"
    legacy.write_text("old legacy", encoding="utf-8")
    manifest = scan_draft(path, root=tmp_path, run_id="run-1", workflow_name="demo")

    publication = publish_draft(manifest, root=tmp_path)

    assert not legacy.exists()
    assert publication.backup_path
    assert (Path(publication.backup_path) / "legacy.py").read_text(encoding="utf-8") == "old legacy"
    assert publication.published_at > 0


def test_publication_restores_previous_package_when_publish_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _draft(tmp_path)
    destination = tmp_path / ".agenthicc" / "workflows" / "demo"
    destination.mkdir(parents=True)
    old_runner = destination / "runner.py"
    old_runner.write_text("old", encoding="utf-8")
    manifest = scan_draft(path, root=tmp_path, run_id="run-1", workflow_name="demo")

    original_replace = draft_module.os.replace
    failed = False

    def fail_publication(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        nonlocal failed
        if Path(source).name == "package" and Path(target) == destination and not failed:
            failed = True
            raise OSError("injected publication rename failure")
        original_replace(source, target)

    monkeypatch.setattr(draft_module.os, "replace", fail_publication)
    with pytest.raises(OSError, match="rename failure"):
        publish_draft(manifest, root=tmp_path)

    assert failed
    assert old_runner.read_text(encoding="utf-8") == "old"


def test_draft_path_rejects_symlinked_workflow_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    agenthicc = tmp_path / ".agenthicc"
    agenthicc.mkdir()
    try:
        os.symlink(outside, agenthicc / "workflows")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(DraftError, match="symlink"):
        build_draft_path(tmp_path, "run-1", "demo")
