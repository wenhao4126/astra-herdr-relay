import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_relay import m


class FinalReviewProviderTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(m, "event"))
        self.enterContext(patch.object(m, "REVIEWS", Path(self.enterContext(tempfile.TemporaryDirectory()))))

    def test_normalizes_complete_ocr_comments(self):
        result = m.normalize_ocr_result({
            "status": "success",
            "comments": [{"path": "a.py", "content": "bug", "start_line": 4,
                          "end_line": 5, "category": "correctness", "severity": "high",
                          "suggestion_code": "fix()"}],
            "manifest": {"files": 1},
        }, duration=1.2)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["findings"][0]["file"], "a.py")
        self.assertEqual(result["findings"][0]["severity"], "high")

    def test_official_complete_status_is_success_candidate(self):
        result=m.normalize_ocr_result({"status":"complete","comments":[]})
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["metadata"]["raw_status"], "complete")

    def test_partial_is_not_pass(self):
        self.assertEqual(m.normalize_ocr_result({"status": "completed_with_warnings", "comments": []})["status"], "PARTIAL")
        self.assertEqual(m.normalize_ocr_result({"status": "partial", "comments": []})["status"], "PARTIAL")
        self.assertEqual(m.normalize_ocr_result({"status": "failed", "comments": []})["status"], "ERROR")
        self.assertEqual(m.normalize_ocr_result({"status": "complete", "comments": []})["status"], "COMPLETE")

    def test_ocr_command_failure_is_error(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, root+"\n", "")), \
             patch.object(m, "run", return_value=subprocess.CompletedProcess([], 2, "", "network")):
            result = m.run_ocr("relay-test", "任务", Path(root), timeout=1)
        self.assertEqual(result["status"], "ERROR")

    def test_malformed_json_is_error(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, root+"\n", "")), \
             patch.object(m, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(m, "read_json", return_value=None):
            result = m.run_ocr("relay-test", "任务", Path(root), timeout=1)
        self.assertEqual(result["status"], "ERROR")

    def test_final_review_does_not_dispatch_codex_and_passes_ocr_to_jev(self):
        ocr = {"source": "opencode-review", "provider": "ocr", "status": "COMPLETE",
               "findings": [{"severity": "medium", "message": "note"}]}
        with patch.object(m, "run_ocr", return_value=ocr), \
             patch.object(m, "review", return_value={"route": "PASS"}) as review:
            result = m.final_review("relay-test", "worker", "任务", "证据", ".")
        self.assertEqual(result["route"], "PASS")
        self.assertEqual(result["ocr"]["status"], "COMPLETE")
        self.assertEqual(len(result["ocr"]["findings"]), 1)
        self.assertIn("OCR ReviewResult", review.call_args.args[3])
        self.assertTrue(review.call_args.kwargs["final_gate"])

    def test_nonblocking_finding_requires_jev_acceptance(self):
        ocr={"status":"COMPLETE","findings":[{"severity":"low","message":"说明待补充"}]}
        for jev_route, expected in (("ACCEPT_WITH_NOTES","ACCEPT_WITH_NOTES"),("PASS","PASS")):
            with self.subTest(jev_route=jev_route), patch.object(m,"run_ocr",return_value=ocr), \
                 patch.object(m,"review",return_value={"route":jev_route}):
                result=m.final_review("relay-notes","worker","任务","证据",".")
            self.assertEqual(result["review_outcome"]["decision"],expected)
            self.assertEqual(len(result["review_outcome"]["notes"]),1 if expected=="ACCEPT_WITH_NOTES" else 0)

    def test_blocking_finding_remains_rework(self):
        ocr={"status":"COMPLETE","findings":[{"severity":"high","message":"缺少功能"}]}
        with patch.object(m,"run_ocr",return_value=ocr), \
             patch.object(m,"review",return_value={"route":"REWORK"}):
            result=m.final_review("relay-blocking","worker","任务","证据",".")
        self.assertEqual(result["review_outcome"]["decision"],"REWORK")

    def test_ocr_error_is_consumed_by_jev_but_cannot_pass(self):
        with patch.object(m, "run_ocr", return_value={"status": "ERROR", "findings": []}), \
             patch.object(m, "review", return_value={"route": "PASS"}) as review:
            result = m.final_review("relay-test", "worker", "任务", "证据", ".")
        self.assertEqual(result["route"], "DEEP_REVIEW")
        self.assertEqual(result["review_outcome"]["decision"],"DEEP_REVIEW")
        self.assertIn('"status": "ERROR"', review.call_args.args[3])

    def test_ocr_error_does_not_store_token(self):
        secret = "sk-sensitive-secret"
        with tempfile.TemporaryDirectory() as root, \
             patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, root+"\n", "")), \
             patch.object(m, "run", return_value=subprocess.CompletedProcess([], 2, secret, secret)):
            result = m.run_ocr("relay-test", "任务", Path(root), timeout=1)
        self.assertNotIn(secret, json.dumps(result))

    def test_nonstandalone_repo_fails_before_ocr(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "父仓库"
            child = parent / "子项目"
            child.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(parent)], check=True)
            with patch.object(m, "run") as run:
                result = m.run_ocr("relay-test", "任务", child)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("独立 Git 仓库", result["summary"])
        run.assert_not_called()

    def test_ready_state_requires_four_pass_artifacts(self):
        from test_event_regression import EventRegressionTests
        EventRegressionTests.setUp(self)
        task_id="relay-evidence"
        m.write_json(m.TASKS/f"{task_id}.json",{"task_id":task_id,"agent":"worker",
                     "dispatch_mode":"opaque_async","phase":"RUNNING"})
        artifacts={key:{"status":"PASS"} for key in
                   ("development_review","test_result","build_result","lint_result")}
        self.assertEqual(m.record_final_evidence(task_id,artifacts)["phase"],"READY_FOR_FINAL")
        self.assertEqual(m.read_json(m.TASKS/f"{task_id}.json",{})["phase"],"READY_FOR_FINAL")
        with self.assertRaises(ValueError):
            m.record_final_evidence(task_id,dict(artifacts,lint_result={"status":"FAIL"}))

    def test_record_final_evidence_keeps_legacy_claude_review_compatibility(self):
        from test_event_regression import EventRegressionTests
        EventRegressionTests.setUp(self)
        task_id="relay-legacy-evidence"
        m.write_json(m.TASKS/f"{task_id}.json",{"task_id":task_id,"agent":"worker",
                     "dispatch_mode":"opaque_async","phase":"RUNNING"})
        artifacts={key:{"status":"PASS"} for key in
                   ("claude_review","test_result","build_result","lint_result")}
        self.assertEqual(m.record_final_evidence(task_id,artifacts)["phase"],"READY_FOR_FINAL")

    def test_record_final_evidence_error_names_general_review_key(self):
        from test_event_regression import EventRegressionTests
        EventRegressionTests.setUp(self)
        task_id="relay-evidence-missing-review"
        m.write_json(m.TASKS/f"{task_id}.json",{"task_id":task_id,"agent":"worker",
                     "dispatch_mode":"opaque_async","phase":"RUNNING"})
        with self.assertRaisesRegex(ValueError,"development_review"):
            m.record_final_evidence(task_id,{"test_result":{"status":"PASS"},
                "build_result":{"status":"PASS"},"lint_result":{"status":"PASS"}})

    def test_development_review_takes_precedence_over_legacy_claude_review(self):
        ready,reason=m.final_gate_evidence_ready({
            "development_review":{"status":"FAIL"},"claude_review":{"status":"PASS"},
            "test_result":{"status":"PASS"},"build_result":{"status":"PASS"},"lint_result":{"status":"PASS"}})
        self.assertFalse(ready)
        self.assertIn("development_review",reason)

    def test_attempts_accumulate_across_rework_tasks(self):
        from test_event_regression import EventRegressionTests
        EventRegressionTests.setUp(self)
        m.write_json(m.TASKS/"relay-parent.json",{"task_id":"relay-parent",
                     "final_state":"REWORK","final_review_attempts":2})
        self.assertEqual(m.final_review_attempt_number({"task_id":"relay-child",
                         "parent_task_id":"relay-parent"}),3)

    def test_disabled_gate_preserves_legacy_review(self):
        from test_finalize_regression import FinalizeRegressionTests
        FinalizeRegressionTests.setUp(self)
        FinalizeRegressionTests.prepare(self)
        with patch.object(m,"FINAL_REVIEW_ENABLED",False), \
             patch.object(m,"review",return_value={"route":"PASS"}) as legacy, \
             patch.object(m,"final_review") as gate:
            result=m.finalize(self.task_id)
        self.assertEqual(result["final_state"],"DONE")
        legacy.assert_called_once()
        gate.assert_not_called()

    def test_finalize_uses_ready_gate_and_returns_control_to_astra(self):
        from test_finalize_regression import FinalizeRegressionTests
        FinalizeRegressionTests.setUp(self)
        FinalizeRegressionTests.prepare(self)
        artifacts={key:{"status":"PASS"} for key in
                   ("development_review","test_result","build_result","lint_result")}
        m.record_final_evidence(self.task_id,artifacts)
        with patch.object(m,"FINAL_REVIEW_ENABLED",True), \
             patch.object(m,"final_review",return_value={"route":"REWORK"}) as gate:
            result=m.finalize(self.task_id)
        self.assertEqual(result["final_state"],"REWORK",result)
        self.assertEqual(result["final_review_attempts"],1)
        gate.assert_called_once()

    def test_finalize_accepts_notes_and_keeps_review_outcome(self):
        from test_finalize_regression import FinalizeRegressionTests
        FinalizeRegressionTests.setUp(self)
        FinalizeRegressionTests.prepare(self)
        artifacts={key:{"status":"PASS"} for key in
                   ("development_review","test_result","build_result","lint_result")}
        m.record_final_evidence(self.task_id,artifacts)
        outcome={"decision":"ACCEPT_WITH_NOTES","notes":[{"message":"小问题"}]}
        with patch.object(m,"FINAL_REVIEW_ENABLED",True), \
             patch.object(m,"final_review",return_value={"route":"ACCEPT_WITH_NOTES","review_outcome":outcome}):
            result=m.finalize(self.task_id)
        self.assertEqual(result["final_state"],"DONE")
        self.assertEqual(result["review_outcome"],outcome)

    def test_finalize_limit_escalates_without_running_ocr(self):
        from test_finalize_regression import FinalizeRegressionTests
        FinalizeRegressionTests.setUp(self)
        FinalizeRegressionTests.prepare(self)
        artifacts={key:{"status":"PASS"} for key in
                   ("development_review","test_result","build_result","lint_result")}
        m.record_final_evidence(self.task_id,artifacts)
        with patch.object(m,"FINAL_REVIEW_ENABLED",True), \
             patch.object(m,"FINAL_REVIEW_MAX_REWORKS",2), \
             patch.object(m,"final_review_attempt_number",return_value=4), \
             patch.object(m,"final_review") as gate:
            result=m.finalize(self.task_id)
        self.assertEqual(result["final_state"],"ESCALATE")
        self.assertEqual(result["review_outcome"]["decision"],"ESCALATE")
        gate.assert_not_called()

    def test_finalize_refuses_gate_without_ready_evidence(self):
        from test_finalize_regression import FinalizeRegressionTests
        FinalizeRegressionTests.setUp(self)
        FinalizeRegressionTests.prepare(self)
        with patch.object(m,"FINAL_REVIEW_ENABLED",True), \
             patch.object(m,"final_review") as gate:
            result=m.finalize(self.task_id)
        self.assertEqual(result["final_state"],"DEEP_REVIEW")
        gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
