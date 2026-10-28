import importlib.util, pathlib, sys, tempfile, unittest
LIB=pathlib.Path(__file__).parents[1]/"lib"
sys.path.insert(0,str(LIB))
P=pathlib.Path(__file__).parents[1]/"lib"/"relay.py"
spec=importlib.util.spec_from_file_location("relay",P); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
real_herdr_json=m.herdr_json


def make_task_package(directory=None, task_id="relay-test"):
    return {
        "schema_version": 1,
        "task_id": task_id,
        "revision": 1,
        "goal": "完成隔离验证任务",
        "scope": {"allowed": ["lib/relay.py"], "forbidden": ["真实配置"]},
        "behavior": {
            "inputs": ["读取结构化任务包"],
            "outputs": ["保存派发状态"],
            "errors": ["非法输入拒绝"],
            "compatibility": ["旧自由文本不派发"],
        },
        "acceptance": ["验证结果明确"],
        "evidence_policy": ["隔离测试记录"],
        "docs": [],
        "working_directory": str(pathlib.Path(directory or pathlib.Path(__file__).parents[1]).resolve()),
    }


def approve_test_task(task, state_dir, model=None):
    import gates
    model = model or m.TYPESAFE_MODEL
    answers = {}
    for key, _weight in gates.SCORE_DIMENSIONS:
        answers["score_" + key] = {
            "type": "score", "score": 4, "confidence": 0.99,
            "legend": {str(i): f"等级{i}" for i in range(5)},
            "probabilities": {str(i): (1.0 if i == 4 else 0.0) for i in range(5)},
        }
    for key in gates.BLOCKER_DIMENSIONS:
        answers["blocker_" + key] = {
            "type": "choice", "choice": "absent", "confidence": 0.99,
            "probabilities": {"present": 0.0, "absent": 1.0, "unknown": 0.0},
        }
    gates.evaluate_once(
        task, model, "unit-test-only",
        state_dir, requester=lambda payload, api_key, timeout:
        {"model": model+"-version", "answers": answers},
    )

def approved_test_task(state_dir=None, task_id="relay-test", model=None):
    state_dir=state_dir or m.STATE
    task=make_task_package(task_id=task_id)
    approve_test_task(task,state_dir,model)
    return task

def seed_agent(name="worker",**changes):
    record={"name":name,"role":"worker","kind":"codex","lifecycle":"IDLE",
            "cwd":str(pathlib.Path(__file__).parents[1].resolve()),
            "herdr_session":"relay-test-session","tab_id":"tab-"+name,
            "pane_id":"pane-"+name,"created_at":1}
    record.update(changes)
    agents=m.registry()["agents"]
    agents[name]=record
    m.save_registry({"agents":agents})
    return record


def fake_herdr_json(args,timeout=60,session=None):
    """隔离真实 Herdr CLI，并按临时 registry 返回可核对身份。"""
    if args[:2]==["agent","get"]:
        name=args[2]
        record=m.registry()["agents"].get(name)
        if not record:
            return {"error":{"code":"agent_not_found"}}
        agent={key:record.get(key) for key in ("name","tab_id","pane_id")}
        agent["agent_status"]=record.get("test_runtime","idle")
        if record.get("herdr_agent_session_value"):
            agent["agent_session"]={"value":record["herdr_agent_session_value"]}
        return {"result":{"agent":agent}}
    if args[:2]==["agent","list"]:
        agents=[]
        for record in m.registry()["agents"].values():
            agent={key:record.get(key) for key in ("name","tab_id","pane_id")}
            agent["agent_status"]=record.get("test_runtime","idle")
            agents.append(agent)
        return {"result":{"agents":agents}}
    if args[:2]==["tab","list"]:
        return {"result":{"tabs":[]}}
    return {"result":{}}

class RelayContractTests(unittest.TestCase):
    def test_prob_rejects_bool(self):
        with self.assertRaises(RuntimeError):
            m.prob(True,"x")

    def test_roles_frozen(self):
        self.assertEqual(m.ALLOWED_ROLES,{"worker","fixer","reviewer","tester","auditor"})

    def test_deterministic_semantic(self):
        self.assertEqual(m.deterministic_semantic("done"),"COMPLETED")
        self.assertEqual(m.deterministic_semantic("blocked"),"BLOCKED")
        self.assertIsNone(m.deterministic_semantic("working"))
