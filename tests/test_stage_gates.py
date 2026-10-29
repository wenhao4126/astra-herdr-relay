import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
import artifact
import evidence
try:
    import stages
except ImportError:
    stages = None

from test_task_contract import make_task_v2


class StageGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        (self.root / "code.py").write_text("answer = 42\n")
        self.state = self.base / "state"
        self.task = make_task_v2(self.root)
        self.assertIsNotNone(stages, "需要本地 stages 模块")
        self.snapshot = artifact.capture_artifact(
            self.task, ["code.py"], self.state,
            {"producer_id": "executor-1", "producer_role": "executor",
             "session_id": "session-executor", "tab_id": "tab-1", "pane_id": "pane-1"})

    def add_evidence(self, kind, status, suffix, *, producer_id=None, session_id=None):
        report = self.root / f"reports/{suffix}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"kind": kind, "status": status}, sort_keys=True) + "\n")
        role = "reviewer" if kind == "review" else "ocr" if kind == "ocr_review" else "tester"
        record = {
            "id": f"{suffix}-evidence", "task_sha256": self.snapshot["task_sha256"],
            "artifact_sha256": self.snapshot["artifact_sha256"], "kind": kind,
            "producer_id": producer_id or f"{suffix}-producer", "producer_role": role,
            "session_id": session_id or f"{suffix}-session", "status": status,
            "report_path": f"reports/{suffix}.json",
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "created_at": "2026-10-28T23:42:33Z",
        }
        return evidence.record_evidence(record, self.state)

    def test_review_stage_needs_snapshot_and_execution_evidence_but_not_review_evidence(self):
        self.add_evidence("tests", "passed", "tests")
        result = stages.validate_stage(self.task, "review", self.state, approved=True)
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["ready_for_next_stage"], result)
        self.assertEqual(result["stage"], "review")

    def test_final_requires_independent_review_and_relay_jev_not_user_reported_route(self):
        self.add_evidence("tests", "passed", "tests")
        self.add_evidence("review", "completed", "review")
        missing = stages.validate_stage(self.task, "final", self.state)
        self.assertFalse(missing["ready_for_next_stage"])
        self.assertIn("JEV_DECISION_REQUIRED", [error["code"] for error in missing["errors"]])
        result = stages.evaluate_final(
            self.task, self.state,
            lambda context: {"route": "PASS", "rework_reason": "不可写入用户摘要"})
        self.assertTrue(result["valid"], result)
        checked = stages.validate_stage(self.task, "final", self.state)
        self.assertTrue(checked["ready_for_next_stage"], checked)
        self.assertEqual(checked["decision"], "PASS")
        self.assertNotIn("rework_reason", json.dumps(result))

    def test_new_evidence_invalidates_previous_jev_decision(self):
        self.add_evidence("tests", "passed", "tests")
        self.add_evidence("review", "completed", "review")
        calls = []
        evaluate = lambda _context: calls.append("jev") or {"route": "PASS"}
        first = stages.evaluate_final(self.task, self.state, evaluate)
        self.assertTrue(first["ready_for_next_stage"], first)
        self.add_evidence("lint", "passed", "lint-after-jev")
        stale = stages.validate_stage(self.task, "final", self.state)
        self.assertFalse(stale["ready_for_next_stage"], stale)
        self.assertIn("JEV_DECISION_REQUIRED", [error["code"] for error in stale["errors"]])
        second = stages.evaluate_final(self.task, self.state, evaluate)
        self.assertTrue(second["ready_for_next_stage"], second)
        self.assertEqual(len(calls), 2)

    def test_final_jev_is_bound_to_complete_author_instance_identity(self):
        self.add_evidence("tests", "passed", "identity-tests")
        self.add_evidence("review", "completed", "identity-review")
        original = dict(self.snapshot["author_instance"])
        first = stages.evaluate_final(self.task, self.state, lambda _context: {"route": "PASS"})
        self.assertTrue(first["ready_for_next_stage"], first)
        original_snapshot = artifact.verify_current_snapshot(self.task, self.state)
        expected_digest = hashlib.sha256(json.dumps(
            original, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        saved = json.loads(next((self.state / "final_jev").glob("*.json")).read_text())
        self.assertEqual(saved["author_identity_sha256"], expected_digest)

        identity_changes = {
            "producer_id": "executor-2",
            "producer_role": "fixer",
            "session_id": "session-executor-replaced",
            "tab_id": "tab-replaced",
            "pane_id": "pane-replaced",
            "agent_session_value": "herdr-agent-replaced",
        }
        for field, value in identity_changes.items():
            with self.subTest(identity_field=field):
                changed = dict(original)
                changed[field] = value
                recaptured = artifact.capture_artifact(self.task, ["code.py"],
                                                       self.state, changed)
                self.assertEqual(recaptured["artifact_sha256"],
                                 original_snapshot["artifact_sha256"])
                stale = stages.validate_stage(self.task, "final", self.state)
                self.assertIn("JEV_DECISION_REQUIRED",
                              [error["code"] for error in stale["errors"]])
                refreshed = stages.evaluate_final(
                    self.task, self.state, lambda _context: {"route": "PASS"})
                self.assertTrue(refreshed["ready_for_next_stage"], refreshed)

    def test_failed_jev_is_unknown_and_is_not_retried_for_same_digests(self):
        self.add_evidence("tests", "passed", "tests-jev-failed")
        self.add_evidence("review", "completed", "review-jev-failed")
        calls = []
        def fail(_context):
            calls.append("called")
            raise RuntimeError("provider unavailable")
        first = stages.evaluate_final(self.task, self.state, fail)
        second = stages.evaluate_final(self.task, self.state, fail)
        self.assertEqual(first["route"], "UNKNOWN")
        self.assertFalse(first["valid"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)

    def test_ocr_is_optional_when_task_and_global_policy_do_not_require_it(self):
        self.add_evidence("tests", "passed", "tests-no-ocr")
        self.add_evidence("review", "completed", "review-no-ocr")
        result = stages.validate_stage(self.task, "final", self.state)
        self.assertNotIn("ocr_review", result["gaps"])
        self.assertIn("JEV_DECISION_REQUIRED", [error["code"] for error in result["errors"]])

    def test_only_passed_test_status_counts_and_completed_review_is_not_semantic_acceptance(self):
        self.add_evidence("tests", "completed", "tests-completed")
        result = stages.validate_stage(self.task, "review", self.state)
        self.assertFalse(result["valid"], result)
        self.assertFalse(result["ready_for_next_stage"], result)
        self.assertIn("tests", result["gaps"])

    def test_ocr_is_required_only_when_task_policy_declares_it(self):
        self.task["evidence_policy"]["final"].append("ocr_review")
        self.snapshot = artifact.capture_artifact(
            self.task, ["code.py"], self.state,
            {"producer_id": "executor-1", "producer_role": "executor",
             "session_id": "session-executor", "tab_id": "tab-1", "pane_id": "pane-1"})
        self.add_evidence("tests", "passed", "tests-ocr")
        self.add_evidence("review", "completed", "review-ocr")
        result = stages.validate_stage(self.task, "final", self.state)
        self.assertFalse(result["ready_for_next_stage"])
        self.assertIn("ocr_review", result["gaps"])

    def test_changed_task_revision_invalidates_snapshot_and_previous_jev(self):
        self.add_evidence("tests", "passed", "tests")
        self.add_evidence("review", "completed", "review")
        stages.evaluate_final(self.task, self.state, lambda _context: {"route": "PASS"})
        self.task["revision"] += 1
        result = stages.validate_stage(self.task, "final", self.state)
        self.assertFalse(result["valid"])
        self.assertFalse(result["ready_for_next_stage"])

    def test_v1_cannot_enter_new_review_or_final_stage(self):
        legacy = dict(self.task)
        legacy["schema_version"] = 1
        legacy.pop("required_doc_ids")
        legacy["evidence_policy"] = ["旧证据说明"]
        for stage in ("review", "final"):
            result = stages.validate_stage(legacy, stage, self.state)
            self.assertFalse(result["ready_for_next_stage"])
            self.assertIn("SCHEMA_V1_STAGE_UNSUPPORTED", [error["code"] for error in result["errors"]])


if __name__ == "__main__":
    unittest.main()
