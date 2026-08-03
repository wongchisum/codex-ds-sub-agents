from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import doctor  # noqa: E402


class DoctorUnitTests(unittest.TestCase):
    SERVICE_ID = "claudecode_gemini"
    UPSTREAM_BASE_URL = "https://api.claudecode.net.cn/api/gemini"
    FINGERPRINT = doctor.service_fingerprint(SERVICE_ID, UPSTREAM_BASE_URL)

    class _HealthResponse:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self.body = body
            self.status = status

        def __enter__(self) -> "DoctorUnitTests._HealthResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            return self.body[:size]

    def test_optional_checks_are_enabled_by_default(self) -> None:
        with mock.patch.object(sys, "argv", ["doctor"]):
            args = doctor.parse_args()
        self.assertFalse(args.skip_adapter_health)
        self.assertFalse(args.skip_codex)

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

    def test_adapter_health_probes_health_endpoint(self) -> None:
        seen = {}

        def opener(request: object, timeout: float) -> DoctorUnitTests._HealthResponse:
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return self._HealthResponse(json.dumps({
                "status": "ok",
                "adapter": "anthropic_messages",
                "service_id": self.SERVICE_ID,
                "fingerprint": self.FINGERPRINT,
            }).encode("utf-8"))

        ok, detail = doctor.check_adapter_health(
            "http://127.0.0.1:18768",
            self.SERVICE_ID,
            self.FINGERPRINT,
            timeout=0.25,
            opener=opener,
        )
        self.assertTrue(ok)
        self.assertEqual("http://127.0.0.1:18768/health", seen["url"])
        self.assertEqual(0.25, seen["timeout"])
        self.assertIn("healthy", detail)

    def test_adapter_health_fails_when_service_is_not_running(self) -> None:
        def opener(request: object, timeout: float) -> object:
            raise urllib.error.URLError("connection refused")

        ok, detail = doctor.check_adapter_health(
            "http://127.0.0.1:18768",
            self.SERVICE_ID,
            self.FINGERPRINT,
            opener=opener,
        )
        self.assertFalse(ok)
        self.assertIn("connection refused", detail)

    def test_adapter_health_rejects_unexpected_payload(self) -> None:
        response = self._HealthResponse(json.dumps({
            "status": "ok",
            "adapter": "anthropic_messages",
            "service_id": "wrong_service",
            "fingerprint": self.FINGERPRINT,
        }).encode("utf-8"))
        ok, detail = doctor.check_adapter_health(
            "http://127.0.0.1:18768",
            self.SERVICE_ID,
            self.FINGERPRINT,
            opener=lambda request, timeout: response,
        )
        self.assertFalse(ok)
        self.assertIn("identity mismatch", detail)

    def test_custom_install_checks_configured_adapter_health(self) -> None:
        args = SimpleNamespace(
            manifest=PROJECT_ROOT / "config" / "gemini-anthropic.example.json",
            skip_keychain=True,
            skip_adapter_health=False,
            skip_codex=False,
        )
        with mock.patch.object(
            doctor,
            "check_adapter_health",
            return_value=(False, "adapter is stopped"),
        ) as health, mock.patch.object(
            doctor.shutil, "which", return_value=None
        ), mock.patch.object(
            doctor, "_run_codex", return_value=(0, "ok")
        ), redirect_stdout(StringIO()):
            result = doctor.check_custom_install(args, self._temp_home())
        self.assertEqual(1, result)
        health.assert_called_once_with(
            "http://127.0.0.1:18768",
            self.SERVICE_ID,
            self.FINGERPRINT,
        )

    def test_custom_install_can_explicitly_skip_optional_checks(self) -> None:
        args = SimpleNamespace(
            manifest=PROJECT_ROOT / "config" / "gemini-anthropic.example.json",
            skip_keychain=True,
            skip_adapter_health=True,
            skip_codex=True,
        )
        with mock.patch.object(doctor, "check_adapter_health") as health, mock.patch.object(
            doctor.shutil, "which"
        ) as codex_lookup, mock.patch.object(doctor, "_run_codex") as run_codex, redirect_stdout(
            StringIO()
        ):
            doctor.check_custom_install(args, self._temp_home())
        health.assert_not_called()
        codex_lookup.assert_not_called()
        run_codex.assert_not_called()

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
