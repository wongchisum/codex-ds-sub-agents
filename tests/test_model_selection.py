from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import model_selection as ms  # noqa: E402


def make_model(
    model_id: str = "m1",
    provider_id: str = "provider-a",
    remote_name: str = "model-1",
    base_url: str = "https://api.example.com/v1",
    protocol: str = "openai",
    credential: ms.CredentialRef = None,
) -> ms.Model:
    if credential is None:
        credential = ms.CredentialRef(kind="env", name="EXAMPLE_API_KEY")
    return ms.Model(
        id=model_id,
        provider_id=provider_id,
        remote_name=remote_name,
        base_url=base_url,
        protocol=protocol,
        credential=credential,
    )


def make_policy(
    fallback_ids=("f1", "f2"),
    max_switches: int = 3,
) -> ms.SelectionPolicy:
    models = [
        make_model("p", "provider-a", "primary-model"),
        make_model("f1", "provider-b", "fallback-model-1"),
        make_model("f2", "provider-b", "fallback-model-2"),
    ]
    return ms.SelectionPolicy(models, "p", fallback_ids, max_switches)


class ModelSelectionTests(unittest.TestCase):
    def test_initial_state_selects_primary(self) -> None:
        policy = make_policy()
        state = policy.begin()
        self.assertEqual(state.active_model_id, "p")
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.attempted, ("p",))
        self.assertEqual(state.switch_count, 0)
        self.assertIsNone(state.last_failure)
        self.assertFalse(state.exhausted)
        self.assertEqual(policy.primary.id, "p")
        self.assertEqual(policy.model("f1").remote_name, "fallback-model-1")
        self.assertEqual(policy.max_switches, 3)

    def test_duplicate_model_ids_rejected(self) -> None:
        models = [make_model("p"), make_model("p")]
        errors = ms.validate_models(models)
        self.assertIn("duplicate model id", " ".join(errors))
        with self.assertRaises(ms.ValidationError):
            ms.SelectionPolicy(models, "p", (), 0)

    def test_missing_primary_reference_rejected(self) -> None:
        models = [make_model("p")]
        with self.assertRaises(ms.ValidationError) as ctx:
            ms.SelectionPolicy(models, "ghost", (), 0)
        self.assertTrue(
            any("not configured" in error for error in ctx.exception.errors),
            ctx.exception.errors,
        )

    def test_missing_fallback_reference_rejected(self) -> None:
        with self.assertRaises(ms.ValidationError) as ctx:
            make_policy(fallback_ids=("ghost",))
        self.assertIn("ghost", str(ctx.exception))

    def test_primary_in_fallback_rejected(self) -> None:
        with self.assertRaises(ms.ValidationError) as ctx:
            make_policy(fallback_ids=("p",))
        self.assertTrue(
            any("fallback list" in error for error in ctx.exception.errors),
            ctx.exception.errors,
        )

    def test_duplicate_fallback_rejected(self) -> None:
        with self.assertRaises(ms.ValidationError) as ctx:
            make_policy(fallback_ids=("f1", "f1"))
        self.assertIn("appears more than once", str(ctx.exception))

    def test_negative_max_switches_rejected(self) -> None:
        with self.assertRaises(ms.ValidationError) as ctx:
            make_policy(max_switches=-1)
        self.assertIn(">= 0", str(ctx.exception))

    def test_invalid_base_urls_rejected(self) -> None:
        for bad_url in ("", "not-a-url", "ftp://api.example.com/v1", "https://"):
            with self.subTest(url=bad_url):
                model = make_model(base_url=bad_url)
                self.assertIn(
                    "base URL", " ".join(ms.validate_model(model)),
                    "url {!r} should be invalid".format(bad_url),
                )

    def test_empty_protocol_rejected(self) -> None:
        model = make_model(protocol="")
        self.assertIn("protocol", " ".join(ms.validate_model(model)))

    def test_empty_remote_model_name_rejected(self) -> None:
        model = make_model(remote_name="")
        self.assertIn("remote model name", " ".join(ms.validate_model(model)))

    def test_missing_credential_reference_rejected(self) -> None:
        model = make_model(credential=ms.CredentialRef(kind="env", name=""))
        errors = ms.validate_model(model)
        self.assertIn("credential reference name", " ".join(errors))

    def test_unsafe_inline_credential_values_rejected(self) -> None:
        unsafe_names = (
            "sk-proj-abc123secret",
            "api_key=realsecret",
            "secret with spaces",
            "x" * 65,
        )
        for name in unsafe_names:
            with self.subTest(name=name):
                model = make_model(
                    credential=ms.CredentialRef(kind="env", name=name)
                )
                self.assertIn(
                    "inline credential",
                    " ".join(ms.validate_model(model)),
                    "name {!r} should be rejected".format(name),
                )

    def test_safe_credential_references_accepted(self) -> None:
        safe = (
            ms.CredentialRef(kind="keychain", name="deepseek-api-key"),
            ms.CredentialRef(kind="env", name="DEEPSEEK_API_KEY"),
        )
        for credential in safe:
            with self.subTest(credential=credential):
                self.assertEqual(ms.validate_model(make_model(credential=credential)), ())

    def test_eligible_categories_trigger_switch(self) -> None:
        for category in (
            "network",
            "timeout",
            "rate_limit",
            "billing",
            "service_unavailable",
        ):
            with self.subTest(category=category):
                policy = make_policy()
                state = policy.record_failure(policy.begin(), category)
                self.assertEqual(state.active_model_id, "f1")
                self.assertEqual(state.switch_count, 1)
                self.assertEqual(state.generation, 2)
                self.assertEqual(state.attempted, ("p", "f1"))
                self.assertEqual(
                    state.last_failure.category, ms.FailureCategory(category)
                )

    def test_ineligible_categories_do_not_switch(self) -> None:
        for category in (
            "auth",
            "invalid_request",
            "model_not_found",
            "task_failure",
            "unknown",
        ):
            with self.subTest(category=category):
                policy = make_policy()
                state = policy.record_failure(policy.begin(), category)
                self.assertEqual(state.active_model_id, "p")
                self.assertEqual(state.switch_count, 0)
                self.assertEqual(state.generation, 1)
                self.assertEqual(state.attempted, ("p",))
                self.assertFalse(state.exhausted)
                self.assertEqual(
                    state.last_failure.category, ms.FailureCategory(category)
                )

    def test_ineligible_failure_does_not_consume_switch_budget(self) -> None:
        policy = make_policy(max_switches=1)
        state = policy.begin()
        state = policy.record_failure(state, "auth")
        state = policy.record_failure(state, "network")
        self.assertEqual(state.active_model_id, "f1")
        self.assertEqual(state.switch_count, 1)

    def test_exhaustion_without_fallbacks(self) -> None:
        policy = make_policy(fallback_ids=())
        state = policy.record_failure(policy.begin(), "network")
        self.assertTrue(state.exhausted)
        self.assertEqual(state.active_model_id, "p")
        self.assertEqual(state.switch_count, 0)

    def test_max_switches_blocks_further_switches(self) -> None:
        policy = make_policy(max_switches=1)
        state = policy.begin()
        state = policy.record_failure(state, "network")
        self.assertEqual(state.active_model_id, "f1")
        self.assertEqual(state.switch_count, 1)
        state = policy.record_failure(state, "network")
        self.assertTrue(state.exhausted)
        self.assertEqual(state.active_model_id, "f1")
        self.assertEqual(state.switch_count, 1)

    def test_never_loops_back_to_attempted_model(self) -> None:
        policy = make_policy()
        state = policy.begin()
        state = policy.record_failure(state, "network")
        self.assertEqual(state.active_model_id, "f1")
        state = policy.record_failure(state, "timeout")
        self.assertEqual(state.active_model_id, "f2")
        state = policy.record_failure(state, "network")
        self.assertTrue(state.exhausted)
        self.assertEqual(state.active_model_id, "f2")
        self.assertEqual(state.attempted, ("p", "f1", "f2"))

    def test_failure_message_is_recorded(self) -> None:
        policy = make_policy()
        state = policy.record_failure(policy.begin(), "timeout", "request timed out")
        self.assertEqual(state.last_failure.message, "request timed out")

    def test_unknown_category_string_rejected(self) -> None:
        policy = make_policy()
        with self.assertRaises(ValueError):
            policy.record_failure(policy.begin(), "definitely-not-a-category")

    def test_foreign_state_rejected(self) -> None:
        other_models = [make_model("x", "provider-z", "other-model")]
        other = ms.SelectionPolicy(other_models, "x", (), 0)
        policy = make_policy()
        with self.assertRaises(ValueError):
            policy.record_failure(other.begin(), "network")

    def test_deterministic_generation_across_policies(self) -> None:
        policy_a = make_policy()
        policy_b = make_policy()
        state_a = policy_a.begin()
        state_b = policy_b.begin()
        self.assertEqual(state_a, state_b)
        for category in ("network", "timeout", "rate_limit"):
            state_a = policy_a.record_failure(state_a, category)
            state_b = policy_b.record_failure(state_b, category)
        self.assertEqual(state_a, state_b)
        self.assertEqual(state_a.generation, 3)
        self.assertEqual(state_a.switch_count, 2)
        self.assertEqual(state_a.attempted, ("p", "f1", "f2"))
        self.assertTrue(state_a.exhausted)

    def test_constructor_reports_multiple_errors(self) -> None:
        models = [
            ms.Model(
                id="",
                provider_id="",
                remote_name="",
                base_url="nope",
                protocol="",
                credential=ms.CredentialRef(kind="", name="sk-leaked-secret"),
            )
        ]
        with self.assertRaises(ms.ValidationError) as ctx:
            ms.SelectionPolicy(models, "p", (), 0)
        self.assertGreaterEqual(len(ctx.exception.errors), 6)


if __name__ == "__main__":
    unittest.main()
