from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import diagnostics  # noqa: E402


class RedactionTests(unittest.TestCase):
    def test_secret_like_keys_are_redacted_recursively(self) -> None:
        value = {
            "provider": "deepseek_anthropic",
            "auth": {"type": "keychain", "service": "deepseek-api-key", "account": "codex"},
            "headers": {"Authorization": "Bearer sk-secret-value", "X-API-Key": "abc123"},
            "nested": {"client_secret": "s3cret", "password": "hunter2"},
            "safe": {"listen_port": 18767, "base_url": "https://api.example.com/anthropic"},
        }
        redacted = diagnostics.redact_json(value)
        self.assertEqual(
            {"type": "keychain", "service": "deepseek-api-key", "account": "codex"},
            redacted["auth"],
        )
        self.assertEqual(diagnostics.REDACTED, redacted["headers"]["Authorization"])
        self.assertEqual(diagnostics.REDACTED, redacted["headers"]["X-API-Key"])
        self.assertEqual(diagnostics.REDACTED, redacted["nested"]["client_secret"])
        self.assertEqual(diagnostics.REDACTED, redacted["nested"]["password"])
        self.assertEqual(18767, redacted["safe"]["listen_port"])
        self.assertEqual("https://api.example.com/anthropic", redacted["safe"]["base_url"])

    def test_bearer_and_api_key_patterns_are_redacted_in_text(self) -> None:
        text = (
            "Authorization: Bearer sk-abcdefghij1234567890\n"
            "api_key = ABC123secretkey987\n"
            "url https://user:pass@example.com/path ok\n"
        )
        redacted = diagnostics.redact_text(text)
        self.assertNotIn("sk-abcdefghij1234567890", redacted)
        self.assertNotIn("ABC123secretkey987", redacted)
        self.assertNotIn("user:pass@", redacted)
        self.assertIn("<redacted>", redacted)


class TailAndBoundsTests(unittest.TestCase):
    def test_bounded_tail_returns_last_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.txt"
            path.write_bytes(b"line0\nline1\nline2\nline3\n")
            content, meta = diagnostics.bounded_tail(path, max_lines=2, max_bytes=1024)
            self.assertEqual(b"line2\nline3\n", content)
            self.assertEqual(2, meta["tail_lines"])
            self.assertEqual(24, meta["total_bytes"])

    def test_bounded_tail_respects_byte_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.txt"
            path.write_text("".join(f"line-{i:04d}\n" for i in range(2000)), encoding="utf-8")
            total = path.stat().st_size
            content, meta = diagnostics.bounded_tail(path, max_lines=2000, max_bytes=256)
            self.assertLessEqual(len(content), 256)
            self.assertEqual(total, meta["total_bytes"])

    def test_bounded_read_reports_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.json"
            path.write_text("x" * 1000, encoding="utf-8")
            content, meta = diagnostics.bounded_read(path, max_bytes=100)
        self.assertEqual(100, len(content))
        self.assertTrue(meta["truncated"])

    def test_missing_file_is_reported_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            bundle_path, items = diagnostics.collect(
                run="missing_files",
                out=out,
                fmt="dir",
                codex_home=root / "home",
                workspace=root / "ws",
                skip_adapter_health=True,
                probe_codex=False,
            )
            names = {item["name"]: item for item in items}
            self.assertTrue(bundle_path.is_dir())
            self.assertEqual("missing", names["install/selection.json"]["status"])
            self.assertEqual("missing", names["install/registry-summary.json"]["status"])
            self.assertEqual("missing", names["mailbox/summary.json"]["status"])
            index = json.loads((bundle_path / "diagnostics.json").read_text(encoding="utf-8"))
            self.assertTrue((bundle_path / "README.txt").is_file())
            self.assertIn("README.txt", {item["name"] for item in index["items"]})

    def test_deterministic_filenames_and_zip_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, _ = diagnostics.collect(
                run="run_a", out=root / "out1", fmt="dir",
                codex_home=root / "home", workspace=root / "ws",
                skip_adapter_health=True, probe_codex=False,
            )
            second, _ = diagnostics.collect(
                run="run_a", out=root / "out2", fmt="dir",
                codex_home=root / "home", workspace=root / "ws",
                skip_adapter_health=True, probe_codex=False,
            )
            self.assertEqual(
                sorted(path.name for path in first.iterdir()),
                sorted(path.name for path in second.iterdir()),
            )
            zip_path, _ = diagnostics.collect(
                run="run_a", out=root / "out3", fmt="zip",
                codex_home=root / "home", workspace=root / "ws",
                skip_adapter_health=True, probe_codex=False,
            )
            self.assertTrue(zip_path.name.endswith(".zip"))
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())
            self.assertIn("diagnostics.json", names)
            self.assertIn("README.txt", names)
            self.assertIn("meta/platform.json", names)

    def test_schema_v2_adapter_and_configure_log_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            log_dir = home / "logs" / "custom-subagents"
            log_dir.mkdir(parents=True)
            (log_dir / "configure-20260802.jsonl").write_text(
                '{"stderr":"api_key=sk-secret-example"}\n', encoding="utf-8"
            )
            adapter_log_dir = home / "logs" / "adapters"
            adapter_log_dir.mkdir(parents=True)
            (adapter_log_dir / "deepseek_anthropic.stderr.log").write_text(
                "Authorization: Bearer sk-adapter-secret\n", encoding="utf-8"
            )
            manifest = PROJECT_ROOT / "config" / "deepseek-anthropic-1m.example.json"
            bundle, _ = diagnostics.collect(
                run="schema_v2",
                out=root / "out",
                fmt="dir",
                codex_home=home,
                workspace=root / "ws",
                manifest=manifest,
                skip_adapter_health=True,
                probe_codex=False,
            )
            health = json.loads(
                (bundle / "adapters" / "health.json").read_text(encoding="utf-8")
            )
            self.assertTrue(health["skipped"])
            configure_tail = (bundle / "configure" / "01.jsonl.tail").read_text(
                encoding="utf-8"
            )
            self.assertIn("<redacted>", configure_tail)
            self.assertNotIn("sk-secret-example", configure_tail)
            adapter_tail = (
                bundle / "adapters" / "deepseek_anthropic.stderr.log.tail"
            ).read_text(encoding="utf-8")
            self.assertIn("<redacted>", adapter_tail)
            self.assertNotIn("sk-adapter-secret", adapter_tail)


