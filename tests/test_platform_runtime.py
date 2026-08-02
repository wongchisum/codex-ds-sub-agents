from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import platform_runtime as runtime  # noqa: E402


class PythonCommandTests(unittest.TestCase):
    def test_python_command_uses_current_interpreter(self) -> None:
        self.assertEqual(Path(sys.executable).resolve(), Path(runtime.python_command()).resolve())

    def test_python_command_toml_is_quoted_and_toml_safe(self) -> None:
        rendered = runtime.python_command_toml()
        self.assertTrue(rendered.startswith('"'))
        self.assertTrue(rendered.endswith('"'))
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib requires Python 3.11+")
        parsed = tomllib.loads(f'text = """run {rendered} arg"""')
        self.assertIn("arg", parsed["text"])

    def test_toml_path_escape_doubles_windows_backslashes(self) -> None:
        self.assertEqual(r"C:\\Users\\a\\.codex", runtime.toml_path_escape(r"C:\Users\a\.codex"))
        self.assertEqual("/tmp/codex-home", runtime.toml_path_escape("/tmp/codex-home"))


class CodexDiscoveryTests(unittest.TestCase):
    def test_codex_executable_prefers_path(self) -> None:
        with patch.object(runtime.shutil, "which", return_value="/opt/bin/codex"), patch.object(
            runtime.sys, "platform", "darwin"
        ):
            self.assertEqual("/opt/bin/codex", runtime.codex_executable())

    def test_codex_executable_falls_back_to_bundled_macos_binary(self) -> None:
        with patch.object(runtime.shutil, "which", return_value=None), patch.object(
            runtime.sys, "platform", "darwin"
        ), patch.object(runtime.Path, "is_file", return_value=True):
            self.assertEqual(
                "/Applications/ChatGPT.app/Contents/Resources/codex",
                runtime.codex_executable(),
            )

    def test_codex_executable_on_windows_uses_path_only(self) -> None:
        with patch.object(runtime.shutil, "which", return_value="C:\\codex\\codex.exe"), patch.object(
            runtime.sys, "platform", "win32"
        ):
            self.assertEqual("C:\\codex\\codex.exe", runtime.codex_executable())


class AdapterPathsTests(unittest.TestCase):
    def test_windows_paths_use_localappdata(self) -> None:
        with patch.object(runtime.sys, "platform", "win32"), patch.dict(
            runtime.os.environ, {"LOCALAPPDATA": r"C:\Users\alice\AppData\Local"}
        ):
            paths = runtime.adapter_paths(Path(r"C:\codex"))
        self.assertEqual(r"C:\codex/adapters", paths.scripts_dir.as_posix())
        self.assertEqual(r"C:\codex/logs/adapters", paths.logs_dir.as_posix())
        self.assertEqual(
            r"C:\Users\alice\AppData\Local/Codex/SubagentAdapters",
            paths.definitions_dir.as_posix(),
        )

    def test_posix_paths_keep_launch_agents(self) -> None:
        with patch.object(runtime.sys, "platform", "darwin"):
            paths = runtime.adapter_paths(Path("/tmp/codex"))
        self.assertEqual(Path.home() / "Library" / "LaunchAgents", paths.definitions_dir)


class ServiceCommandTests(unittest.TestCase):
    def test_service_command_builds_full_argument_list(self) -> None:
        command = runtime.service_command(
            r"C:\Python\python.exe",
            Path(r"C:\codex\adapters\anthropic_responses_adapter.py"),
            listen_host="127.0.0.1",
            port=18766,
            service_id="provider_x",
            max_output_tokens=4096,
            upstream_base_url="https://example.invalid/api",
            model_catalog=Path(r"C:\codex\models\provider_x--model.json"),
            audit_log=Path(r"C:\codex\logs\adapters\provider_x.audit.jsonl"),
        )
        self.assertEqual(
            (
                r"C:\Python\python.exe",
                r"C:\codex\adapters\anthropic_responses_adapter.py",
                "--listen", "127.0.0.1",
                "--port", "18766",
                "--service-id", "provider_x",
                "--max-output-tokens", "4096",
                "--upstream-base-url", "https://example.invalid/api",
                "--model-catalog", r"C:\codex\models\provider_x--model.json",
                "--audit-log", r"C:\codex\logs\adapters\provider_x.audit.jsonl",
            ),
            command,
        )


class WindowsCommandQuotingTests(unittest.TestCase):
    def test_quotes_only_arguments_needing_it(self) -> None:
        self.assertEqual(r'C:\Python\python.exe', runtime.quote_windows_argument(r"C:\Python\python.exe"))
        self.assertEqual(r'"C:\Program Files\Python\python.exe"', runtime.quote_windows_argument(r"C:\Program Files\Python\python.exe"))
        self.assertEqual('""', runtime.quote_windows_argument(""))

    def test_join_quotes_spaced_paths(self) -> None:
        joined = runtime.quote_windows_command(
            [r"C:\Program Files\Python\python.exe", r"C:\codex scripts\claim_task.py", "--workspace", "."]
        )
        self.assertEqual(
            r'"C:\Program Files\Python\python.exe" "C:\codex scripts\claim_task.py" --workspace .',
            joined,
        )


if __name__ == "__main__":
    unittest.main()
