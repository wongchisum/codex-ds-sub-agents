from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))

import adapter_service as service  # noqa: E402
from model_manifest import load_manifest  # noqa: E402


class AdapterServiceTests(unittest.TestCase):
    class _HealthResponse:
        def __init__(self, payload: object, status: int = 200) -> None:
            self.body = json.dumps(payload).encode("utf-8")
            self.status = status

        def __enter__(self) -> "AdapterServiceTests._HealthResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            return self.body[:size]

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, duration: float) -> None:
            self.now += duration

    def test_gemini_service_is_persistent_and_audited(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            plist = plistlib.loads(service.render_plist(spec))
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual({"SuccessfulExit": False}, plist["KeepAlive"])
        self.assertIn("--audit-log", plist["ProgramArguments"])
        self.assertIn("--max-output-tokens", plist["ProgramArguments"])
        service_id_index = plist["ProgramArguments"].index("--service-id")
        self.assertEqual("claudecode_gemini", plist["ProgramArguments"][service_id_index + 1])
        self.assertEqual("http://127.0.0.1:18768/health", spec.health_url)
        self.assertEqual(
            service.service_fingerprint(
                "claudecode_gemini",
                "https://api.claudecode.net.cn/api/gemini",
            ),
            spec.fingerprint,
        )

    def test_health_requires_exact_service_identity(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        spec = service.service_specs(manifest, Path("/tmp/codex"), Path("/tmp/agents"))[0]
        payload = {
            "status": "ok",
            "adapter": "anthropic_messages",
            "service_id": spec.provider_id,
            "fingerprint": spec.fingerprint,
        }
        ok, detail = service.check_health_url(
            spec.health_url,
            spec.provider_id,
            spec.fingerprint,
            opener=lambda request, timeout: self._HealthResponse(payload),
        )
        self.assertTrue(ok, detail)

    def test_health_rejects_old_or_wrong_service_on_expected_port(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        spec = service.service_specs(manifest, Path("/tmp/codex"), Path("/tmp/agents"))[0]
        for payload in (
            {"status": "ok", "adapter": "anthropic_messages"},
            {
                "status": "ok",
                "adapter": "anthropic_messages",
                "service_id": "another_provider",
                "fingerprint": spec.fingerprint,
            },
            {
                "status": "ok",
                "adapter": "anthropic_messages",
                "service_id": spec.provider_id,
                "fingerprint": "sha256:" + "0" * 64,
            },
        ):
            with self.subTest(payload=payload):
                ok, detail = service.check_health_url(
                    spec.health_url,
                    spec.provider_id,
                    spec.fingerprint,
                    opener=lambda request, timeout, value=payload: self._HealthResponse(value),
                )
                self.assertFalse(ok)
                self.assertIn("identity mismatch", detail)

    def test_startup_health_poll_retries_until_exact_service_is_ready(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        spec = service.service_specs(manifest, Path("/tmp/codex"), Path("/tmp/agents"))[0]
        clock = self._Clock()
        results = iter(((False, "connection refused"), (False, "identity mismatch"), (True, "ok")))
        probe_timeouts = []

        def probe(current: service.ServiceSpec, timeout: float) -> tuple[bool, str]:
            self.assertIs(spec, current)
            probe_timeouts.append(timeout)
            return next(results)

        service.wait_for_health(
            spec,
            timeout=1.0,
            interval=0.2,
            probe=probe,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )
        self.assertEqual(3, len(probe_timeouts))
        self.assertAlmostEqual(0.4, clock.now)

    def test_startup_health_poll_has_a_hard_deadline(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        spec = service.service_specs(manifest, Path("/tmp/codex"), Path("/tmp/agents"))[0]
        clock = self._Clock()
        probe_timeouts = []

        def probe(current: service.ServiceSpec, timeout: float) -> tuple[bool, str]:
            probe_timeouts.append(timeout)
            return False, "wrong service"

        with self.assertRaisesRegex(service.ServiceError, "did not become healthy"):
            service.wait_for_health(
                spec,
                timeout=0.5,
                interval=0.2,
                probe=probe,
                clock=clock.monotonic,
                sleeper=clock.sleep,
            )
        self.assertAlmostEqual(0.5, clock.now)
        self.assertTrue(probe_timeouts)
        self.assertLessEqual(max(probe_timeouts), 0.5)

    def test_install_waits_for_verified_health_after_kickstart(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            results = [
                subprocess.CompletedProcess([], 3, "", "not loaded"),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            with patch.object(service, "launchctl", side_effect=results), patch.object(
                service, "wait_for_health"
            ) as wait:
                service.install_and_start(spec, 501)
        wait.assert_called_once_with(spec)

    def test_failed_health_rolls_back_job_for_port_conflict_or_wrong_identity(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        for detail in (
            "port is occupied by a non-adapter process",
            "health endpoint identity mismatch",
        ):
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
                results = [
                    subprocess.CompletedProcess([], 3, "", "not loaded"),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ]
                with patch.object(service, "launchctl", side_effect=results) as launchctl, patch.object(
                    service,
                    "wait_for_health",
                    side_effect=service.ServiceError(detail),
                ):
                    with self.assertRaisesRegex(service.ServiceError, detail):
                        service.install_and_start(spec, 501)
                self.assertFalse(spec.plist_path.exists())
                self.assertEqual(
                    ("bootout", f"gui/501/{spec.label}"),
                    launchctl.call_args_list[-1].args[0],
                )

    def test_failed_health_keeps_plist_when_job_cleanup_fails(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            results = [
                subprocess.CompletedProcess([], 3, "", "not loaded"),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 5, "", "bootout failed"),
            ]
            with patch.object(service, "launchctl", side_effect=results), patch.object(
                service,
                "wait_for_health",
                side_effect=service.ServiceError("identity mismatch"),
            ):
                with self.assertRaisesRegex(service.ServiceError, "rollback failed: bootout failed"):
                    service.install_and_start(spec, 501)
            self.assertEqual(service.render_plist(spec), spec.plist_path.read_bytes())

    def test_start_never_overwrites_or_stops_modified_user_service(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            spec.plist_path.parent.mkdir(parents=True)
            spec.plist_path.write_text("user managed", encoding="utf-8")
            with patch.object(service, "launchctl") as launchctl:
                with self.assertRaisesRegex(service.ServiceError, "modified service definition"):
                    service.install_and_start(spec, 501)
            self.assertEqual("user managed", spec.plist_path.read_text(encoding="utf-8"))
        launchctl.assert_not_called()

    def test_start_never_stops_loaded_job_without_owned_plist(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            loaded = subprocess.CompletedProcess([], 0, "loaded", "")
            with patch.object(service, "launchctl", return_value=loaded) as launchctl:
                with self.assertRaisesRegex(service.ServiceError, "without a verifiable managed plist"):
                    service.install_and_start(spec, 501)
            self.assertFalse(spec.plist_path.exists())
        launchctl.assert_called_once_with(("print", f"gui/501/{spec.label}"))

    def test_deepseek_anthropic_profile_has_service(self) -> None:
        manifest = load_manifest(ROOT / "config" / "deepseek-anthropic-1m.example.json")
        self.assertTrue(service.service_specs(manifest, Path("/tmp/codex"), Path("/tmp/agents")))

    def test_provider_filter_selects_only_requested_adapter(self) -> None:
        manifest = load_manifest(ROOT / "config" / "model-providers.example.json")
        specs = service.service_specs(manifest, Path("/tmp/codex"), Path("/tmp/agents"))
        selected = service.filter_service_specs(specs, ["aicodemirror_claude"])
        self.assertEqual(["aicodemirror_claude"], [spec.provider_id for spec in selected])
        with self.assertRaisesRegex(service.ServiceError, "no adapter provider: missing"):
            service.filter_service_specs(specs, ["missing"])

    def test_existing_loaded_job_is_restored_when_replacement_bootstrap_fails(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            spec.plist_path.parent.mkdir(parents=True)
            spec.plist_path.write_bytes(service.render_plist(spec))
            results = [
                subprocess.CompletedProcess([], 0, "loaded", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 5, "", "replacement failed"),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            with patch.object(service, "launchctl", side_effect=results) as launchctl:
                with self.assertRaisesRegex(service.ServiceError, "replacement failed"):
                    service.install_and_start(spec, 501)
            self.assertEqual(service.render_plist(spec), spec.plist_path.read_bytes())
        self.assertEqual(
            [
                ("print", f"gui/501/{spec.label}"),
                ("bootout", f"gui/501/{spec.label}"),
                ("bootstrap", "gui/501", str(spec.plist_path)),
                ("bootstrap", "gui/501", str(spec.plist_path)),
            ],
            [call.args[0] for call in launchctl.call_args_list],
        )

    def test_restore_failure_reports_both_errors_and_keeps_managed_plist(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            expected = service.render_plist(spec)
            spec.plist_path.parent.mkdir(parents=True)
            spec.plist_path.write_bytes(expected)
            results = [
                subprocess.CompletedProcess([], 0, "loaded", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 5, "", "replacement failed"),
                subprocess.CompletedProcess([], 6, "", "restore failed"),
            ]
            with patch.object(service, "launchctl", side_effect=results):
                with self.assertRaisesRegex(
                    service.ServiceError,
                    "replacement failed; restoring previous job failed: restore failed",
                ):
                    service.install_and_start(spec, 501)
            self.assertEqual(expected, spec.plist_path.read_bytes())

    def test_stopping_missing_service_is_idempotent(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            missing = subprocess.CompletedProcess([], 3, "", "not found")
            with patch.object(service, "launchctl", return_value=missing) as launchctl:
                service.stop_and_remove(spec, 501)
        launchctl.assert_called_once_with(("print", f"gui/501/{spec.label}"))

    def test_missing_plist_never_stops_loaded_unverifiable_job(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            loaded = subprocess.CompletedProcess([], 0, "loaded", "")
            with patch.object(service, "launchctl", return_value=loaded) as launchctl:
                with self.assertRaisesRegex(service.ServiceError, "unverifiable job"):
                    service.stop_and_remove(spec, 501)
        launchctl.assert_called_once_with(("print", f"gui/501/{spec.label}"))

    def test_modified_service_is_never_stopped_or_removed(self) -> None:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = service.service_specs(manifest, root / "codex", root / "agents")[0]
            spec.plist_path.parent.mkdir(parents=True)
            spec.plist_path.write_text("user managed", encoding="utf-8")
            with patch.object(service, "launchctl") as launchctl:
                with self.assertRaisesRegex(service.ServiceError, "modified service"):
                    service.stop_and_remove(spec, 501)
            self.assertTrue(spec.plist_path.exists())
        launchctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class WindowsAdapterServiceTests(unittest.TestCase):
    """Windows backend tests: all platform dispatch is mocked, never executed."""

    def _manifest_spec(self, root: Path) -> service.ServiceSpec:
        manifest = load_manifest(ROOT / "config" / "gemini-anthropic.example.json")
        with patch.dict(
            "os.environ", {"LOCALAPPDATA": str(root / "localappdata")}
        ):
            return service.service_specs(manifest, root / "codex", root / "agents")[0]

    def test_windows_spec_uses_localappdata_xml_and_current_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("platform_runtime.is_windows", return_value=True), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(root / "localappdata")}
            ):
                spec = self._manifest_spec(root)
            self.assertEqual(
                root / "localappdata" / "Codex" / "SubagentAdapters" / f"{spec.label}.xml",
                spec.plist_path,
            )
            self.assertEqual(spec.label, spec.task_name)
            self.assertEqual(sys.executable, spec.arguments[0])
            self.assertTrue(spec.arguments[1].endswith("service_runner.py"))
            self.assertIn("--stdout-log", spec.arguments)
            self.assertIn("--stderr-log", spec.arguments)
            self.assertTrue(spec.definition_path.name.endswith(".xml"))

    def test_windows_task_xml_quotes_command_and_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("platform_runtime.is_windows", return_value=True):
                spec = self._manifest_spec(root)
            xml = service.windows_task_xml_text(spec)
        self.assertIn(f"<Command>{sys.executable}</Command>", xml)
        self.assertIn("<Arguments>", xml)
        self.assertIn("--service-id", xml)
        self.assertIn("--audit-log", xml)
        self.assertIn("<WorkingDirectory>", xml)
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("LeastPrivilege", xml)
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="UTF-16"?>'))

    def test_windows_definition_bytes_are_utf16(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("platform_runtime.is_windows", return_value=True):
                spec = self._manifest_spec(root)
            payload = service.windows_task_xml(spec)
        self.assertTrue(payload.startswith(b"\xff\xfe") or payload.startswith(b"\xfe\xff"))

    def test_windows_install_creates_task_and_waits_for_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = {
                "query": subprocess.CompletedProcess([], 1, "", "not found"),
                "create": subprocess.CompletedProcess([], 0, "", ""),
                "run": subprocess.CompletedProcess([], 0, "", ""),
            }
            with patch("platform_runtime.is_windows", return_value=True):
                spec = self._manifest_spec(root)
                with patch.object(
                    service, "schtasks", side_effect=lambda args: results[args[0].lower()]
                ) as schtasks, patch.object(service, "wait_for_health") as wait:
                    service.install_and_start(spec, None)
            wait.assert_called_once_with(spec)
            self.assertEqual(
                [
                    ("Query", "/TN", spec.label, "/FO", "LIST"),
                    ("Create", "/TN", spec.label, "/XML", str(spec.plist_path), "/F"),
                    ("Run", "/TN", spec.label),
                ],
                [call.args[0] for call in schtasks.call_args_list],
            )
            self.assertTrue(spec.plist_path.is_file())

    def test_windows_install_refuses_existing_task_without_managed_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = subprocess.CompletedProcess([], 0, "running", "")
            with patch("platform_runtime.is_windows", return_value=True):
                spec = self._manifest_spec(root)
                with patch.object(service, "schtasks", return_value=loaded) as schtasks:
                    with self.assertRaisesRegex(service.ServiceError, "without a verifiable managed definition"):
                        service.install_and_start(spec, None)
            self.assertEqual(
                ("Query", "/TN", spec.label, "/FO", "LIST"),
                schtasks.call_args.args[0],
            )

    def test_windows_stop_ends_deletes_and_removes_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = {
                "query": subprocess.CompletedProcess([], 0, "running", ""),
                "end": subprocess.CompletedProcess([], 0, "", ""),
                "delete": subprocess.CompletedProcess([], 0, "", ""),
            }
            with patch("platform_runtime.is_windows", return_value=True):
                spec = self._manifest_spec(root)
                spec.plist_path.parent.mkdir(parents=True)
                spec.plist_path.write_bytes(service.windows_task_xml(spec))
                with patch.object(
                    service, "schtasks", side_effect=lambda args: results[args[0].lower()]
                ) as schtasks:
                    service.stop_and_remove(spec, None)
            self.assertFalse(spec.plist_path.exists())
            self.assertEqual(
                [
                    ("Query", "/TN", spec.label, "/FO", "LIST"),
                    ("End", "/TN", spec.label),
                    ("Delete", "/TN", spec.label, "/F"),
                ],
                [call.args[0] for call in schtasks.call_args_list],
            )

    def test_windows_render_writes_definition_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("platform_runtime.is_windows", return_value=True):
                spec = self._manifest_spec(root)
                service.atomic_write(spec.plist_path, service.render_definition(spec))
                self.assertTrue(spec.plist_path.read_bytes().startswith(b"\xff\xfe"))
