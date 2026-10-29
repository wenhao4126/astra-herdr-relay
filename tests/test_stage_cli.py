import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
import artifact
import doc_registry
import evidence
import relay as m
from task_contract import canonical_digest
from test_task_contract import make_task_v2


class StageCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.state = self.home / ".local/state/astra-herdr-relay"
        self.tasks = self.state / "tasks"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "code.py").write_text("answer = 42\n")
        self.task = make_task_v2(self.repo)
        self.task["evidence_policy"]["review"] = ["tests"]
        self.task["evidence_policy"]["final"] = ["tests", "review"]
        self.task_file = self.base / "task.json"
        self.task_file.write_text(json.dumps(self.task), encoding="utf-8")
        self.files_file = self.base / "files.json"
        self.files_file.write_text('["code.py"]', encoding="utf-8")
        self.author = {"producer_id": "executor-1", "producer_role": "fixer",
                       "session_id": "herdr-session-1", "tab_id": "tab-1", "pane_id": "pane-1"}
        self.tasks.mkdir(parents=True)
        runtime_id = "relay-runtime-1"
        (self.tasks / (runtime_id + ".json")).write_text(json.dumps({
            "task_id": runtime_id, "task_sha256": canonical_digest(self.task),
            "task_package": self.task, "dispatch_confirmed": True,
            "author_instance": self.author}), encoding="utf-8")
        self.runtime_id = runtime_id
        self.patchers = [patch.object(m, "STATE", self.state),
                         patch.object(m, "TASKS", self.tasks),
                         patch.object(m, "EVENTS", self.state / "events.jsonl"),
                         patch.object(m, "SEMANTIC", self.state / "semantic"),
                         patch.object(m, "REVIEWS", self.state / "reviews"),
                         patch.object(m, "REGISTRY", self.state / "agents.json"),
                         patch.object(m, "LOCKS", self.state / "locks"),
                         patch.object(m, "REQUESTS", self.state / "requests"),
                         patch.object(m, "FINAL_REVIEW_ENABLED", False)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = m.main(args)
        return code, json.loads(output.getvalue())

    def test_help_lists_all_v2_evidence_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            m.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        for command in ("record-doc", "capture-artifact", "record-evidence",
                        "validate-stage", "evaluate-final"):
            self.assertIn(command, output.getvalue())
        self.assertIn("登记 Context7 资料声明", output.getvalue())

    def test_capture_artifact_cli_uses_relay_authored_instance_and_prints_digest(self):
        code, result = self.run_cli(["capture-artifact", "--task-file", str(self.task_file),
                                     "--files-file", str(self.files_file),
                                     "--runtime-task-id", self.runtime_id])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["author_instance"]["producer_id"], "executor-1")
        self.assertRegex(result["artifact_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(result["delivery_scope"], "only_declared_files_not_repository_correctness")

    def test_record_doc_cli_returns_computed_digest_and_declared_trust_boundary(self):
        record_file = self.base / "doc.json"
        record_file.write_text(json.dumps({
            "id": "cli-doc", "question": "接口契约问题",
            "library_id": "/typesafe-docs/typesafeai", "project_version": "0.5.0",
            "source_url": "https://github.com/typesafe-docs/typesafeai/blob/main/docs/04-primitives/README.md",
            "excerpt": "相关摘录", "conclusion": "范围内结论", "limitations": "不验证语义",
            "retrieved_at": "2026-10-28T23:42:33Z", "status": "verified",
        }), encoding="utf-8")
        code, result = self.run_cli(["record-doc", "--json-file", str(record_file)])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["content_sha256"], doc_registry.content_digest(
            json.loads(record_file.read_text(encoding="utf-8"))))
        self.assertFalse(result["semantic_verified"])

    def test_evaluate_final_calls_existing_review_interface_without_reason_fields(self):
        snapshot = artifact.capture_artifact(self.task, ["code.py"], self.state, self.author)
        self.add_report("tests", "passed", "test-report")
        self.add_report("review", "completed", "review-report", producer_role="reviewer",
                        producer_id="reviewer-1", session_id="herdr-session-2")
        with patch.object(m, "review", return_value={"route": "PASS"}) as reviewer:
            code, result = self.run_cli(["evaluate-final", "--task-file", str(self.task_file)])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["route"], "PASS")
        self.assertTrue(result["ready_for_next_stage"])
        self.assertTrue(reviewer.call_args.kwargs["final_gate"])
        self.assertFalse(reviewer.call_args.kwargs["include_reasons"])
        self.assertIn(snapshot["artifact_sha256"], reviewer.call_args.args[3])

    def test_v2_legacy_final_evidence_cannot_open_new_final_gate(self):
        artifact.capture_artifact(self.task, ["code.py"], self.state, self.author)
        self.add_report("tests", "passed", "legacy-tests")
        self.add_report("review", "completed", "legacy-review", producer_role="reviewer",
                        producer_id="reviewer-1", session_id="herdr-session-2")
        legacy_state = {"task_id": self.task["task_id"], "dispatch_mode": "opaque_async",
                        "task_package": self.task, "phase": "RUNNING"}
        (self.tasks / (self.task["task_id"] + ".json")).write_text(
            json.dumps(legacy_state), encoding="utf-8")
        evidence_file = self.base / "legacy-final.json"
        evidence_file.write_text(json.dumps({
            "development_review": {"status": "PASS"},
            "test_result": {"status": "PASS"},
            "build_result": {"status": "PASS"},
            "lint_result": {"status": "PASS"},
        }), encoding="utf-8")
        code, result = self.run_cli(["record-final-evidence", self.task["task_id"],
                                     "--json-file", str(evidence_file)])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["mode"], "legacy-record-only")
        state = json.loads((self.tasks / (self.task["task_id"] + ".json")).read_text())
        self.assertNotEqual(state.get("phase"), "READY_FOR_FINAL")
        code, stage = self.run_cli(["validate-stage", "--task-file", str(self.task_file),
                                    "--stage", "final"])
        self.assertEqual(code, 1)
        self.assertIn("JEV_DECISION_REQUIRED", [error["code"] for error in stage["errors"]])

    def test_evaluate_final_preserves_jev_failure_as_unknown_without_retry(self):
        artifact.capture_artifact(self.task, ["code.py"], self.state, self.author)
        self.add_report("tests", "passed", "failed-jev-tests")
        self.add_report("review", "completed", "failed-jev-review", producer_role="reviewer",
                        producer_id="reviewer-1", session_id="herdr-session-2")
        with patch.object(m, "review", return_value={"route": "DEEP_REVIEW",
                                                       "error": "provider unavailable"}) as reviewer:
            code, first = self.run_cli(["evaluate-final", "--task-file", str(self.task_file)])
        self.assertEqual(code, 1)
        self.assertEqual(first["route"], "UNKNOWN")
        with patch.object(m, "review", return_value={"route": "PASS"}) as retry:
            code, second = self.run_cli(["evaluate-final", "--task-file", str(self.task_file)])
        self.assertEqual(code, 1)
        self.assertTrue(second["cached"])
        reviewer.assert_called_once()
        retry.assert_not_called()

    def test_v2_ocr_requirement_comes_only_from_task_policy(self):
        artifact.capture_artifact(self.task, ["code.py"], self.state, self.author)
        self.add_report("tests", "passed", "policy-tests")
        self.add_report("review", "completed", "policy-review", producer_role="reviewer",
                        producer_id="reviewer-1", session_id="herdr-session-2")
        with patch.object(m, "FINAL_REVIEW_ENABLED", True):
            code, result = self.run_cli(["validate-stage", "--task-file", str(self.task_file),
                                         "--stage", "final"])
        self.assertEqual(code, 1)
        self.assertNotIn("ocr_review", result["gaps"])
        self.assertIn("JEV_DECISION_REQUIRED", [error["code"] for error in result["errors"]])

    def test_final_jev_failure_log_does_not_expose_provider_error_text(self):
        secret = "sk-sensitive-provider-detail"
        saved = []
        with patch.object(m, "jev_call", side_effect=RuntimeError(secret)), \
             patch.object(m, "write_json", side_effect=lambda _path, value: saved.append(value)), \
             patch.object(m, "event", side_effect=lambda _name, **value: saved.append(value)):
            result = m.review("relay-v2", "worker-1", "任务", "证据",
                              final_gate=True, include_reasons=False)
        self.assertEqual(result["error"], "Jev Review 接口评估失败")
        self.assertNotIn(secret, json.dumps([result, *saved], ensure_ascii=False))

    def add_report(self, kind, status, name, *, producer_role="tester",
                   producer_id=None, session_id=None):
        path = self.repo / "reports" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"kind": kind, "status": status}) + "\n", encoding="utf-8")
        record = {"id": name + "-evidence", "task_sha256": canonical_digest(self.task),
                  "artifact_sha256": artifact.verify_current_snapshot(self.task, self.state)["artifact_sha256"],
                  "kind": kind, "producer_id": producer_id or name,
                  "producer_role": producer_role, "session_id": session_id or name + "-session",
                  "status": status, "report_path": "reports/" + name + ".json",
                  "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "created_at": "2026-10-28T23:42:33Z"}
        evidence.record_evidence(record, self.state)


if __name__ == "__main__":
    unittest.main()
