

class TestCLINotify:
    def test_notify_dry_run(self) -> None:
        """Notify with --dry-run should succeed and show summary."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            vault_path = f.name
        Path(vault_path).unlink(missing_ok=True)
        Path(vault_path + ".salt").unlink(missing_ok=True)

        try:
            # Create vault
            result = _run_cli("vault", "create", vault_path, "--password", "pw")
            assert result.returncode == 0

            # Add an entry
            result = _run_cli(
                "vault", "add", vault_path, "--password", "pw",
                "--service", "Test", "--username", "user",
                "--pass", "secret123",
            )
            assert result.returncode == 0

            # Run notify dry-run
            result = _run_cli("notify", "--vault-path", vault_path,
                              "--master-password", "pw", "--dry-run")
            assert result.returncode == 0
            assert "Warnings sent:" in result.stdout
            assert "Notifications sent:" in result.stdout
            assert "(dry run" in result.stdout or "dry run" in result.stdout
        finally:
            Path(vault_path).unlink(missing_ok=True)
            Path(vault_path + ".salt").unlink(missing_ok=True)

    def test_notify_with_policy(self) -> None:
        """Notify with policy args should accept --max-age-days and --warning-days."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            vault_path = f.name
        Path(vault_path).unlink(missing_ok=True)
        Path(vault_path + ".salt").unlink(missing_ok=True)

        try:
            _run_cli("vault", "create", vault_path, "--password", "pw")
            _run_cli("vault", "add", vault_path, "--password", "pw",
                     "--service", "Test", "--username", "user",
                     "--pass", "secret123")

            result = _run_cli("notify", "--vault-path", vault_path,
                              "--master-password", "pw", "--dry-run",
                              "--max-age-days", "90", "--warning-days", "14")
            assert result.returncode == 0
            assert "Warnings sent:" in result.stdout
        finally:
            Path(vault_path).unlink(missing_ok=True)
            Path(vault_path + ".salt").unlink(missing_ok=True)

    def test_notify_invalid_vault(self) -> None:
        """Notify with non-existent vault should fail gracefully."""
        result = _run_cli("notify", "--vault-path", "/nonexistent/vault.json",
                          "--master-password", "pw")
        assert result.returncode != 0

    def test_notify_help(self) -> None:
        """Notify --help should show options."""
        result = _run_cli("notify", "--help")
        assert result.returncode == 0
        assert "--vault-path" in result.stdout
        assert "--dry-run" in result.stdout
        assert "--max-age-days" in result.stdout
        assert "--warning-days" in result.stdout


class TestCLINotify:
    def test_notify_dry_run(self) -> None:
        """Notify with --dry-run should succeed and show summary."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            vault_path = f.name
        Path(vault_path).unlink(missing_ok=True)
        Path(vault_path + ".salt").unlink(missing_ok=True)

        try:
            # Create vault
            result = _run_cli("vault", "create", vault_path, "--password", "pw")
            assert result.returncode == 0

            # Add an entry
            result = _run_cli(
                "vault", "add", vault_path, "--password", "pw",
                "--service", "Test", "--username", "user",
                "--pass", "secret123",
            )
            assert result.returncode == 0

            # Run notify dry-run
            result = _run_cli("notify", "--vault-path", vault_path,
                              "--master-password", "pw", "--dry-run")
            assert result.returncode == 0
            assert "Warnings sent:" in result.stdout
            assert "Notifications sent:" in result.stdout
            assert "(dry run" in result.stdout or "dry run" in result.stdout
        finally:
            Path(vault_path).unlink(missing_ok=True)
            Path(vault_path + ".salt").unlink(missing_ok=True)

    def test_notify_with_policy(self) -> None:
        """Notify with policy args should accept --max-age-days and --warning-days."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            vault_path = f.name
        Path(vault_path).unlink(missing_ok=True)
        Path(vault_path + ".salt").unlink(missing_ok=True)

        try:
            _run_cli("vault", "create", vault_path, "--password", "pw")
            _run_cli("vault", "add", vault_path, "--password", "pw",
                     "--service", "Test", "--username", "user",
                     "--pass", "secret123")

            result = _run_cli("notify", "--vault-path", vault_path,
                              "--master-password", "pw", "--dry-run",
                              "--max-age-days", "90", "--warning-days", "14")
            assert result.returncode == 0
            assert "Warnings sent:" in result.stdout
        finally:
            Path(vault_path).unlink(missing_ok=True)
            Path(vault_path + ".salt").unlink(missing_ok=True)

    def test_notify_invalid_vault(self) -> None:
        """Notify with non-existent vault should fail gracefully."""
        result = _run_cli("notify", "--vault-path", "/nonexistent/vault.json",
                          "--master-password", "pw")
        assert result.returncode != 0

    def test_notify_help(self) -> None:
        """Notify --help should show options."""
        result = _run_cli("notify", "--help")
        assert result.returncode == 0
        assert "--vault-path" in result.stdout
        assert "--dry-run" in result.stdout
        assert "--max-age-days" in result.stdout
        assert "--warning-days" in result.stdout


class TestCLINotify:
    def test_notify_dry_run(self) -> None:
        """Notify with --dry-run should succeed and show summary."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            vault_path = f.name
        Path(vault_path).unlink(missing_ok=True)
        Path(vault_path + ".salt").unlink(missing_ok=True)

        try:
            # Create vault
            result = _run_cli("vault", "create", vault_path, "--password", "pw")
            assert result.returncode == 0

            # Add an entry
            result = _run_cli(
                "vault", "add", vault_path, "--password", "pw",
                "--service", "Test", "--username", "user",
                "--pass", "secret123",
            )
            assert result.returncode == 0

            # Run notify dry-run
            result = _run_cli("notify", "--vault-path", vault_path,
                              "--master-password", "pw", "--dry-run")
            assert result.returncode == 0
            assert "Warnings sent:" in result.stdout
            assert "Notifications sent:" in result.stdout
            assert "(dry run" in result.stdout or "dry run" in result.stdout
        finally:
            Path(vault_path).unlink(missing_ok=True)
            Path(vault_path + ".salt").unlink(missing_ok=True)

    def test_notify_with_policy(self) -> None:
        """Notify with policy args should accept --max-age-days and --warning-days."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            vault_path = f.name
        Path(vault_path).unlink(missing_ok=True)
        Path(vault_path + ".salt").unlink(missing_ok=True)

        try:
            _run_cli("vault", "create", vault_path, "--password", "pw")
            _run_cli("vault", "add", vault_path, "--password", "pw",
                     "--service", "Test", "--username", "user",
                     "--pass", "secret123")

            result = _run_cli("notify", "--vault-path", vault_path,
                              "--master-password", "pw", "--dry-run",
                              "--max-age-days", "90", "--warning-days", "14")
            assert result.returncode == 0
            assert "Warnings sent:" in result.stdout
        finally:
            Path(vault_path).unlink(missing_ok=True)
            Path(vault_path + ".salt").unlink(missing_ok=True)

    def test_notify_invalid_vault(self) -> None:
        """Notify with non-existent vault should fail gracefully."""
        result = _run_cli("notify", "--vault-path", "/nonexistent/vault.json",
                          "--master-password", "pw")
        assert result.returncode != 0

    def test_notify_help(self) -> None:
        """Notify --help should show options."""
        result = _run_cli("notify", "--help")
        assert result.returncode == 0
        assert "--vault-path" in result.stdout
        assert "--dry-run" in result.stdout
        assert "--max-age-days" in result.stdout
        assert "--warning-days" in result.stdout
