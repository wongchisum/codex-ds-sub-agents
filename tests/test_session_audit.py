from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import session_audit  # noqa: E402


class SessionAuditTests(unittest.TestCase):
    def test_declared_and_actual_context_remain_distinct(self) -> None:
        lines = [
            {"type": "session_meta", "payload": {
                "id": "worker-1", "model_provider": "provider-a",
                "source": {"subagent": {"thread_spawn": {
                    "parent_thread_id": "parent-1", "agent_role": "worker-a",
                }}},
            }},
            {"type": "turn_context", "payload": {"model": "remote-a"}},
            {"type": "event_msg", "payload": {
                "type": "task_started", "model_context_window": 258400,
            }},
            {"type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": None,
                "error": {"message": "upstream_invalid_response"},
            }},
        ]
        selection = {"models": {"local-a": {
            "provider": "provider-a", "remote_model": "remote-a", "context_window": 1048576,
        }}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            rollout.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")
            selected = root / "selection.json"
            selected.write_text(json.dumps(selection), encoding="utf-8")
            result = session_audit.audit(rollout, selected)
        self.assertEqual(1048576, result["declared_context_window"])
        self.assertEqual(258400, result["actual_model_context_window"])
        self.assertFalse(result["context_matches_declaration"])
        self.assertFalse(result["first_turn_direct_nonempty_result"])
        self.assertTrue(result["upstream_invalid_response"])
        self.assertEqual("worker-1", result["worker_thread_id"])

    def test_multiple_turns_are_reported_as_follow_up(self) -> None:
        lines = [
            {"type": "turn_context", "payload": {"model": "m"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "first"}},
            {"type": "turn_context", "payload": {"model": "m"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "second"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            rollout.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")
            result = session_audit.audit(rollout)
        self.assertTrue(result["follow_up_occurred"])
        self.assertTrue(result["first_turn_direct_nonempty_result"])

    def test_task_text_does_not_fake_upstream_error_and_provider_disambiguates_model(self) -> None:
        lines = [
            {"type": "session_meta", "payload": {
                "id": "worker-2", "model_provider": "provider-b",
            }},
            {"type": "turn_context", "payload": {
                "model": "shared-remote",
                "user_message": "Check whether upstream_invalid_response occurred",
            }},
            {"type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": "done",
            }},
        ]
        selection = {"models": {
            "local-a": {
                "provider": "provider-a", "remote_model": "shared-remote",
                "context_window": 100,
            },
            "local-b": {
                "provider": "provider-b", "remote_model": "shared-remote",
                "context_window": 200,
            },
        }}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            rollout.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")
            selected = root / "selection.json"
            selected.write_text(json.dumps(selection), encoding="utf-8")
            result = session_audit.audit(rollout, selected)
        self.assertFalse(result["upstream_invalid_response"])
        self.assertEqual(200, result["declared_context_window"])


if __name__ == "__main__":
    unittest.main()
