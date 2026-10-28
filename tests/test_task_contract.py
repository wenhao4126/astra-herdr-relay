import copy
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
from task_contract import TaskContractError, canonical_digest, validate_task


def make_task(directory):
    return {
        "schema_version": 1,
        "task_id": "relay-task-1",
        "revision": 1,
        "goal": "完成一个可验收的修改",
        "scope": {"allowed": ["lib/relay.py"], "forbidden": ["真实配置"]},
        "behavior": {
            "inputs": ["接收版本化任务包"],
            "outputs": ["保存结构化结果"],
            "errors": ["非法输入明确失败"],
            "compatibility": ["保留显式控制入口"],
        },
        "acceptance": ["隔离测试通过"],
        "evidence_policy": ["保留测试结果"],
        "docs": [],
        "working_directory": str(Path(directory).resolve()),
    }


class TaskContractTests(unittest.TestCase):
    def test_canonical_digest_binds_all_package_fields_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task(directory)
            reordered = copy.deepcopy(dict(reversed(list(task.items()))))
            self.assertEqual(canonical_digest(task), canonical_digest(reordered))
            task["scope"]["forbidden"].append("终端越界")
            self.assertNotEqual(canonical_digest(task), canonical_digest(reordered))

    def test_rejects_coercion_unknown_fields_and_boolean_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task(directory)
            task["revision"] = True
            with self.assertRaises(TaskContractError):
                validate_task(task)
            task = make_task(directory)
            task["unexpected"] = "不能静默忽略"
            with self.assertRaises(TaskContractError):
                validate_task(task)

    def test_requires_existing_canonical_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task(directory)
            task["working_directory"] = str(Path(directory) / "missing")
            with self.assertRaises(TaskContractError):
                validate_task(task)

    def test_rejects_invalid_document_digest_and_non_json_float(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task(directory)
            task["docs"] = [{"source": "docs/api", "version": "v1", "sha256": "bad"}]
            with self.assertRaises(TaskContractError):
                validate_task(task)
            task = make_task(directory)
            task["evidence_policy"] = [1.5]
            with self.assertRaises(TaskContractError):
                validate_task(task)


if __name__ == "__main__":
    unittest.main()
