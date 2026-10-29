import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
try:
    import artifact
except ImportError:
    artifact = None
try:
    import evidence
except ImportError:
    evidence = None

from test_task_contract import make_task_v2


class EvidenceRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        (self.root / "code.py").write_text("answer = 42\n")
        self.state = self.base / "state"
        self.task = make_task_v2(self.root)
        self.assertIsNotNone(evidence, "需要本地 evidence 模块")
        self.snapshot = artifact.capture_artifact(
            self.task, ["code.py"], self.state,
            {"producer_id": "executor-1", "producer_role": "executor",
             "session_id": "session-executor", "tab_id": "tab-1", "pane_id": "pane-1"})
        self.report = self.root / "reports/review.json"
        self.report.parent.mkdir()
        self.report.write_text('{"findings":[]}\n')

    def record(self, *, kind="review", producer_id="reviewer-1", session_id="session-reviewer",
               status="completed", report_path="reports/review.json"):
        role = "reviewer" if kind == "review" else "ocr" if kind == "ocr_review" else "tester"
        return {
            "id": f"evidence-{kind}-{producer_id}",
            "task_sha256": self.snapshot["task_sha256"],
            "artifact_sha256": self.snapshot["artifact_sha256"],
            "kind": kind,
            "producer_id": producer_id,
            "producer_role": role,
            "session_id": session_id,
            "status": status,
            "report_path": report_path,
            "report_sha256": hashlib.sha256(self.report.read_bytes()).hexdigest(),
            "created_at": "2026-10-28T23:42:33Z",
        }

    def test_records_review_completion_but_requires_distinct_author_and_session(self):
        saved = evidence.record_evidence(self.record(), self.state)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(evidence.load_evidence_records(
            self.snapshot["task_sha256"], self.snapshot["artifact_sha256"], self.state)[0]["id"],
            saved["id"])
        with self.assertRaises(evidence.EvidenceError):
            evidence.record_evidence(self.record(producer_id="executor-1"), self.state)
        same_session = self.record(producer_id="reviewer-2", session_id="session-executor")
        with self.assertRaises(evidence.EvidenceError):
            evidence.record_evidence(same_session, self.state)

    def test_report_digest_is_checked_and_changed_report_invalidates_saved_evidence(self):
        saved = evidence.record_evidence(self.record(kind="tests", producer_id="tester-1",
                                                     status="passed"), self.state)
        self.assertEqual(saved["status"], "passed")
        self.report.write_text('{"findings":["changed"]}\n')
        with self.assertRaises(evidence.EvidenceError):
            evidence.load_evidence_records(self.snapshot["task_sha256"],
                                           self.snapshot["artifact_sha256"], self.state)

    def test_wrong_report_digest_and_naive_timestamp_are_rejected(self):
        bad = self.record()
        bad["report_sha256"] = "0" * 64
        with self.assertRaises(evidence.EvidenceError):
            evidence.record_evidence(bad, self.state)
        bad = self.record(producer_id="reviewer-2")
        bad["created_at"] = "2026-10-28T23:42:33"
        with self.assertRaises(evidence.EvidenceError):
            evidence.record_evidence(bad, self.state)

    def test_ocr_evidence_requires_complete_normalized_ocr_report(self):
        report = self.record(kind="ocr_review", producer_id="ocr-1", session_id="ocr-session")
        self.report.write_text('{"status":"PARTIAL","findings":[]}\n')
        report["report_sha256"] = hashlib.sha256(self.report.read_bytes()).hexdigest()
        with self.assertRaises(evidence.EvidenceError):
            evidence.record_evidence(report, self.state)
        self.report.write_text('{"status":"COMPLETE","findings":[]}\n')
        report["report_sha256"] = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.assertEqual(evidence.record_evidence(report, self.state)["kind"], "ocr_review")


if __name__ == "__main__":
    unittest.main()
