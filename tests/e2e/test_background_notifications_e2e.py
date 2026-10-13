"""Process-level notification delivery journey tests."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.notifications import BackgroundNotificationOutbox
from agenthicc.background.settings import BackgroundNotificationSettings
from agenthicc.background.store import BackgroundStore

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(sys.platform != "linux", reason="uses Linux notify-send executable contract")
def test_background_completion_delivers_local_notification_in_sender_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    receipt_file = tmp_path / "notification.json"
    notifier = bin_dir / "notify-send"
    notifier.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['AGENTHICC_NOTIFY_TEST_RECEIPT']).write_text(\n"
        "    json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    notifier.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("AGENTHICC_NOTIFY_TEST_RECEIPT", str(receipt_file))

    settings = BackgroundNotificationSettings.from_mapping(
        {"local_enabled": True, "local_backend": "desktop"}
    )
    store = BackgroundStore(tmp_path / "background")
    store.configure_notifications(settings)
    store.create(
        BackgroundSession.create(
            "e2e-notification-session",
            title="Sensitive session title",
            cwd=str(tmp_path / "example-workspace"),
            workflow_name="goal_flow",
            intent="sensitive prompt must not be in notice",
        ).evolve(status=SessionStatus.RUNNING)
    )
    store.transition("e2e-notification-session", SessionStatus.COMPLETED)

    outbox = BackgroundNotificationOutbox(store.root)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        row = outbox.list_deliveries()[0]
        if row["status"] in {"delivered", "failed"}:
            break
        time.sleep(0.05)
    else:
        pytest.fail("background notification dispatcher did not finish")

    assert row["status"] == "delivered"
    assert receipt_file.is_file()
    notice_args = json.loads(receipt_file.read_text(encoding="utf-8"))
    rendered = json.dumps(notice_args)
    assert "example-workspace" in rendered
    assert "Background run completed." in rendered
    assert "Sensitive session title" not in rendered
    assert "sensitive prompt" not in rendered
    assert store.get("e2e-notification-session").status is SessionStatus.COMPLETED
