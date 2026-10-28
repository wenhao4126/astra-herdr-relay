import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_relay import approved_test_task, fake_herdr_json, m, seed_agent


class EventRegressionTests(unittest.TestCase):
    def test_async_dispatch_omits_wait_only_timeout(self):
        seed_agent("test-worker")
        task=approved_test_task(m.STATE,model=m.TYPESAFE_MODEL)
        with patch.object(m, "run") as popen:
            popen.return_value.returncode = 0
            result = m.dispatch_opaque("test-worker", task)
        self.assertTrue(result["accepted"])
        command = popen.call_args.args[0]
        self.assertNotIn("--timeout", command)
        self.assertNotIn("--wait", command)
        self.assertIn("--session",command)
        self.assertIn("[RELAY FINISHED "+result["task_id"]+"]",command[6])
        self.assertIn('"schema_version":1',command[6])

    def test_followup_omits_wait_only_timeout(self):
        seed_agent("test-worker")
        task=approved_test_task(m.STATE,model=m.TYPESAFE_MODEL)
        with patch.object(m, "run") as run:
            run.return_value.returncode = 0
            result = m.send_opaque("test-worker", json.dumps(task))
        self.assertTrue(result["accepted"])
        command=run.call_args.args[0]
        self.assertEqual(command[:6], ["herdr","--session","relay-test-session","agent","prompt","pane-test-worker"])
        self.assertIn('"schema_version":1',command[6])

    def test_explicit_status_control_never_sends_free_text(self):
        seed_agent("test-worker")
        with patch.object(m,"run") as external:
            result=m.send_opaque("test-worker",' {"control":"status"} ')
        self.assertEqual(result["control"],"status")
        self.assertEqual(result["status"],"IDLE")
        external.assert_not_called()

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for key, value in {
            "STATE": root, "REGISTRY": root / "agents.json",
            "TASKS": root / "tasks", "EVENTS": root / "events.jsonl",
            "SEMANTIC": root / "semantic", "REVIEWS": root / "reviews",
            "LOCKS": root / "locks",
        }.items():
            self.enterContext(patch.object(m, key, value))
        self.enterContext(patch.object(m,"HERDR_SESSION","relay-test-session"))
        self.enterContext(patch.object(m,"herdr_json",side_effect=fake_herdr_json))

    def test_spawn_records_named_agent_and_reaches_idle(self):
        # 隔离外部进程，验证创建流程确实能越过日志记录并启动代理。
        with patch.object(m, "herdr_json", side_effect=[
            {"result": {"tabs": [{"tab_id": "astra-tab", "focused": True}]}},
            {"result": {"tab": {"tab_id": "test-tab"},
                        "root_pane": {"pane_id": "test-pane"}}},
            {"result":{"agent":{"name":"test-worker","tab_id":"test-tab",
                                  "pane_id":"test-pane","agent_status":"idle"}}},
            {"result":{"agent":{"name":"test-worker","tab_id":"test-tab",
                                  "pane_id":"test-pane","agent_status":"idle"}}},
            {"result": {"tabs": [{"tab_id": "astra-tab", "focused": True}]}},
        ]) as herdr:
            rec = m.spawn("worker", "/tmp", name="test-worker")
        self.assertEqual(rec["lifecycle"], "IDLE")
        self.assertEqual(herdr.call_count, 5)
        self.assertIn("--no-focus", herdr.call_args_list[1].args[0])
        self.assertNotIn("--focus", herdr.call_args_list[1].args[0])
        self.assertEqual(herdr.call_args_list[2].args[0],
                         ["agent", "start", "test-worker", "--kind", "codex",
                          "--pane", "test-pane", "--timeout", "30000"])
        self.assertEqual(herdr.call_args_list[3].args[0], ["agent","get","test-worker"])
        self.assertEqual(herdr.call_args_list[3].kwargs["session"], "relay-test-session")
        events = [json.loads(line) for line in m.EVENTS.read_text().splitlines()]
        self.assertEqual(events[0]["event"], "agent_creating")
        self.assertEqual(events[0]["name"], "test-worker")
        self.assertEqual(events[-1]["event"], "agent_ready")

    def test_claude_start_blocked_keeps_background_tab_for_user(self):
        with patch.object(m, "herdr_json", side_effect=[
            {"result": {"tabs": [{"tab_id": "astra-tab", "focused": True}]}},
            {"result": {"tab": {"tab_id": "test-tab"},
                        "root_pane": {"pane_id": "test-pane"}}},
            RuntimeError("agent_not_ready"),
            {"result": {"tabs": [{"tab_id": "test-tab", "focused": True}]}},
            {},
        ]) as herdr:
            rec = m.spawn("reviewer", "/tmp", name="claude-reviewer", kind="claude", model="haiku-4.5")
        self.assertEqual(rec["lifecycle"], "BLOCKED")
        self.assertEqual(rec["status"], "NOT_READY")
        self.assertEqual(rec["tab_id"], "test-tab")
        self.assertEqual(herdr.call_count, 5)
        self.assertIn("--no-focus", herdr.call_args_list[1].args[0])
        self.assertEqual(herdr.call_args_list[2].args[0][-3:], ["--", "--model", "haiku-4.5"])

    def test_spawn_never_marks_unconfirmed_start_response_idle(self):
        cases=(
            ({"name":"worker","tab_id":"tab","pane_id":"pane","agent_status":"starting"},"NOT_READY","BLOCKED"),
            ({"name":"worker","tab_id":"tab","pane_id":"pane","agent_status":"blocked"},"NOT_READY","BLOCKED"),
            ({"name":"worker","tab_id":"tab","pane_id":"pane","agent_status":"unknown"},"UNKNOWN","UNKNOWN"),
            ({"name":"worker","tab_id":"tab","pane_id":"pane"},"UNKNOWN","UNKNOWN"),
            ({"name":"worker","tab_id":"replacement","pane_id":"pane","agent_status":"idle"},"UNKNOWN","UNKNOWN"),
        )
        for response,status,lifecycle in cases:
            with self.subTest(status=status,response=response):
                m.save_registry({"agents":{}})
                with patch.object(m,"herdr_json",side_effect=[
                    {"result":{"tabs":[]}},
                    {"result":{"tab":{"tab_id":"tab"},"root_pane":{"pane_id":"pane"}}},
                    {"result":{"agent":response}},
                ]) as herdr:
                    record=m.spawn("worker","/tmp",name="worker")
                self.assertEqual(record["status"],status)
                self.assertEqual(record["lifecycle"],lifecycle)
                self.assertFalse(any(call.args[0][:2]==["agent","get"]
                                     for call in herdr.call_args_list))

    def test_spawn_get_replacement_after_ready_start_returns_unknown(self):
        with patch.object(m,"herdr_json",side_effect=[
            {"result":{"tabs":[]}},
            {"result":{"tab":{"tab_id":"tab"},"root_pane":{"pane_id":"pane"}}},
            {"result":{"agent":{"name":"worker","tab_id":"tab","pane_id":"pane",
                                  "agent_status":"idle"}}},
            {"result":{"agent":{"name":"worker","tab_id":"replacement","pane_id":"pane",
                                  "agent_status":"idle"}}},
        ]) as herdr:
            record=m.spawn("worker","/tmp",name="worker")
        self.assertEqual(record["lifecycle"],"UNKNOWN")
        self.assertEqual(record["status"],"UNKNOWN")
        self.assertEqual(herdr.call_args_list[3].args[0],["agent","get","worker"])

    def test_codex_start_forwards_explicit_model(self):
        with patch.object(m, "herdr_json", side_effect=[
            {"result": {"tabs": []}},
            {"result": {"tab": {"tab_id": "test-tab"},
                        "root_pane": {"pane_id": "test-pane"}}},
            {"result":{"agent":{"name":"test-worker","tab_id":"test-tab",
                                  "pane_id":"test-pane","agent_status":"idle"}}},
            {"result":{"agent":{"name":"test-worker","tab_id":"test-tab",
                                  "pane_id":"test-pane","agent_status":"idle"}}},
        ]) as herdr:
            m.spawn("worker", "/tmp", name="test-worker", model="gpt-6-luna")
        self.assertEqual(herdr.call_args_list[2].args[0][-3:], ["--", "--model", "gpt-6-luna"])

    def test_focus_restores_only_when_created_tab_still_focused(self):
        with patch.object(m, "herdr_json", side_effect=[
            {"result": {"tabs": [{"tab_id": "astra", "focused": True}]}},
            {"result": {"tab": {"tab_id": "new"}, "root_pane": {"pane_id": "p"}}},
            {"result":{"agent":{"name":"test-worker","tab_id":"new",
                                  "pane_id":"p","agent_status":"idle"}}},
            {"result":{"agent":{"name":"test-worker","tab_id":"new",
                                  "pane_id":"p","agent_status":"idle"}}},
            {"result": {"tabs": [{"tab_id": "new", "focused": True}]}}, {},
        ]) as herdr:
            m.spawn("worker", "/tmp", name="test-worker")
        self.assertEqual(herdr.call_args_list[-1].args[0], ["tab", "focus", "astra"])

    def test_focus_does_not_override_user_tab_change(self):
        with patch.object(m, "herdr_json", side_effect=[
            {"result": {"tabs": [{"tab_id": "astra", "focused": True}]}},
            {"result": {"tab": {"tab_id": "new"}, "root_pane": {"pane_id": "p"}}},
            {"result":{"agent":{"name":"test-worker","tab_id":"new",
                                  "pane_id":"p","agent_status":"idle"}}},
            {"result":{"agent":{"name":"test-worker","tab_id":"new",
                                  "pane_id":"p","agent_status":"idle"}}},
            {"result": {"tabs": [{"tab_id": "user-choice", "focused": True}]}},
        ]) as herdr:
            m.spawn("worker", "/tmp", name="test-worker")
        self.assertEqual(herdr.call_count, 5)

    def test_start_review_pair_stops_on_duplicate_without_cleanup(self):
        with patch.object(m, "spawn", side_effect=[{"name": "codex-worker-luna", "lifecycle":"IDLE"}, SystemExit("agent already active")]) as spawn, \
             patch.object(m, "release") as release:
            result=m.start_review_pair("/tmp")
        self.assertFalse(result["complete"])
        self.assertEqual([x["name"] for x in result["created"]], ["codex-worker-luna"])
        self.assertEqual(result["errors"][0]["name"], "opencode-reviewer")
        release.assert_not_called()
        self.assertEqual(spawn.call_count, 2)

    def test_start_review_pair_reuses_fixed_agents_without_spawning_or_changing_focus(self):
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna","role":"worker","lifecycle":"IDLE","kind":"codex","cwd":"/tmp","model":"gpt-6-luna","herdr_session":"relay-test-session","tab_id":"tab-worker","pane_id":"pane-worker","created_at":1},
            "opencode-reviewer": {"name":"opencode-reviewer","role":"reviewer","lifecycle":"IDLE","kind":"opencode","cwd":"/tmp","model":None,"herdr_session":"relay-test-session","tab_id":"tab-reviewer","pane_id":"pane-reviewer","created_at":2},
        }})
        with patch.object(m, "spawn") as spawn, patch.object(m, "herdr_json",side_effect=fake_herdr_json) as herdr:
            result=m.start_review_pair("/tmp")
        spawn.assert_not_called()
        self.assertEqual([call.args[0] for call in herdr.call_args_list],
                         [["agent","get","codex-worker-luna"],
                          ["agent","get","opencode-reviewer"]])
        self.assertTrue(all(call.kwargs.get("session")=="relay-test-session"
                            for call in herdr.call_args_list))
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["reused"]), 2)
        self.assertEqual(next(x for x in result["reused"] if x["name"]=="opencode-reviewer")["model_status"], "unverified")

    def test_start_review_pair_does_not_call_blocked_pair_complete(self):
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna","role":"worker","lifecycle":"IDLE","kind":"codex","cwd":"/tmp","model":"gpt-6-luna"},
            "opencode-reviewer": {"name":"opencode-reviewer","role":"reviewer","lifecycle":"BLOCKED","kind":"opencode","cwd":"/tmp","model":None},
        }})
        with patch.object(m, "spawn") as spawn:
            result=m.start_review_pair("/tmp")
        spawn.assert_not_called()
        self.assertFalse(result["complete"])

    def test_start_review_pair_rejects_reused_wrong_kind_without_mutating_agent(self):
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna","role":"worker","lifecycle":"IDLE","kind":"codex","cwd":"/tmp","model":"gpt-6-luna","herdr_session":"relay-test-session","tab_id":"tab-worker","pane_id":"pane-worker","created_at":1},
            "opencode-reviewer": {"name":"opencode-reviewer","role":"reviewer","lifecycle":"IDLE","kind":"claude","cwd":"/tmp","model":None},
        }})
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release, \
             patch.object(m,"herdr_json",side_effect=fake_herdr_json):
            result=m.start_review_pair("/tmp")
        spawn.assert_not_called()
        release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertIn("kind", result["errors"][0]["error"])
        self.assertEqual(m.registry()["agents"]["opencode-reviewer"]["kind"], "claude")

    def test_start_review_pair_rejects_reused_agent_missing_role_without_mutating_agent(self):
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "kind":"codex", "cwd":"/tmp", "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode", "cwd":"/tmp", "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair("/tmp")
        spawn.assert_not_called(); release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertTrue(any(x["name"] == "codex-worker-luna" and "role" in x["error"] for x in result["errors"]))
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_rejects_reused_agent_missing_cwd_without_mutating_agent(self):
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "role":"worker", "kind":"codex", "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode", "cwd":"/tmp", "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair("/tmp")
        spawn.assert_not_called(); release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertTrue(any(x["name"] == "codex-worker-luna" and "cwd" in x["error"] for x in result["errors"]))
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_rejects_empty_cwd_without_mutating_agent(self):
        cwd = str(Path.cwd())
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "role":"worker", "kind":"codex", "cwd":cwd, "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode", "cwd":cwd, "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair("")
        spawn.assert_not_called(); release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertTrue(result["errors"])
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_rejects_whitespace_cwd_without_mutating_agent(self):
        cwd = str(Path.cwd())
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "role":"worker", "kind":"codex", "cwd":cwd, "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode", "cwd":cwd, "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair("   \t")
        spawn.assert_not_called(); release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertTrue(result["errors"])
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_rejects_non_string_cwd_without_mutating_agent(self):
        cwd = str(Path.cwd())
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "role":"worker", "kind":"codex", "cwd":cwd, "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode", "cwd":cwd, "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair(123)
        spawn.assert_not_called(); release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertTrue(result["errors"])
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_rejects_stored_empty_cwd_without_mutating_agent(self):
        cwd = str(Path.cwd())
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "role":"worker", "kind":"codex", "cwd":"", "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode", "cwd":cwd, "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair(cwd)
        spawn.assert_not_called(); release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertTrue(any(x["name"] == "codex-worker-luna" and "cwd" in x["error"] for x in result["errors"]))
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_rejects_stored_non_string_cwd_without_mutating_agent(self):
        cwd = str(Path.cwd())
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "role":"worker", "kind":"codex", "cwd":123, "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode", "cwd":cwd, "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair(cwd)
        spawn.assert_not_called(); release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertTrue(any(x["name"] == "codex-worker-luna" and "cwd" in x["error"] for x in result["errors"]))
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_rejects_reused_agent_with_wrong_cwd_without_mutating_agent(self):
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "role":"worker", "kind":"codex",
                                   "cwd":"/tmp/old-review-cwd", "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode",
                                   "cwd":str(Path.cwd()), "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair("/tmp/new-review-cwd/../new-review-cwd")
        spawn.assert_not_called()
        release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertNotIn("codex-worker-luna", [x["name"] for x in result["reused"]])
        self.assertTrue(any(x["name"] == "codex-worker-luna" and "cwd" in x["error"]
                            for x in result["errors"]))
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_rejects_reused_agent_with_wrong_role_without_mutating_agent(self):
        m.save_registry({"agents": {
            "codex-worker-luna": {"name":"codex-worker-luna", "role":"reviewer", "kind":"codex",
                                   "cwd":str(Path("/tmp").resolve()), "lifecycle":"IDLE"},
            "opencode-reviewer": {"name":"opencode-reviewer", "role":"reviewer", "kind":"opencode",
                                   "cwd":str(Path("/tmp").resolve()), "lifecycle":"IDLE"},
        }})
        before = m.registry()
        with patch.object(m, "spawn") as spawn, patch.object(m, "release") as release:
            result = m.start_review_pair("/tmp")
        spawn.assert_not_called()
        release.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertNotIn("codex-worker-luna", [x["name"] for x in result["reused"]])
        self.assertTrue(any(x["name"] == "codex-worker-luna" and "role" in x["error"]
                            for x in result["errors"]))
        self.assertEqual(m.registry(), before)

    def test_start_review_pair_uses_fixed_codex_worker_without_model(self):
        with patch.object(m, "spawn", side_effect=[
            {"name":"codex-worker-luna","lifecycle":"IDLE"},
            {"name":"opencode-reviewer","lifecycle":"IDLE"},
        ]) as spawn:
            result=m.start_review_pair("/tmp")
        self.assertTrue(result["complete"])
        self.assertEqual(spawn.call_args_list[0].kwargs["name"], "codex-worker-luna")
        self.assertEqual(spawn.call_args_list[0].kwargs["kind"], "codex")
        self.assertIsNone(spawn.call_args_list[0].kwargs["model"])

    def test_start_review_pair_uses_fixed_opencode_reviewer_without_model(self):
        with patch.object(m, "spawn", side_effect=[
            {"name":"codex-worker-luna","lifecycle":"IDLE"},
            {"name":"opencode-reviewer","lifecycle":"IDLE"},
        ]) as spawn:
            result=m.start_review_pair("/tmp")
        self.assertTrue(result["complete"])
        self.assertEqual(spawn.call_args_list[1].kwargs["name"], "opencode-reviewer")
        self.assertEqual(spawn.call_args_list[1].kwargs["kind"], "opencode")
        self.assertIsNone(spawn.call_args_list[1].kwargs["model"])

    def test_opencode_spawn_does_not_forward_model_argument(self):
        with patch.object(m, "herdr_json", side_effect=[
            {"result": {"tabs": []}},
            {"result": {"tab": {"tab_id": "test-tab"}, "root_pane": {"pane_id": "test-pane"}}}, {},
        ]) as herdr:
            rec=m.spawn("reviewer", "/tmp", name="opencode-reviewer", kind="opencode", model=None)
        self.assertEqual(rec["kind"], "opencode")
        self.assertIsNone(rec["model"])
        self.assertEqual(herdr.call_args_list[2].args[0], ["agent", "start", "opencode-reviewer", "--kind", "opencode", "--pane", "test-pane", "--timeout", "30000"])

    def test_start_review_pair_cli_passes_working_directory(self):
        with patch("sys.argv", ["astra-relay", "start-review-pair", "--cwd", "/tmp"]), \
             patch.object(m, "start_review_pair", return_value={"complete": True}) as pair:
            m.main()
        pair.assert_called_once_with("/tmp")

    def test_other_kind_rejects_model_before_creating_tab(self):
        with patch.object(m, "herdr_json") as herdr:
            with self.assertRaises(SystemExit):
                m.spawn("worker", "/tmp", name="test-worker", kind="other", model="gpt-6-luna")
        herdr.assert_not_called()

    def test_spawn_cli_passes_model(self):
        with patch("sys.argv", ["astra-relay", "spawn", "--role", "worker", "--model", "gpt-6-luna"]), \
             patch.object(m, "spawn", return_value={"name": "test-worker"}) as spawn:
            m.main()
        self.assertEqual(spawn.call_args.args[5], "gpt-6-luna")

    def test_approved_claude_start_can_become_ready(self):
        m.save_registry({"agents": {"claude-reviewer": {
            "name": "claude-reviewer", "role": "reviewer", "kind": "claude",
            "lifecycle": "BLOCKED", "tab_id": "test-tab", "pane_id": "test-pane",
            "herdr_session":"relay-test-session",
        }}})
        snapshot={"result": {"agents": [{"name": "claude-reviewer", "tab_id": "test-tab",
                                         "pane_id": "test-pane", "agent_status": "idle"}]}}
        self.assertEqual(m.sync(snapshot=snapshot)["agents"]["claude-reviewer"]["lifecycle"], "IDLE")

    def test_herdr_session_is_explicit_when_configured(self):
        with patch.object(m, "HERDR_SESSION", "relay-v0.50"):
            self.assertEqual(m.herdr_command("tab", "list"),
                             ["herdr", "--session", "relay-v0.50", "tab", "list"])

    def test_sensor_logs_named_agent_without_calling_jev(self):
        seed_agent("test-worker")
        with patch.object(m, "agent_status_obj", return_value={
            "result": {"agent": {"name":"test-worker", "tab_id":"tab-test-worker",
                                   "pane_id":"pane-test-worker", "status": "done"}}
        }), patch.object(m, "jev_call") as jev:
            result = m.sensor("test-worker")
        self.assertEqual(result["semantic_status"], "COMPLETED")
        jev.assert_not_called()
        event = json.loads(m.EVENTS.read_text().splitlines()[-1])
        self.assertEqual(event["event"], "runtime_sensor")
        self.assertEqual(event["name"], "test-worker")
