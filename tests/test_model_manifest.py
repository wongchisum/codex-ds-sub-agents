from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import model_manifest as mm  # noqa: E402
import model_selection as ms  # noqa: E402


EXAMPLE = PROJECT_ROOT / "config" / "model-providers.example.json"


class ModelManifestTests(unittest.TestCase):
    def test_example_uses_one_primary_and_ordered_fallback(self) -> None:
        manifest = mm.load_manifest(EXAMPLE)
        self.assertEqual("claude-opus-4-6", manifest.selection.primary.id)
        self.assertEqual(["gemini-3-5-flash"], [item.id for item in manifest.selection.fallbacks])
        self.assertEqual(1, manifest.selection.max_switches)
        self.assertEqual(
            "aicodemirror-api-key",
            manifest.providers["aicodemirror_claude"].auth.name,
        )
        self.assertEqual(
            manifest.providers["aicodemirror_claude"].auth,
            manifest.providers["claudecode_gemini"].auth,
        )

    def test_gemini_manifest_uses_verified_anthropic_route(self) -> None:
        manifest = mm.load_manifest(PROJECT_ROOT / "config" / "gemini-anthropic.example.json")
        provider = manifest.providers["claudecode_gemini"]
        model = manifest.models["gemini-3-5-flash"]

        self.assertEqual("gemini-3-5-flash", manifest.selection.primary.id)
        self.assertEqual((), manifest.selection.fallbacks)
        self.assertEqual("https://api.claudecode.net.cn/api/gemini", provider.base_url)
        self.assertEqual("http://127.0.0.1:18768", provider.effective_base_url)
        self.assertEqual("anthropic_messages", provider.adapter.kind)
        self.assertEqual(4096, provider.adapter.max_output_tokens)
        self.assertEqual("gemini-3.5-flash", model.remote_model)
        self.assertEqual("claudecode_gemini_worker", model.agent)

    def test_example_contains_no_inline_credential(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("sk-", text)
        self.assertNotIn("api_key", text.lower())
        manifest = mm.load_manifest(EXAMPLE)
        for model in manifest.selection.models.values():
            self.assertEqual((), ms.validate_model(model))

    def test_provider_and_agent_rendering(self) -> None:
        manifest = mm.load_manifest(EXAMPLE)
        provider = mm.render_provider(manifest.providers["aicodemirror_claude"])
        self.assertIn("[model_providers.aicodemirror_claude]", provider)
        self.assertIn('base_url = "http://127.0.0.1:18766"', provider)
        self.assertIn('wire_api = "responses"', provider)
        self.assertIn("aicodemirror-api-key", provider)
        self.assertNotIn("sk-", provider)

        template = (PROJECT_ROOT / "agents" / "model-worker.toml.template").read_text(encoding="utf-8")
        agent = mm.render_agent(
            template,
            Path("/tmp/codex-home"),
            manifest.models["claude-opus-4-6"],
        )
        self.assertIn('model = "claude-opus-4-6"', agent)
        self.assertIn('model_provider = "aicodemirror_claude"', agent)
        self.assertIn(
            'model_catalog_json = "/tmp/codex-home/models/aicodemirror_claude--claude-opus-4-6.json"',
            agent,
        )
        self.assertIn("/tmp/codex-home", agent)
        self.assertIn("declared configuration value", agent)
        self.assertNotIn("__", agent)

    def test_render_agent_escapes_windows_paths_and_python_command(self) -> None:
        manifest = mm.load_manifest(EXAMPLE)
        template = (PROJECT_ROOT / "agents" / "model-worker.toml.template").read_text(encoding="utf-8")
        agent = mm.render_agent(
            template,
            Path(r"C:\Users\alice\.codex"),
            manifest.models["claude-opus-4-6"],
        )
        self.assertNotIn("__PYTHON_COMMAND__", agent)
        self.assertNotIn("__CODEX_HOME__", agent)
        self.assertNotIn("__", agent)
        self.assertIn(r"C:\\Users\\alice\\.codex", agent)
        self.assertIn('model = "claude-opus-4-6"', agent)

    def test_adapter_keeps_remote_url_separate_from_codex_url(self) -> None:
        manifest = mm.load_manifest(EXAMPLE)
        provider = manifest.providers["aicodemirror_claude"]
        self.assertEqual("https://api.aicodemirror.ai/api/claudecode", provider.base_url)
        self.assertEqual("http://127.0.0.1:18766", provider.effective_base_url)
        self.assertEqual("anthropic_messages", provider.adapter.kind)

        gemini = manifest.providers["claudecode_gemini"]
        self.assertEqual("https://api.claudecode.net.cn/api/gemini", gemini.base_url)
        self.assertEqual("http://127.0.0.1:18768", gemini.effective_base_url)
        self.assertEqual("anthropic_messages", gemini.adapter.kind)

    def test_context_window_is_rendered_from_manifest(self) -> None:
        manifest = mm.load_manifest(PROJECT_ROOT / "config" / "deepseek-anthropic-1m.example.json")
        model = manifest.models["deepseek-v4-flash"]
        rendered = json.loads(mm.render_model_catalog(model))["models"][0]
        self.assertEqual(1048576, rendered["context_window"])
        self.assertEqual(1048576, rendered["max_context_window"])
        self.assertEqual(95, rendered["effective_context_window_percent"])
        self.assertEqual("deepseek-v4-flash", rendered["slug"])

    def test_current_codex_protocol_allowlist(self) -> None:
        manifest = mm.load_manifest(EXAMPLE)
        self.assertEqual((), mm.unsupported_protocols(manifest))

    def test_inline_key_field_is_rejected(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0]["auth"]["api_key"] = "not-stored-here"
        with self.assertRaises(ms.ValidationError) as context:
            self._load(data)
        self.assertIn("forbidden inline credential", str(context.exception))

    def test_unknown_provider_is_rejected(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["models"][0]["provider"] = "missing"
        with self.assertRaises(ms.ValidationError):
            self._load(data)

    def test_invalid_context_window_is_rejected(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["models"][0]["context_window"] = 0
        with self.assertRaises(ms.ValidationError):
            self._load(data)

    def test_max_context_window_cannot_be_smaller(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["models"][0]["max_context_window"] = 100000
        with self.assertRaises(ms.ValidationError):
            self._load(data)

    def test_non_loopback_adapter_is_rejected(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0]["local_adapter"] = {"listen_host": "0.0.0.0"}
        with self.assertRaises(ms.ValidationError):
            self._load(data)

    def test_adapter_output_limit_must_be_bounded(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][1]["local_adapter"]["max_output_tokens"] = 64001
        with self.assertRaisesRegex(ms.ValidationError, "max_output_tokens"):
            self._load(data)

    def test_duplicate_agent_name_is_rejected(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["models"][1]["agent"] = data["models"][0]["agent"]
        with self.assertRaisesRegex(ms.ValidationError, "duplicate agent name"):
            self._load(data)

    def test_adapter_listen_address_conflict_is_rejected(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][1]["local_adapter"]["listen_host"] = "localhost"
        data["providers"][1]["local_adapter"]["listen_port"] = 18766
        with self.assertRaisesRegex(ms.ValidationError, "listen address conflict"):
            self._load(data)

    def test_adapter_upstream_requires_strict_https_url(self) -> None:
        invalid_urls = (
            "http://example.com/api",
            "https://",
            "https://user:secret@example.com/api",
            "https://example.com/api?token=value",
            "https://example.com/api#fragment",
            "https://example.com:0/api",
            "https://example.com\\evil",
        )
        for base_url in invalid_urls:
            with self.subTest(base_url=base_url):
                data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
                data["providers"][0]["base_url"] = base_url
                with self.assertRaisesRegex(ms.ValidationError, "HTTPS URL"):
                    self._load(data)

    def test_direct_provider_keeps_http_base_url_support(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0].pop("local_adapter", None)
        data["providers"][0]["upstream_protocol"] = "openai_responses"
        data["providers"][0]["base_url"] = "http://127.0.0.1:8080/v1"

        manifest = self._load(data)

        self.assertEqual(
            "http://127.0.0.1:8080/v1",
            manifest.providers["aicodemirror_claude"].effective_base_url,
        )

    def test_direct_provider_rejects_sensitive_or_malformed_url_parts(self) -> None:
        invalid_urls = (
            "ftp://example.com/v1",
            "http://user:secret@example.com/v1",
            "https://example.com/v1?token=value",
            "https://example.com/v1?",
            "https://example.com/v1#fragment",
            "https://example.com/v1#",
            "http://example.com:0/v1",
            "http://example.com\\evil",
        )
        for base_url in invalid_urls:
            with self.subTest(base_url=base_url):
                data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
                data["providers"][0].pop("local_adapter", None)
                data["providers"][0]["upstream_protocol"] = "openai_responses"
                data["providers"][0]["base_url"] = base_url
                with self.assertRaisesRegex(ms.ValidationError, r"HTTP\(S\) URL"):
                    self._load(data)

    def test_reasoning_effort_matches_generated_catalog(self) -> None:
        for effort in mm.SUPPORTED_REASONING_EFFORTS:
            with self.subTest(effort=effort):
                data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
                data["models"][0]["reasoning_effort"] = effort
                manifest = self._load(data)
                catalog = json.loads(
                    mm.render_model_catalog(manifest.models["claude-opus-4-6"])
                )["models"][0]
                self.assertEqual(effort, catalog["default_reasoning_level"])
                self.assertIn(
                    effort,
                    [item["effort"] for item in catalog["supported_reasoning_levels"]],
                )

        for invalid in ("xhigh", "", None, 3):
            with self.subTest(invalid=invalid):
                data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
                data["models"][0]["reasoning_effort"] = invalid
                with self.assertRaisesRegex(ms.ValidationError, "reasoning_effort"):
                    self._load(data)

    def test_provider_retry_and_timeout_values_are_bounded(self) -> None:
        invalid_values = (
            ("request_max_retries", 0),
            ("request_max_retries", mm.MAX_RETRY_COUNT + 1),
            ("stream_max_retries", 0),
            ("stream_max_retries", mm.MAX_RETRY_COUNT + 1),
            ("stream_idle_timeout_ms", mm.MIN_STREAM_IDLE_TIMEOUT_MS - 1),
            ("stream_idle_timeout_ms", mm.MAX_STREAM_IDLE_TIMEOUT_MS + 1),
        )
        for key, value in invalid_values:
            with self.subTest(key=key, value=value):
                data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
                data["providers"][0][key] = value
                with self.assertRaisesRegex(ms.ValidationError, key):
                    self._load(data)

    def test_adapter_provider_requires_exactly_one_model_catalog(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        second = dict(data["models"][0])
        second.update(
            {
                "id": "claude-sonnet-4-6",
                "remote_model": "claude-sonnet-4-6",
                "agent": "aicodemirror_sonnet_worker",
            }
        )
        data["models"].append(second)

        with self.assertRaisesRegex(ms.ValidationError, "exactly one model catalog"):
            self._load(data)

    def test_direct_provider_can_define_multiple_model_catalogs(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0].pop("local_adapter", None)
        data["providers"][0]["upstream_protocol"] = "openai_responses"
        second = dict(data["models"][0])
        second.update(
            {
                "id": "claude-sonnet-4-6",
                "remote_model": "claude-sonnet-4-6",
                "agent": "aicodemirror_sonnet_worker",
            }
        )
        data["models"].append(second)

        manifest = self._load(data)

        self.assertEqual(
            "aicodemirror_claude", manifest.models[second["id"]].provider_id
        )

    def test_unknown_protocol_is_configurable_but_not_activatable(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0]["protocol"] = "messages"
        manifest = self._load(data)
        self.assertEqual(("aicodemirror_claude",), mm.unsupported_protocols(manifest))

    def test_custom_auth_header_uses_environment_reference(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0]["auth"] = {
            "type": "env_header",
            "header": "x-api-key",
            "variable": "AICODEMIRROR_API_KEY",
        }
        manifest = self._load(data)
        rendered = mm.render_provider(manifest.providers["aicodemirror_claude"])
        self.assertIn(
            'env_http_headers = { "x-api-key" = "AICODEMIRROR_API_KEY" }',
            rendered,
        )
        self.assertNotIn("[model_providers.aicodemirror_claude.auth]", rendered)

    def test_v1_direct_responses_normalizes_to_openai_upstream(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["schema_version"] = 1
        for provider in data["providers"]:
            provider.pop("upstream_protocol", None)
            provider.pop("local_adapter", None)
        manifest = self._load(data)
        provider = manifest.providers["aicodemirror_claude"]
        self.assertEqual("openai_responses", provider.upstream_protocol)
        self.assertIsNone(provider.adapter)
        self.assertEqual(provider.base_url, provider.effective_base_url)

    def test_v1_adapter_normalizes_to_anthropic_messages_upstream(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["schema_version"] = 1
        for provider in data["providers"]:
            tuning = provider.pop("local_adapter", {})
            provider.pop("upstream_protocol", None)
            provider["adapter"] = {"type": "anthropic_messages", **tuning}
        manifest = self._load(data)
        for provider in manifest.providers.values():
            self.assertEqual("anthropic_messages", provider.upstream_protocol)
            self.assertIsNotNone(provider.adapter)
            self.assertEqual("anthropic_messages", provider.adapter.kind)

    def test_v2_direct_openai_responses_requires_no_adapter(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0].pop("local_adapter", None)
        data["providers"][0]["upstream_protocol"] = "openai_responses"
        provider = self._load(data).providers["aicodemirror_claude"]
        self.assertEqual("openai_responses", provider.upstream_protocol)
        self.assertIsNone(provider.adapter)

    def test_v2_anthropic_messages_requires_no_adapter_block(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0].pop("local_adapter", None)
        data["providers"][0]["upstream_protocol"] = "anthropic_messages"
        provider = self._load(data).providers["aicodemirror_claude"]
        self.assertEqual("anthropic_messages", provider.upstream_protocol)
        self.assertIsNotNone(provider.adapter)
        self.assertEqual("anthropic_messages", provider.adapter.kind)
        self.assertEqual("127.0.0.1", provider.adapter.listen_host)
        self.assertEqual(18766, provider.adapter.listen_port)

    def test_v2_anthropic_messages_accepts_local_adapter_tuning(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0]["upstream_protocol"] = "anthropic_messages"
        data["providers"][0]["local_adapter"] = {"listen_port": 19001}
        provider = self._load(data).providers["aicodemirror_claude"]
        self.assertEqual("anthropic_messages", provider.upstream_protocol)
        self.assertEqual(19001, provider.adapter.listen_port)

    def test_v2_multiple_anthropic_providers_get_unique_default_ports(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        for provider in data["providers"]:
            provider.pop("local_adapter", None)
        manifest = self._load(data)
        self.assertEqual(
            [18766, 18767],
            [provider.adapter.listen_port for provider in manifest.providers.values()],
        )

    def test_v2_rejects_legacy_adapter_field(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0]["adapter"] = {"type": "anthropic_messages"}
        with self.assertRaisesRegex(ms.ValidationError, "schema_version 1 compatibility"):
            self._load(data)

    def test_v2_openai_responses_with_adapter_is_conflicting(self) -> None:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["providers"][0]["upstream_protocol"] = "openai_responses"
        data["providers"][0]["local_adapter"] = {"listen_port": 18766}
        with self.assertRaisesRegex(ms.ValidationError, "conflicts with adapter"):
            self._load(data)

    def test_v2_unsupported_upstream_protocol_is_rejected(self) -> None:
        for value in ("responses", "messages", "", 3):
            with self.subTest(value=value):
                data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
                data["providers"][0]["upstream_protocol"] = value
                with self.assertRaisesRegex(
                    ms.ValidationError, "upstream_protocol must be one of"
                ):
                    self._load(data)

    @staticmethod
    def _load(data: dict) -> mm.ModelManifest:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return mm.load_manifest(path)


if __name__ == "__main__":
    unittest.main()
