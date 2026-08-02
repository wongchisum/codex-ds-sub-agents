from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import configure  # noqa: E402
from model_manifest import load_manifest  # noqa: E402


class ConfigureTests(unittest.TestCase):
    def test_protocol_catalog_is_not_model_specific(self) -> None:
        self.assertEqual(
            {"openai_responses", "anthropic_messages"},
            {protocol.name for protocol in configure.PROTOCOLS},
        )
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, configure.main(["--list-protocols"]))
        self.assertNotIn("gemini", output.getvalue().lower())
        self.assertNotIn("deepseek", output.getvalue().lower())

    def test_profiles_cover_primary_examples_and_legacy(self) -> None:
        self.assertEqual(
            {
                "deepseek-anthropic",
                "gemini-anthropic",
                "claude-gemini",
                "legacy-deepseek",
            },
            set(configure.PROFILE_BY_NAME),
        )
        for profile in configure.PROFILES:
            if profile.manifest is not None:
                self.assertTrue(profile.manifest.is_file())
                load_manifest(profile.manifest)

    def test_model_protocol_catalog_lists_four_canonical_choices(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, configure.main(["--list-model-protocols"]))
        rendered = output.getvalue().lower()
        for label in (
            "deepseek (anthropic)",
            "deepseek (openai)",
            "gemini (anthropic)",
            "claude (anthropic)",
        ):
            self.assertIn(label, rendered)
        self.assertIn("fallbacks are configured separately", rendered)

    def test_primary_preset_has_no_implicit_fallback(self) -> None:
        document = json.loads(
            configure.build_preset_manifest("deepseek-anthropic", ())
        )
        self.assertEqual("deepseek-v4-flash", document["selection"]["primary"])
        self.assertEqual([], document["selection"]["fallbacks"])
        self.assertEqual(0, document["selection"]["max_switches"])

    def test_fallback_preserves_declared_order(self) -> None:
        document = json.loads(
            configure.build_preset_manifest(
                "claude-anthropic",
                ("gemini-anthropic", "deepseek-openai"),
            )
        )
        self.assertEqual(
            ["gemini-3-5-flash", "deepseek-v4-flash-openai"],
            document["selection"]["fallbacks"],
        )
        self.assertEqual(2, document["selection"]["max_switches"])

    def test_duplicate_primary_or_fallback_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            configure.build_preset_manifest(
                "gemini-anthropic", ("deepseek-openai", "gemini-anthropic")
            )

    def test_legacy_profile_remains_accepted(self) -> None:
        args = configure.parse_args(["--profile", "legacy-deepseek"])
        self.assertEqual("legacy-deepseek", args.profile)

    def test_custom_manifest_is_copied_to_stable_private_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            source = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"
            destination, manifest = configure.install_managed_manifest(
                source, codex_home, "team.gemini"
            )
            self.assertEqual(
                codex_home / "custom-subagents" / "manifests" / "team-gemini.json",
                destination,
            )
            self.assertEqual(source.read_bytes(), destination.read_bytes())
            self.assertEqual(0o600, destination.stat().st_mode & 0o777)
            self.assertEqual("gemini-3-5-flash", manifest.selection.primary.id)

    def test_changed_managed_manifest_requires_new_name_or_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            first = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"
            second = PROJECT_ROOT / "config" / "deepseek-anthropic-1m.example.json"
            destination, _manifest = configure.install_managed_manifest(
                first, codex_home, "team"
            )
            original = destination.read_bytes()
            with self.assertRaisesRegex(ValueError, "use a new --name"):
                configure.install_managed_manifest(second, codex_home, "team")
            self.assertEqual(original, destination.read_bytes())

    def test_managed_manifest_never_writes_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            destination = configure.managed_manifest_path(codex_home, "team")
            destination.parent.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text("outside", encoding="utf-8")
            destination.symlink_to(outside)
            source = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"
            with self.assertRaisesRegex(ValueError, "regular file"):
                configure.install_managed_manifest(source, codex_home, "team")
            self.assertEqual("outside", outside.read_text(encoding="utf-8"))

    def test_missing_credentials_deduplicates_shared_keychain_service(self) -> None:
        manifest = load_manifest(PROJECT_ROOT / "config" / "model-providers.example.json")
        missing = configure.missing_credentials(
            manifest,
            keychain_check=lambda _account, _service: False,
        )
        self.assertEqual(
            (configure.MissingCredential("keychain", "aicodemirror-api-key", "codex"),),
            missing,
        )

    def test_environment_credentials_are_checked_without_reading_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.json"
            raw = json.loads(
                (PROJECT_ROOT / "config" / "gemini-anthropic.example.json").read_text(
                    encoding="utf-8"
                )
            )

            raw["providers"][0]["auth"] = {
                "type": "env",
                "variable": "CUSTOM_SUBAGENT_TOKEN",
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            manifest = load_manifest(path)
            self.assertEqual(
                (configure.MissingCredential("env", "CUSTOM_SUBAGENT_TOKEN", "codex"),),
                configure.missing_credentials(manifest, environ={}),
            )
            self.assertEqual(
                (),
                configure.missing_credentials(
                    manifest, environ={"CUSTOM_SUBAGENT_TOKEN": "present"}
                ),
            )

    def test_windows_credential_instruction_uses_interactive_helper(self) -> None:
        output = StringIO()
        with patch.object(
            configure.platform, "system", return_value="Windows"
        ), redirect_stdout(output):
            configure.print_credential_instructions(
                (configure.MissingCredential("keychain", "provider-key", "codex"),)
            )
        rendered = output.getvalue()
        self.assertIn("credential_store.py", rendered)
        self.assertIn(" set ", rendered)
        self.assertNotIn("-w ", rendered)

    def test_install_and_doctor_commands_share_managed_manifest(self) -> None:
        home = Path("/tmp/codex-home")
        manifest = home / "custom-subagents" / "manifests" / "team.json"
        install = configure.install_command(home, manifest, True)
        doctor = configure.doctor_command(
            home,
            manifest,
            skip_credential_check=True,
            skip_adapter_health=True,
        )
        self.assertIn(str(manifest), install)
        self.assertIn("--no-start-adapters", install)
        self.assertIn(str(manifest), doctor)
        self.assertIn("--skip-keychain", doctor)
        self.assertIn("--skip-adapter-health", doctor)

    def test_command_log_is_redacted_and_records_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "configure.jsonl"
            completed = subprocess.CompletedProcess(
                ["python", "install.py"],
                7,
                stdout="api_key=sk-example-secret\n",
                stderr="failed\n",
            )
            with redirect_stdout(StringIO()):
                result = configure.run_command(
                    ["python", "install.py"],
                    runner=lambda *args, **kwargs: completed,
                    log_path=log,
                    phase="install",
                )
            self.assertEqual(7, result)
            content = log.read_text(encoding="utf-8")
            self.assertIn('"exit_code": 7', content)
            self.assertIn("<redacted>", content)
            self.assertNotIn("sk-example-secret", content)

    def test_main_stops_before_install_when_credentials_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> int:
                calls.append(command)
                return 0

            with patch.object(configure, "run_command", side_effect=fake_run), patch.object(
                configure,
                "missing_credentials",
                return_value=(
                    configure.MissingCredential(
                        "keychain", "deepseek-api-key", "codex"
                    ),
                ),
            ):
                result = configure.main(
                    [
                        "--profile",
                        "deepseek-anthropic",
                        "--codex-home",
                        str(Path(directory) / "codex-home"),
                    ]
                )

            self.assertEqual(configure.NEEDS_CREDENTIAL_EXIT, result)
            self.assertEqual([], calls, "installation must not start before credentials exist")

    def test_main_runs_doctor_after_successful_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> int:
                calls.append(command)
                return 0

            with patch.object(configure, "run_command", side_effect=fake_run), patch.object(
                configure, "missing_credentials", return_value=()
            ):
                result = configure.main(
                    [
                        "--profile",
                        "gemini-anthropic",
                        "--codex-home",
                        str(Path(directory) / "codex-home"),
                    ]
                )

            self.assertEqual(0, result)
            self.assertEqual(2, len(calls))
            self.assertIn("install.py", calls[0][1])
            self.assertIn("doctor.py", calls[1][1])

    def test_install_failure_is_returned_without_running_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(configure, "run_command", return_value=7) as run, patch.object(
                configure, "missing_credentials", return_value=()
            ):
                result = configure.main(
                    [
                        "--profile",
                        "gemini-anthropic",
                        "--codex-home",
                        str(Path(directory) / "codex-home"),
                    ]
                )
            self.assertEqual(7, result)
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
