import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
import artifact
import evidence
import stages
import test_event_regression
from test_relay import approve_test_task, fake_herdr_json, m, seed_agent
from test_task_contract import make_task_v2
from gates import GateError


class V2DelegateGuardTests(unittest.TestCase):
    def setUp(self):
        test_event_regression.EventRegressionTests.setUp(self)
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "code.py").write_text("answer = 42\n", encoding="utf-8")
        self.task = make_task_v2(self.repo)

    def test_v2_delegate_rejects_before_approval_registry_or_external_side_effects(self):
        checks = ("_require_task_approval", "request_claim", "registry", "registry_transaction",
                  "write_json", "event", "herdr_json", "run", "review", "agent_status_obj")
        with contextlib.ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(m, name)) for name in checks}
            approval = stack.enter_context(patch.object(m.gates, "require_approval"))
            provider = stack.enter_context(patch.object(m.gates, "http_request"))
            with self.assertRaises(GateError) as raised:
                m.delegate("worker", self.task, request_id="req-v2-sync-rejected")
        self.assertEqual(raised.exception.code, "V2_ASYNC_REQUIRED")
        self.assertIn("dispatch-task", str(raised.exception))
        self.assertIn("dispatch-opaque", str(raised.exception))
        for name, mock in mocks.items():
            mock.assert_not_called()
        approval.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(list(m.STATE.iterdir()), [],
                         "拒绝 v2 同步入口时不得创建 Relay 状态或 DONE 任务记录")

    def test_v2_delegate_cli_returns_structured_nonzero_without_task_record(self):
        task_file = self.base / "v2-task.json"
        task_file.write_text(json.dumps(self.task), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = m.main(["delegate", "worker", "--task-file", str(task_file)])
        self.assertNotEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error"]["code"], "V2_ASYNC_REQUIRED")
        self.assertFalse(m.TASKS.exists())

    def test_astra_delegate_wrapper_returns_structured_nonzero_for_v2(self):
        home = self.base / "fake-home"
        installed = home / ".local/lib/astra-herdr-relay"
        installed.mkdir(parents=True)
        for source in (Path(__file__).parents[1] / "lib").glob("*.py"):
            shutil.copyfile(source, installed / source.name)
        task_file = self.base / "wrapper-task.json"
        task_file.write_text(json.dumps(self.task), encoding="utf-8")
        env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1",
                   PATH=str(Path(sys.executable).parent) + ":/usr/bin:/bin")
        wrapper = Path(__file__).parents[1] / "bin/astra-delegate"
        result = subprocess.run([str(wrapper), "worker", "--task-file", str(task_file)],
                                capture_output=True, text=True, env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        response = json.loads(result.stdout)
        self.assertEqual(response["error"]["code"], "V2_ASYNC_REQUIRED")
        self.assertFalse((home / ".local/state/astra-herdr-relay/tasks").exists())
        self.assertFalse((home / ".local/state/astra-herdr-relay/agents.json").exists())

    def _prepare_async_v2(self):
        seed_agent(cwd=str(self.repo.resolve()), herdr_session=m.HERDR_SESSION,
                   tab_id="v2-tab", pane_id="v2-pane", created_at=20261029)
        approve_test_task(self.task, m.STATE)
        output = {"text": ""}

        def run(cmd, **_kwargs):
            if "prompt" in cmd:
                prompt = cmd[cmd.index("prompt") + 2]
                marker = re.search(r"^\[RELAY TASK [^\]]+\]$", prompt, re.M).group(0)
                task_id = marker[len("[RELAY TASK "):-1]
                output["text"] = (marker + "\n[RELAY FINISHED " + task_id +
                                  "]\n本次异步任务结果")
            return subprocess.CompletedProcess(cmd, 0, output["text"], "")

        self.enterContext(patch.object(m, "run", side_effect=run))
        self.enterContext(patch.object(m, "herdr_json", side_effect=fake_herdr_json))
        self.enterContext(patch.object(m, "agent_status_obj", return_value={
            "result": {"agent": {"name": "worker", "tab_id": "v2-tab", "pane_id": "v2-pane",
                                  "agent_status": "done"}}
        }))
        dispatched = m.dispatch_opaque("worker", self.task, request_id="req-v2-async-dispatch")
        self.assertTrue(dispatched["accepted"], dispatched)
        task_state = m.read_json(m.TASKS / (dispatched["task_id"] + ".json"), {})
        self.assertTrue(task_state["dispatch_confirmed"])
        snapshot = artifact.capture_artifact(
            self.task, ["code.py"], m.STATE, task_state["author_instance"])
        self._record("tests", "passed", snapshot["task_sha256"], snapshot["artifact_sha256"],
                     "tests-v2", "tester", "session-tests")
        self._record("review", "completed", snapshot["task_sha256"], snapshot["artifact_sha256"],
                     "review-v2", "reviewer", "session-reviewer")
        return dispatched["task_id"], snapshot

    def _record(self, kind, status, task_sha, artifact_sha, suffix, role, session):
        report = self.repo / "reports" / (suffix + ".json")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"kind": kind, "status": status}) + "\n", encoding="utf-8")
        evidence.record_evidence({
            "id": suffix + "-record", "task_sha256": task_sha,
            "artifact_sha256": artifact_sha, "kind": kind, "producer_id": suffix,
            "producer_role": role, "session_id": session, "status": status,
            "report_path": "reports/" + report.name,
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "created_at": "2026-10-29T13:00:00+08:00",
        }, m.STATE)

    def test_async_v2_finalize_with_final_jev_waits_for_principal_and_never_done(self):
        task_id, _snapshot = self._prepare_async_v2()
        evaluated = stages.evaluate_final(self.task, m.STATE,
                                          lambda _context: {"route": "PASS"})
        self.assertTrue(evaluated["ready_for_next_stage"], evaluated)
        with patch.object(m, "review") as legacy_review:
            result = m.finalize(task_id)
        self.assertEqual(result["final_state"], "AWAITING_PRINCIPAL", result)
        self.assertEqual(m.read_json(m.TASKS / (task_id + ".json"), {})["final_state"],
                         "AWAITING_PRINCIPAL")
        self.assertNotEqual(result["final_state"], "DONE")
        legacy_review.assert_not_called()

    def test_async_v2_finalize_without_final_jev_cannot_mark_done(self):
        task_id, _snapshot = self._prepare_async_v2()
        result = m.finalize(task_id)
        self.assertEqual(result["final_state"], "HUMAN_REVIEW", result)
        self.assertIn("JEV_DECISION_REQUIRED",
                      [error["code"] for error in result["stage_validation"]["errors"]])
        self.assertNotEqual(result["final_state"], "DONE")


if __name__ == "__main__":
    unittest.main()
