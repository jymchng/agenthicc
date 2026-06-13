from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from agenthicc.plugins.trust import check_trust, _sha256
from agenthicc.plugins.audit import record_call
from agenthicc.plugins.deps import prompt_install

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Trust tests
# ---------------------------------------------------------------------------

def test_known_hash_skips_prompt(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("x = 1\n")
    tf = tmp_path / "trusted.json"
    h = _sha256(f)
    tf.write_text(
        f'{{"version":1,"trusted":{{"{f}":{{"sha256":"{h}"}}}}}}'
    )
    decision = check_trust(f, trust_file=tf, interactive=True)
    assert decision == "trust_once"   # hash matches → no prompt


def test_auto_trust_skips_prompt(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("x = 1\n")
    tf = tmp_path / "trusted.json"
    decision = check_trust(f, auto_trust=True, trust_file=tf)
    assert decision in ("trust_once", "always_trust")


def test_headless_mode_skips_untrusted(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("x = 1\n")
    tf = tmp_path / "trusted.json"
    decision = check_trust(f, interactive=False, trust_file=tf)
    assert decision == "skip"


# ---------------------------------------------------------------------------
# Audit tests
# ---------------------------------------------------------------------------

def test_record_call_writes_jsonl(tmp_path):
    audit = tmp_path / "audit.jsonl"
    record_call(
        agent_name="researcher",
        tool_name="search_arxiv",
        args={"query": "llm"},
        ok=True,
        duration_ms=500.0,
        audit_file=audit,
    )
    lines = audit.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool"] == "search_arxiv"
    assert record["ok"] is True


# ---------------------------------------------------------------------------
# Deps tests
# ---------------------------------------------------------------------------

def test_prompt_install_headless_skips(tmp_path):
    """Headless mode must never block or install."""
    result = prompt_install(
        tmp_path / "t.py",
        ["httpx>=0.27"],
        auto_install=True,   # even with auto_install=True ...
        interactive=False,   # ... headless wins
    )
    assert result is False   # skip


def test_prompt_install_auto_install_calls_pip(tmp_path):
    with patch("agenthicc.plugins.deps._run_install") as mock_install:
        result = prompt_install(
            tmp_path / "t.py",
            ["httpx>=0.27"],
            auto_install=True,
            interactive=True,
        )
    assert result is True
    mock_install.assert_called_once_with(["httpx>=0.27"], target="venv")


def test_prompt_install_interactive_install_choice(tmp_path):
    with patch("builtins.input", return_value="I"), \
         patch("agenthicc.plugins.deps._run_install") as mock_install:
        result = prompt_install(
            tmp_path / "t.py",
            ["requests"],
            auto_install=False,
            interactive=True,
        )
    assert result is True
    mock_install.assert_called_once()


def test_prompt_install_interactive_skip_choice(tmp_path):
    with patch("builtins.input", return_value="S"):
        result = prompt_install(
            tmp_path / "t.py",
            ["requests"],
            auto_install=False,
            interactive=True,
        )
    assert result is False


def test_prompt_install_quit_raises_system_exit(tmp_path):
    with patch("builtins.input", return_value="Q"), pytest.raises(SystemExit):
        prompt_install(
            tmp_path / "t.py",
            ["requests"],
            auto_install=False,
            interactive=True,
        )
