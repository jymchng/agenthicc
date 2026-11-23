"""Exercise the headless event sink and operator approval entry point."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING

import pytest

from agenthicc.runners.headless import _emit_extension_json

from .harness import reply
from .test_s04_create_tools import _NAMES, _queue_valid_authoring

if TYPE_CHECKING:
    from agenthicc.kernel import Event

    from .harness import AuthoredSession, ScriptedTransport


@pytest.mark.e2e
@pytest.mark.parametrize("policy", ["auto", "stage_only"])
async def test_headless_publication_and_cli_approval(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
    policy: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = authored_session.context
    auto = policy == "auto"
    remove_sink = context.processor.add_sink(_emit_extension_json)
    staged_counts: list[object] = []

    def collect_staging(event: Event) -> None:
        if event.event_type == "AuthoredStagingChanged":
            staged_counts.append(event.payload["staged_sets"])

    remove_staging = context.processor.add_sink(collect_staging)
    _queue_valid_authoring(authored_session, script)
    script.expect(
        "return reports the configured publication state",
        lambda request: (
            request.contains("[WORKFLOW RETURN]")
            and request.contains("loaded" if auto else "staged")
            and set(request.tools).intersection(_NAMES) == (set(_NAMES) if auto else set())
        ),
        reply("I received the publication status."),
    )
    try:
        result = await authored_session.run("default", "Create the fixture tools.")
        assert result.status == "complete", result.error
        await context.processor.drain()
    finally:
        remove_sink()
        remove_staging()
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    announcements = [line for line in lines if line.get("event") == "authored_published"]
    assert len(announcements) == int(auto)
    assert staged_counts == ([] if auto else [1])
    store = context.authored.store()
    if auto:
        assert {item["name"] for item in announcements[0]["artifacts"]} == set(_NAMES)
        assert all(item["state"] == "loaded" for item in announcements[0]["artifacts"])
    else:
        assert not store.records() and not authored_session.trust.records()
        assert not list((authored_session.project / ".agenthicc/tools").glob("*.py"))
        (staged,) = store.staged_runs()
        assert all(item.artifact.state == "staged" for item in staged.candidates)
        from agenthicc.__main__ import main

        monkeypatch.setattr(sys, "argv", ["agenthicc", "authored", "approve", staged.run_id])
        await asyncio.to_thread(main)
        assert "loaded" in capsys.readouterr().out
        await context.reload.reload(frozenset({"tools"}), reason="command")
        script.expect(
            "another headless request offers the approved tools",
            lambda request: set(_NAMES) <= set(request.tools),
            reply("The approved tools are available."),
        )
        assert (
            await authored_session.run("default", "Check the approved tools.")
        ).status == "complete"
    assert {record.artifact.name for record in store.records()} == set(_NAMES)
    assert len(authored_session.trust.records()) == 2
    assert store.staged_runs() == ()