class ExclusionTests(unittest.TestCase):
    def test_readme_regenerate_hint_uses_actual_interpreter(self) -> None:
        """Issue #2: generated commands must use sys.executable, not bare python3."""
        readme = diagnostics.build_readme(
            run="regenerate-hint",
            bundle_path=Path("/tmp/bundle"),
            items=[],
        )
        self.assertIn(f"{sys.executable} scripts/diagnostics.py --run regenerate-hint", readme)
        self.assertNotIn("\n  python3 scripts/diagnostics.py", readme)

    def test_task_markdown_and_config_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir(parents=True)
            secret_task = "Task: run_me\n\nInstall the provider with api_key = sk-leak-value\n"
            (home / "task.md").write_text(secret_task, encoding="utf-8")
            (home / "config.toml").write_text("[model_providers.deepseek.auth]\napi_key = \"sk-leak\"\n", encoding="utf-8")
            out = root / "out"
            bundle_path, items = diagnostics.collect(
                run="exclusions", out=out, fmt="dir",
                codex_home=home, workspace=root / "ws",
                skip_adapter_health=True, probe_codex=False,
            )
            collected = {
                item["name"]: item for item in items if item["status"] == "collected"
            }
            self.assertNotIn("task.md", collected)
            self.assertNotIn("config.toml", collected)
            for path in bundle_path.rglob("*"):
                if path.is_file():
                    content = path.read_text(encoding="utf-8", errors="replace")
                    self.assertNotIn("sk-leak-value", content)
                    self.assertNotIn("sk-leak", content)

    def test_secret_values_in_receipt_summaries_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ws = root / "ws"
            claimed = ws / ".deepseek-delegations" / "claimed"
            claimed.mkdir(parents=True)
            receipt = {
                "schema_version": 1,
                "status": "claimed",
                "task_id": "task_one",
                "claim_id": "1111-abc",
                "attempt_id": "attempt-1",
                "claimed_at": "2026-08-02T00:00:00Z",
                "completed_at": None,
                "exit_code": None,
                "summary": "login api_key sk-secret123",
                "agent": "deepseek_anthropic_worker",
            }
            (claimed / "task_one--1111-abc.md.receipt").write_text(json.dumps(receipt), encoding="utf-8")
            out = root / "out"
            bundle_path, items = diagnostics.collect(
                run="receipts", out=out, fmt="dir",
                codex_home=root / "home", workspace=ws,
                skip_adapter_health=True, probe_codex=False,
            )
            summary_path = bundle_path / "mailbox" / "summary.json"
            content = summary_path.read_text(encoding="utf-8")
            self.assertNotIn("sk-secret123", content)
            self.assertIn("<redacted>", content)
            summary = json.loads(content)
            self.assertEqual(1, len(summary["receipts"]))
            self.assertEqual("task_one", summary["receipts"][0]["task_id"])


if __name__ == "__main__":
    unittest.main()
