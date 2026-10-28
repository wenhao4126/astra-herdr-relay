import subprocess
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
from gates import GateError
import test_event_regression
from test_relay import approve_test_task, m, make_task_package, seed_agent


class TaskFlowTests(unittest.TestCase):
    def setUp(self):
        test_event_regression.EventRegressionTests.setUp(self)
        self.enterContext(patch.object(m, "TYPESAFE_MODEL", "jev-test"))
        self.enterContext(patch.object(m, "HERDR_SESSION", "herdr-session-test"))
        m.ensure()

    def seed_agent(self, name="worker"):
        seed_agent(name,herdr_session="herdr-session-test",tab_id="tab-test",pane_id="pane-test",
                   cwd=str(m.STATE.resolve()))

    def test_free_text_dispatch_is_rejected_before_external_call(self):
        self.seed_agent()
        with patch.object(m, "run") as external:
            with self.assertRaises(GateError) as error:
                m.dispatch_opaque("worker", "自由文本不能绕过任务门禁")
        self.assertEqual(error.exception.code, "TASK_PACKAGE_REQUIRED")
        external.assert_not_called()

    def test_missing_jev_approval_is_rejected_before_herdr(self):
        self.seed_agent()
        task = make_task_package(m.STATE)
        with patch.object(m, "run") as external:
            with self.assertRaises(GateError) as error:
                m.dispatch_opaque("worker", task, request_id="req-not-approved")
        self.assertEqual(error.exception.code, "TASK_GATE_REQUIRED")
        external.assert_not_called()

    def test_approved_dispatch_persists_request_and_deduplicates(self):
        self.seed_agent()
        task = make_task_package(m.STATE)
        approve_test_task(task, m.STATE)
        with patch.object(m, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as external:
            first = m.dispatch_opaque("worker", task, request_id="req-dispatch-1")
            second = m.dispatch_opaque("worker", task, request_id="req-dispatch-1")
        self.assertTrue(first["accepted"])
        self.assertTrue(second["cached"])
        self.assertEqual(external.call_count, 1)
        request = m.read_json(m.STATE / "requests/req-dispatch-1.json", {})
        self.assertEqual(request["status"], "DISPATCHED")
        self.assertEqual(request["task_sha256"], first["task_sha256"])

    def test_dispatch_timeout_is_unknown_and_not_replayed(self):
        self.seed_agent()
        task = make_task_package(m.STATE)
        approve_test_task(task, m.STATE)
        timeout = subprocess.TimeoutExpired("herdr agent prompt", 20)
        with patch.object(m, "run", side_effect=timeout) as external:
            first = m.dispatch_opaque("worker", task, request_id="req-dispatch-timeout")
            second = m.dispatch_opaque("worker", task, request_id="req-dispatch-timeout")
        self.assertEqual(first["status"], "UNKNOWN")
        self.assertEqual(second["status"], "UNKNOWN")
        self.assertEqual(external.call_count, 1)

    def test_spawn_requires_explicit_session_and_real_cwd(self):
        cwd = str(Path(__file__).resolve().parent)
        with patch.object(m, "herdr_json") as external:
            with patch.object(m,"HERDR_SESSION",""):
                with self.assertRaises(GateError) as error:
                    m.spawn("worker", cwd, name="worker")
        self.assertEqual(error.exception.code, "HERDR_SESSION_REQUIRED")
        external.assert_not_called()
        with self.assertRaises(GateError):
            m.spawn("worker", str(Path(cwd) / "missing"), name="worker",
                    session="herdr-session-test")

    def test_spawn_passes_explicit_model_reasoning_and_instance_identity(self):
        cwd = str(Path(__file__).resolve().parent)
        responses = [
            {"result": {"tabs": []}},
            {"result": {"tab": {"tab_id": "tab-new"},
                         "root_pane": {"pane_id": "pane-new"}}},
            {"result": {"agent_status": "idle"}},
        ]
        with patch.object(m, "herdr_json", side_effect=responses) as external:
            record = m.spawn("worker", cwd, name="worker", kind="codex",
                             model="gpt-6-luna", reasoning="max",
                             session="herdr-session-test")
        self.assertEqual(record["herdr_session"], "herdr-session-test")
        self.assertEqual((record["tab_id"], record["pane_id"]), ("tab-new", "pane-new"))
        command = external.call_args_list[2].args[0]
        self.assertEqual(command[-6:], ["--", "--model", "gpt-6-luna", "-c", "model_reasoning_effort=max", "--strict-config"])

    def test_synchronous_rework_returns_to_caller_without_second_prompt(self):
        self.seed_agent()
        task = make_task_package(m.STATE)
        approve_test_task(task, m.STATE)
        captured_prompt = {}
        def fake_run(command,**kwargs):
            if "prompt" in command:
                captured_prompt["text"] = command[command.index("prompt") + 2]
                return subprocess.CompletedProcess(command,0,"","")
            marker=__import__("re").search(r"^\[RELAY TASK [^\]]+\]$",captured_prompt["text"],flags=__import__("re").M).group(0)
            task_id=marker[len("[RELAY TASK "):-1]
            return subprocess.CompletedProcess(command,0,f"{marker}\n[RELAY FINISHED {task_id}]\n完成","")
        with patch.object(m, "run", side_effect=fake_run) as external, \
             patch.object(m, "agent_status_obj", return_value={"result": {"agent": {
                 "name":"worker", "tab_id":"tab-test", "pane_id":"pane-test", "status": "done"}}}), \
             patch.object(m, "review", return_value={"route": "REWORK"}):
            result = m.delegate("worker", task, request_id="req-delegate-1")
        self.assertEqual(result["final_state"], "REWORK")
        self.assertEqual(external.call_count, 2)

    def test_herdr_list_failure_is_unknown_and_not_removed(self):
        self.seed_agent()
        with patch.object(m,"herdr_json",side_effect=RuntimeError("临时查询失败")):
            result=m.sync()
        self.assertEqual(result["status"],"UNKNOWN")
        self.assertEqual(m.registry()["agents"]["worker"]["lifecycle"],"IDLE")

    def test_only_confirmed_missing_agent_becomes_removed_without_recreation(self):
        self.seed_agent()
        empty={"result":{"agents":[]}}
        missing={"error":{"code":"agent_not_found"}}
        with patch.object(m,"STARTUP_GRACE",0),patch.object(m,"agent_status_obj",return_value=missing),patch.object(m,"spawn") as create:
            result=m.sync(snapshot=empty,session="herdr-session-test")
        self.assertEqual(result["changed"],["worker"])
        self.assertEqual(m.registry()["agents"]["worker"]["lifecycle"],"REMOVED")
        create.assert_not_called()

    def test_unknown_agent_get_does_not_become_removed(self):
        self.seed_agent()
        with patch.object(m,"STARTUP_GRACE",0),patch.object(m,"agent_status_obj",return_value=None):
            result=m.sync(snapshot={"result":{"agents":[]}},session="herdr-session-test")
        self.assertEqual(result["changed"],[])
        self.assertEqual(m.registry()["agents"]["worker"]["lifecycle"],"IDLE")

    def test_old_spawn_request_never_returns_stopped_instance_as_success(self):
        cwd=str(Path(__file__).resolve().parent)
        responses=[{"result":{"tabs":[]}},
                   {"result":{"tab":{"tab_id":"tab-new"},"root_pane":{"pane_id":"pane-new"}}},
                   {"result":{"agent":{"name":"worker","status":"idle"}}}]
        with patch.object(m,"herdr_json",side_effect=responses):
            first=m.spawn("worker",cwd,name="worker",session="herdr-session-test",request_id="req-create-stale")
        self.assertEqual(first["lifecycle"],"IDLE")
        stopped={**first,"lifecycle":"STOPPED"}
        m.save_registry({"agents":{"worker":stopped}})
        with patch.object(m,"herdr_json") as external:
            replay=m.spawn("worker",cwd,name="worker",session="herdr-session-test",request_id="req-create-stale")
        self.assertEqual(replay["status"],"REQUEST_STALE_INSTANCE")
        self.assertEqual(replay["error_code"],"REQUEST_STALE_INSTANCE")
        external.assert_not_called()

    def test_old_spawn_request_for_removed_instance_is_stale(self):
        cwd=str(Path(__file__).resolve().parent)
        responses=[{"result":{"tabs":[]}},
                   {"result":{"tab":{"tab_id":"tab-new"},"root_pane":{"pane_id":"pane-new"}}},
                   {"result":{"agent":{"name":"worker","status":"idle"}}}]
        with patch.object(m,"herdr_json",side_effect=responses):
            first=m.spawn("worker",cwd,name="worker",session="herdr-session-test",request_id="req-create-removed")
        m.save_registry({"agents":{"worker":{**first,"lifecycle":"REMOVED"}}})
        with patch.object(m,"herdr_json") as external:
            replay=m.spawn("worker",cwd,name="worker",session="herdr-session-test",request_id="req-create-removed")
        self.assertEqual(replay["status"],"REQUEST_STALE_INSTANCE")
        external.assert_not_called()

    def create_replayable_agent(self, request_id, external_session=None):
        cwd=str(Path(__file__).resolve().parent)
        identity={"name":"worker","tab_id":"tab-new","pane_id":"pane-new","status":"idle"}
        if external_session:
            identity["agent_session"]={"value":external_session}
        responses=[{"result":{"tabs":[]}},
                   {"result":{"tab":{"tab_id":"tab-new"},"root_pane":{"pane_id":"pane-new"}}},
                   {"result":{"agent":identity}}]
        with patch.object(m,"herdr_json",side_effect=responses):
            return m.spawn("worker",cwd,name="worker",session="herdr-session-test",request_id=request_id)

    def test_spawn_replay_requires_herdr_confirmation_of_live_instance(self):
        self.create_replayable_agent("req-create-live")
        observed={"result":{"agent":{"name":"worker","tab_id":"tab-new",
                                       "pane_id":"pane-new","status":"idle"}}}
        with patch.object(m,"herdr_json",return_value=observed) as external:
            replay=m.spawn("worker",str(Path(__file__).resolve().parent),name="worker",
                           session="herdr-session-test",request_id="req-create-live")
        self.assertTrue(replay["cached"])
        external.assert_called_once_with(["agent","get","worker"],timeout=15,session="herdr-session-test")

    def test_spawn_replay_reports_remote_stopped_instance_as_stale(self):
        self.create_replayable_agent("req-create-remote-stopped")
        observed={"result":{"agent":{"name":"worker","tab_id":"tab-new",
                                       "pane_id":"pane-new","status":"stopped"}}}
        with patch.object(m,"herdr_json",return_value=observed) as external:
            replay=m.spawn("worker",str(Path(__file__).resolve().parent),name="worker",
                           session="herdr-session-test",request_id="req-create-remote-stopped")
        self.assertEqual(replay["status"],"REQUEST_STALE_INSTANCE")
        external.assert_called_once()
        self.assertEqual(m.read_json(m.REQUESTS/"req-create-remote-stopped.json",{})["status"],"STALE")

    def test_spawn_replay_reports_replaced_remote_identity_as_stale(self):
        self.create_replayable_agent("req-create-replaced")
        observed={"result":{"agent":{"name":"worker","tab_id":"tab-replacement",
                                       "pane_id":"pane-replacement","status":"idle"}}}
        with patch.object(m,"herdr_json",return_value=observed) as external:
            replay=m.spawn("worker",str(Path(__file__).resolve().parent),name="worker",
                           session="herdr-session-test",request_id="req-create-replaced")
        self.assertEqual(replay["status"],"REQUEST_STALE_INSTANCE")
        external.assert_called_once()

    def test_spawn_replay_compares_herdr_agent_session_when_available(self):
        self.create_replayable_agent("req-create-session-mismatch",external_session="session-original")
        observed={"result":{"agent":{"name":"worker","tab_id":"tab-new",
                                       "pane_id":"pane-new","status":"idle",
                                       "agent_session":{"value":"session-replacement"}}}}
        with patch.object(m,"herdr_json",return_value=observed) as external:
            replay=m.spawn("worker",str(Path(__file__).resolve().parent),name="worker",
                           session="herdr-session-test",request_id="req-create-session-mismatch")
        self.assertEqual(replay["status"],"REQUEST_STALE_INSTANCE")
        external.assert_called_once()

    def test_spawn_replay_without_confirming_identity_is_unknown_without_recreation(self):
        self.create_replayable_agent("req-create-unconfirmed")
        with patch.object(m,"herdr_json",return_value={"result":{"agent":{"name":"worker","status":"idle"}}}) as external:
            replay=m.spawn("worker",str(Path(__file__).resolve().parent),name="worker",
                           session="herdr-session-test",request_id="req-create-unconfirmed")
        self.assertEqual(replay["status"],"UNKNOWN")
        self.assertEqual(replay["error_code"],"CREATE_STATUS_UNKNOWN")
        external.assert_called_once()
        self.assertEqual(m.read_json(m.REQUESTS/"req-create-unconfirmed.json",{})["status"],"UNKNOWN")

    def test_spawn_timeout_request_is_unknown_and_never_replayed(self):
        cwd=str(Path(__file__).resolve().parent)
        responses=[{"result":{"tabs":[]}},
                   {"result":{"tab":{"tab_id":"tab-timeout"},"root_pane":{"pane_id":"pane-timeout"}}},
                   subprocess.TimeoutExpired("herdr agent start",40)]
        with patch.object(m,"herdr_json",side_effect=responses):
            first=m.spawn("worker",cwd,name="worker",session="herdr-session-test",request_id="req-create-timeout")
        self.assertEqual(first["status"],"UNKNOWN")
        with patch.object(m,"herdr_json") as external:
            replay=m.spawn("worker",cwd,name="worker",session="herdr-session-test",request_id="req-create-timeout")
        self.assertEqual(replay["status"],"UNKNOWN")
        self.assertEqual(replay["error_code"],"HERDR_TIMEOUT")
        external.assert_not_called()

    def test_same_agent_name_is_rejected_across_sessions(self):
        cwd=str(Path(__file__).resolve().parent)
        m.save_registry({"agents":{"worker":{"name":"worker","lifecycle":"STOPPED",
            "herdr_session":"other-session","tab_id":"old-tab","pane_id":"old-pane"}}})
        with patch.object(m,"herdr_json") as external:
            with self.assertRaises(GateError) as error:
                m.spawn("worker",cwd,name="worker",session="herdr-session-test")
        self.assertEqual(error.exception.code,"AGENT_NAME_SESSION_CONFLICT")
        external.assert_not_called()

    def test_cli_dispatch_keeps_task_package_working_directory(self):
        with tempfile.TemporaryDirectory() as other_cwd:
            task=make_task_package(other_cwd,task_id="cross-directory")
            approve_test_task(task,m.STATE,model="jev-test")
            created={"name":"worker","lifecycle":"IDLE","tab_id":"tab","pane_id":"pane"}
            dispatched={"agent":"worker","accepted":True,"status":"DISPATCHED",
                        "task_id":"relay-cross-directory","task_sha256":m._task_digest(task)}
            with patch.object(m,"spawn",return_value=created) as spawn, \
                 patch.object(m,"dispatch_opaque",return_value=dispatched):
                result=m.dispatch_task(task,"worker",name="worker",session="herdr-session-test",
                                      request_id="req-cross-directory")
        self.assertTrue(result["accepted"])
        self.assertEqual(spawn.call_args.args[1],str(Path(other_cwd).resolve()))


if __name__ == "__main__":
    unittest.main()
