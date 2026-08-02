from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = ROOT / "skills" / "deepseek-delegation" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
MODULE_PATH = SKILL_SCRIPTS / "delegation_runtime.py"
SPEC = importlib.util.spec_from_file_location("delegation_runtime", MODULE_PATH)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def selection() -> dict:
    return {
        "selection": {"primary": "primary", "fallbacks": ["fallback"], "max_switches": 1},
        "models": {
            "primary": {"agent": "primary_worker", "provider": "provider_a", "remote_model": "model-a"},
            "fallback": {"agent": "fallback_worker", "provider": "provider_b", "remote_model": "model-b"},
        },
    }


class FailureClassificationTests(unittest.TestCase):
    def test_native_stream_disconnect_is_network(self) -> None:
        self.assertEqual(
            "network",
            runtime.classify_failure(message="stream disconnected before completion: error sending request"),
        )

    def test_upstream_invalid_response_is_service_unavailable(self) -> None:
        self.assertEqual(
            "service_unavailable",
            runtime.classify_failure(http_status=502, error_code="upstream_invalid_response"),
        )

    def test_unknown_failure_never_becomes_eligible(self) -> None:
        self.assertEqual("unknown", runtime.classify_failure(message="worker returned an unexpected result"))

    def test_anthropic_overload_is_service_unavailable(self) -> None:
        self.assertEqual(
            "service_unavailable",
            runtime.classify_failure(http_status=529, error_code="overloaded_error"),
        )

    def test_credit_balance_error_is_billing_even_when_http_400(self) -> None:
        self.assertEqual(
            "billing",
            runtime.classify_failure(http_status=400, message="Your credit balance is too low"),
        )

    def test_macos_dns_error_is_network(self) -> None:
        self.assertEqual(
            "network",
            runtime.classify_failure(message="nodename nor servname provided, or not known"),
        )


class RuntimeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.selection_path = self.workspace / "selection.json"
        self.selection_path.write_text(json.dumps(selection()), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_eligible_failure_switches_once_and_persists(self) -> None:
        started = runtime.begin(self.workspace, "run_1", self.selection_path)
        self.assertEqual("primary_worker", started["active"]["agent"])
        switched = runtime.record_failure(
            self.workspace,
            "run_1",
            {"category": None, "http_status": None, "error_code": None, "message": "connection refused"},
        )
        self.assertTrue(switched["switched"])
        self.assertEqual("fallback_worker", switched["active"]["agent"])
        state = runtime.show(self.workspace, "run_1")
        self.assertEqual(["primary", "fallback"], state["attempted"])

    def test_ineligible_failure_does_not_switch(self) -> None:
        runtime.begin(self.workspace, "run_2", self.selection_path)
        result = runtime.record_failure(
            self.workspace,
            "run_2",
            {"category": "auth", "http_status": 401, "error_code": None, "message": None},
        )
        self.assertFalse(result["switched"])
        self.assertEqual("running", result["status"])
        self.assertEqual(["primary"], result["attempted"])

    def test_second_eligible_failure_exhausts_policy(self) -> None:
        runtime.begin(self.workspace, "run_3", self.selection_path)
        failure = {"category": "network", "http_status": None, "error_code": None, "message": None}
        runtime.record_failure(self.workspace, "run_3", failure)
        result = runtime.record_failure(self.workspace, "run_3", failure)
        self.assertEqual("exhausted", result["status"])
        self.assertTrue(result["exhausted"])

    def test_existing_run_cannot_be_restarted(self) -> None:
        runtime.begin(self.workspace, "run_4", self.selection_path)
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.begin(self.workspace, "run_4", self.selection_path)

    def test_finish_is_idempotent_but_outcome_cannot_change(self) -> None:
        runtime.begin(self.workspace, "run_5", self.selection_path)
        first = runtime.finish(self.workspace, "run_5", "completed")
        second = runtime.finish(self.workspace, "run_5", "completed")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.finish(self.workspace, "run_5", "blocked")

    def test_failure_message_is_classified_but_not_persisted(self) -> None:
        runtime.begin(self.workspace, "run_6", self.selection_path)
        secret = "stream disconnected with Bearer secret-value"
        runtime.record_failure(
            self.workspace,
            "run_6",
            {"category": None, "http_status": None, "error_code": None, "message": secret},
        )
        state_path = runtime.state_path(self.workspace, "run_6")
        raw = state_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        failure = runtime.show(self.workspace, "run_6")["failures"][0]
        self.assertTrue(failure["message_present"])
        self.assertEqual(64, len(failure["message_sha256"]))

    def test_oversized_failure_message_is_rejected(self) -> None:
        runtime.begin(self.workspace, "run_7", self.selection_path)
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.record_failure(
                self.workspace,
                "run_7",
                {
                    "category": None,
                    "http_status": None,
                    "error_code": None,
                    "message": "x" * (runtime.MAX_FAILURE_MESSAGE_CHARS + 1),
                },
            )


if __name__ == "__main__":
    unittest.main()
