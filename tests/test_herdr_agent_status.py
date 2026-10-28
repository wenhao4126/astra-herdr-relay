import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_relay import m


class HerdrAgentStatusTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for key in ('STATE', 'REGISTRY', 'TASKS', 'EVENTS', 'SEMANTIC', 'REVIEWS', 'LOCKS'):
            self.enterContext(patch.object(m, key, root / key))
        m.save_registry({'agents': {'worker': {'lifecycle': 'IDLE'}}})
        m.ensure()
        self.markers = []
        self.enterContext(patch.object(m, 'run', side_effect=self.fake_run))

    def fake_run(self, cmd, **kwargs):
        if cmd[2] == 'prompt':
            self.markers.append(cmd[4].splitlines()[0])
            return subprocess.CompletedProcess(cmd, 0, '', '')
        return subprocess.CompletedProcess(cmd, 0, '\n'.join(self.markers + ['验证证据', self.markers[-1]]), '')

    def test_sensor_maps_agent_status_done_without_jev(self):
        with patch.object(m, "agent_status_obj", return_value={
            "result": {"agent": {"agent_status": "done"}}
        }), patch.object(m, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(m, "jev_call") as jev:
            result = m.sensor("worker", force=True)
        self.assertEqual(result["runtime_status"], "done")
        self.assertEqual(result["semantic_status"], "COMPLETED")
        jev.assert_not_called()

    def test_delegate_accepts_agent_status_done_and_reviews(self):
        with patch.object(m, "agent_status_obj", return_value={
            "result": {"agent": {"agent_status": "done"}}
        }), patch.object(m, "review", return_value={"route": "PASS"}) as review:
            state = m.delegate("worker", "完成并检查任务")
        self.assertEqual(state["final_state"], "DONE")
        self.assertIn("验证证据", review.call_args.args[3])

    def test_async_sync_settles_agent_status_done_after_working(self):
        task = m.dispatch_opaque("worker", "异步任务")
        def snapshot(status):
            return {"result": {"agents": [{"name": "worker", "agent_status": status}]}}
        with patch.object(m, "herdr_json", return_value=snapshot("working")):
            m.sync()
        with patch.object(m, "herdr_json", return_value=snapshot("done")):
            m.sync()
        state = m.read_json(m.TASKS / f"{task['task_id']}.json", {})
        self.assertEqual(state["phase"], "AWAITING_REVIEW")
        self.assertEqual(m.registry()["agents"]["worker"]["lifecycle"], "IDLE")
        self.assertFalse((m.LOCKS / "worker.lock").exists())

    def test_agent_status_is_preferred_and_legacy_fields_remain_supported(self):
        self.assertEqual(m.runtime_status({"agent_status": "idle", "status": "working"}), "idle")
        self.assertEqual(m.runtime_status({"agent_status": "working"}), "working")
        self.assertEqual(m.runtime_status({"status": "done"}), "done")
        self.assertEqual(m.runtime_status({"state": "blocked"}), "blocked")
        self.assertEqual(m.runtime_status({}), "unknown")

if __name__ == "__main__":
    unittest.main()
