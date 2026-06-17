"""Tests for password_generator.notifications."""

import os
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

import pytest
from password_generator import (
    Vault,
    VaultEntry,
    create_vault,
)
from password_generator.notifications import (
    send_expiry_warnings,
    send_expired_notifications,
    check_and_notify,
)
from password_generator.policy import PasswordPolicy


@pytest.fixture
def vault_path() -> str:
    """Create a temporary vault path."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    # Clean up
    Path(path).unlink(missing_ok=True)
    Path(path + ".salt").unlink(missing_ok=True)
    return path


@pytest.fixture
def vault_with_expiring(vault_path: str) -> Vault:
    """Create a vault with entries at various expiry stages."""
    vault = create_vault(vault_path, "pw")
    # Expiring within 7 days
    soon = (datetime.now() + timedelta(days=3)).isoformat()
    # Expiring far in the future
    far = (datetime.now() + timedelta(days=90)).isoformat()
    # Already expired
    past = (datetime.now() - timedelta(days=1)).isoformat()
    # No expiry
    vault.add_entry(VaultEntry("SoonService", "u1", "p1", expires_at=soon))
    vault.add_entry(VaultEntry("FarService", "u2", "p2", expires_at=far))
    vault.add_entry(VaultEntry("ExpiredService", "u3", "p3", expires_at=past))
    vault.add_entry(VaultEntry("NoExpiry", "u4", "p4"))
    return vault


@pytest.fixture
def vault_with_aged_entries(vault_path: str) -> Vault:
    """Create a vault where some entries have passwords older than policy max_age_days."""
    vault = create_vault(vault_path, "pw")
    # Entry with no explicit expires_at but old updated_at
    old_entry = VaultEntry("OldService", "u1", "p1")
    old_entry.updated_at = (datetime.now() - timedelta(days=100)).isoformat()
    vault.add_entry(old_entry)

    # Entry with explicit expiry (soon)
    soon = (datetime.now() + timedelta(days=3)).isoformat()
    vault.add_entry(VaultEntry("SoonService", "u2", "p2", expires_at=soon))

    # Entry with no expiry and recent updated_at (should not trigger)
    recent_entry = VaultEntry("RecentService", "u3", "p3")
    recent_entry.updated_at = (datetime.now() - timedelta(days=1)).isoformat()
    vault.add_entry(recent_entry)

    # Already expired entry
    past = (datetime.now() - timedelta(days=1)).isoformat()
    vault.add_entry(VaultEntry("ExpiredService", "u4", "p4", expires_at=past))

    return vault


class TestSendExpiryWarnings:
    def test_warnings_found(self, vault_with_expiring: Vault) -> None:
        warnings = send_expiry_warnings(vault_with_expiring, days_before=7, dry_run=True)
        assert len(warnings) == 1
        assert warnings[0]["service"] == "SoonService"
        days_left = int(warnings[0]["days_left"])
        assert 2 <= days_left <= 4  # Allow for timing variation

    def test_warnings_not_found(self, vault_with_expiring: Vault) -> None:
        warnings = send_expiry_warnings(vault_with_expiring, days_before=1, dry_run=True)
        assert len(warnings) == 0

    def test_warnings_no_recipient_logs_warning(self, vault_with_expiring: Vault) -> None:
        """Should not crash when no email is configured."""
        # Ensure NOTIFICATION_EMAIL is not set
        old = os.environ.pop("NOTIFICATION_EMAIL", None)
        try:
            warnings = send_expiry_warnings(vault_with_expiring, days_before=7)
            assert len(warnings) == 1
        finally:
            if old is not None:
                os.environ["NOTIFICATION_EMAIL"] = old

    def test_empty_vault(self, vault_path: str) -> None:
        vault = create_vault(vault_path, "pw")
        warnings = send_expiry_warnings(vault, days_before=7, dry_run=True)
        assert warnings == []

    def test_warnings_with_policy_uses_warning_days(self, vault_with_expiring: Vault) -> None:
        """Policy.expiration_warning_days should be used as the default days_before."""
        policy = PasswordPolicy(expiration_warning_days=7)
        warnings = send_expiry_warnings(vault_with_expiring, policy=policy, dry_run=True)
        assert len(warnings) == 1
        assert warnings[0]["service"] == "SoonService"

    def test_warnings_with_policy_detects_aged_entries(self, vault_with_aged_entries: Vault) -> None:
        """Entries older than max_age_days should be flagged even without explicit expiry."""
        policy = PasswordPolicy(max_age_days=90, expiration_warning_days=7)
        warnings = send_expiry_warnings(vault_with_aged_entries, policy=policy, dry_run=True)
        services = [w["service"] for w in warnings]
        assert "SoonService" in services  # Explicit expiry
        assert "OldService" in services   # Aged-out per policy
        # RecentService should NOT be flagged (only 1 day old)
        assert "RecentService" not in services

    def test_warnings_with_policy_no_policy_no_change(self, vault_with_expiring: Vault) -> None:
        """Without policy, behaviour should be unchanged."""
        warnings = send_expiry_warnings(vault_with_expiring, days_before=7, dry_run=True)
        assert len(warnings) == 1


class TestSendExpiredNotifications:
    def test_expired_found(self, vault_with_expiring: Vault) -> None:
        notifications = send_expired_notifications(vault_with_expiring, dry_run=True)
        assert len(notifications) == 1
        assert notifications[0]["service"] == "ExpiredService"

    def test_no_expired(self, vault_path: str) -> None:
        vault = create_vault(vault_path, "pw")
        future = (datetime.now() + timedelta(days=30)).isoformat()
        vault.add_entry(VaultEntry("Service", "u", "p", expires_at=future))
        notifications = send_expired_notifications(vault, dry_run=True)
        assert notifications == []

    def test_expired_no_recipient_logs_warning(self, vault_with_expiring: Vault) -> None:
        old = os.environ.pop("NOTIFICATION_EMAIL", None)
        try:
            notifications = send_expired_notifications(vault_with_expiring)
            assert len(notifications) == 1
        finally:
            if old is not None:
                os.environ["NOTIFICATION_EMAIL"] = old

    def test_expired_with_policy_detects_aged_out(self, vault_with_aged_entries: Vault) -> None:
        """Entries aged past max_age_days should appear in notifications."""
        policy = PasswordPolicy(max_age_days=90)
        notifications = send_expired_notifications(vault_with_aged_entries, policy=policy, dry_run=True)
        services = [n["service"] for n in notifications]
        assert "ExpiredService" in services  # Explicit expiry
        assert "OldService" in services       # Aged-out per policy

    def test_expired_with_policy_no_max_age_no_change(self, vault_with_expiring: Vault) -> None:
        """Without max_age_days, behaviour unchanged."""
        policy = PasswordPolicy()  # max_age_days=0
        notifications = send_expired_notifications(vault_with_expiring, policy=policy, dry_run=True)
        assert len(notifications) == 1
        assert notifications[0]["service"] == "ExpiredService"


class TestCheckAndNotify:
    def test_check_and_notify(self, vault_path: str) -> None:
        vault = create_vault(vault_path, "pw")
        soon = (datetime.now() + timedelta(days=3)).isoformat()
        past = (datetime.now() - timedelta(days=1)).isoformat()
        vault.add_entry(VaultEntry("Soon", "u1", "p1", expires_at=soon))
        vault.add_entry(VaultEntry("Expired", "u2", "p2", expires_at=past))

        result = check_and_notify(vault_path, "pw", days_before=7, dry_run=True)
        assert result["warnings_sent"] == 1
        assert result["notifications_sent"] == 1

    def test_check_and_notify_nothing(self, vault_path: str) -> None:
        vault = create_vault(vault_path, "pw")
        far = (datetime.now() + timedelta(days=90)).isoformat()
        vault.add_entry(VaultEntry("Far", "u", "p", expires_at=far))

        result = check_and_notify(vault_path, "pw", days_before=7, dry_run=True)
        assert result["warnings_sent"] == 0
        assert result["notifications_sent"] == 0

    def test_check_and_notify_with_policy(self, vault_path: str) -> None:
        """Full workflow with policy should detect both expiring and aged-out entries."""
        vault = create_vault(vault_path, "pw")
        soon = (datetime.now() + timedelta(days=3)).isoformat()
        past = (datetime.now() - timedelta(days=1)).isoformat()
        old_entry = VaultEntry("OldService", "u1", "p1")
        old_entry.updated_at = (datetime.now() - timedelta(days=100)).isoformat()
        vault.add_entry(old_entry)
        vault.add_entry(VaultEntry("Soon", "u2", "p2", expires_at=soon))
        vault.add_entry(VaultEntry("Expired", "u3", "p3", expires_at=past))

        policy = PasswordPolicy(max_age_days=90, expiration_warning_days=7)
        result = check_and_notify(vault_path, "pw", days_before=7, dry_run=True, policy=policy)
        # "Soon" (expires in 3 days) should generate a warning
        assert result["warnings_sent"] >= 1
        # "Expired" and/or "OldService" should generate notifications
        assert result["notifications_sent"] >= 1
