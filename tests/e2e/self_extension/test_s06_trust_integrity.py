"""A published tool whose bytes changed is refused on the next reload."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .harness import reply
from .test_s04_create_tools import _NAMES, _queue_valid_authoring

if TYPE_CHECKING:
    from .harness import AuthoredSession, ScriptedTransport


@pytest.mark.e2e
async def test_edited_published_bytes_leave_the_provider_surface(
    authored_session: AuthoredSession, script: ScriptedTransport
) -> None:
    _queue_valid_authoring(authored_session, script)
    script.expect(
        "approved bytes are callable",
        lambda request: request.contains("[WORKFLOW RETURN]") and set(_NAMES) <= set(request.tools),
        reply("The tools are ready."),
    )
    outcome = await authored_session.run("default", "Create the fixture tools.")
    assert outcome.status == "complete", outcome.error
    path = authored_session.project / ".agenthicc/tools/counts.py"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n# Edited outside the authoring workflow.\n")

    context = authored_session.context
    report = await context.reload.reload(frozenset({"tools"}), reason="command")
    assert any(
        item.code == "untrusted_modified" and item.path.endswith("counts.py")
        for item in report.diagnostics
    )
    assert not authored_session.trust.permits(path, kind="tool")
    script.expect(
        "edited schemas and trust mutations are unavailable",
        lambda request: (
            not {*_NAMES[:2], "trust", "record_artifact", "commit_authored"}.intersection(
                request.tools
            )
            and _NAMES[2] in request.tools
        ),
        reply("I can see the verified tools."),
    )
    assert (await authored_session.run("default", "Check the active tools.")).status == "complete"
