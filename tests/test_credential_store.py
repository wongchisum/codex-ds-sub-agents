from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import credential_store  # noqa: E402


class CredentialStoreTests(unittest.TestCase):
    def test_windows_target_is_namespaced_and_contains_no_secret(self) -> None:
        identity = credential_store.validate_identity("provider-key", "codex")
        self.assertEqual(
            "codex-custom-subagents:provider-key:codex",
            identity.windows_target,
        )

    def test_identity_rejects_control_characters(self) -> None:
        with self.assertRaisesRegex(credential_store.CredentialError, "control"):
            credential_store.validate_identity("provider\nkey", "codex")

    def test_macos_read_uses_argument_array_and_returns_only_stdout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["security"], returncode=0, stdout="secret-value\n", stderr=""
        )
        with patch.object(Path, "is_file", return_value=True):
            value = credential_store.macos_read(
                credential_store.CredentialIdentity("provider-key", "codex"),
                runner=lambda *args, **kwargs: completed,
            )
        self.assertEqual("secret-value", value)

    def test_unsupported_platform_requires_environment_reference(self) -> None:
        identity = credential_store.CredentialIdentity("provider-key", "codex")
        with self.assertRaisesRegex(credential_store.CredentialError, "environment"):
            credential_store.read_credential(identity, system="Linux")

    def test_cli_exists_does_not_print_or_read_secret_in_test_process(self) -> None:
        with patch.object(credential_store, "credential_exists", return_value=True):
            self.assertEqual(
                0,
                credential_store.main(
                    ["exists", "--service", "provider-key", "--account", "codex"]
                ),
            )

    def test_non_windows_set_is_rejected_without_prompting(self) -> None:
        with patch.object(credential_store.platform, "system", return_value="Darwin"), patch.object(
            credential_store.getpass, "getpass"
        ) as prompt:
            result = credential_store.main(["set", "--service", "provider-key"])
        self.assertEqual(2, result)
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
