from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import doctor  # noqa: E402


class DoctorUnitTests(unittest.TestCase):
    def test_keychain_check_times_out(self) -> None:
        with mock.patch.object(
            doctor.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 5)
        ):
            ok, detail = doctor.check_keychain(Path("/usr/bin/security"), 5.0)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_keychain_check_missing_password(self) -> None:
        failed = subprocess.CompletedProcess(["security"], 44, stdout="", stderr="")
        with mock.patch.object(doctor.subprocess, "run", return_value=failed):
            ok, detail = doctor.check_keychain(Path("/usr/bin/security"), 5.0)
        self.assertFalse(ok)
        self.assertIn("not found", detail)

    def test_keychain_check_found(self) -> None:
        passed = subprocess.CompletedProcess(["security"], 0, stdout="", stderr="")
        with mock.patch.object(doctor.subprocess, "run", return_value=passed):
            ok, detail = doctor.check_keychain(Path("/usr/bin/security"), 5.0)
        self.assertTrue(ok)
        self.assertIn("exists", detail)

    def test_strict_config_failure_classified_as_user_config(self) -> None:
        codex_home = self._temp_home()
        failed = subprocess.CompletedProcess(["codex"], 1, stdout="bad user config", stderr="")
        passed = subprocess.CompletedProcess(["codex"], 0, stdout="ok", stderr="")
        with mock.patch.object(doctor.subprocess, "run", side_effect=[failed, passed]):
            ok, detail = doctor.check_strict_config("/bin/true", codex_home, PROJECT_ROOT, 30.0)
        self.assertFalse(ok)
        self.assertIn("user config", detail)

    def test_strict_config_failure_classified_as_install_issue(self) -> None:
        codex_home = self._temp_home()
        first = subprocess.CompletedProcess(["codex"], 1, stdout="config broken", stderr="")
        second = subprocess.CompletedProcess(["codex"], 1, stdout="still broken", stderr="")
        with mock.patch.object(doctor.subprocess, "run", side_effect=[first, second]):
            ok, detail = doctor.check_strict_config("/bin/true", codex_home, PROJECT_ROOT, 30.0)
        self.assertFalse(ok)
        self.assertIn("installation", detail)

    def test_strict_config_timeout(self) -> None:
        codex_home = self._temp_home()
        with mock.patch.object(
            doctor.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 30)
        ):
            ok, detail = doctor.check_strict_config("/bin/true", codex_home, PROJECT_ROOT, 30.0)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    @staticmethod
    def _temp_home() -> Path:
        directory = tempfile.mkdtemp()
        home = Path(directory) / "home"
        home.mkdir()
        return home


if __name__ == "__main__":
    unittest.main()
