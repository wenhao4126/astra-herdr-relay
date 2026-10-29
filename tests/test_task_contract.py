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


def make_task_v2(directory):
    task = make_task(directory)
    task.update(schema_version=2, required_doc_ids=[])
    task["evidence_policy"] = {
        "review": ["tests"],
        "final": ["tests", "review"],
        "no_external_docs_reason": "本任务只改本地协议与测试，不依赖外部接口。",
    }
    return task


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

    def test_schema_v2_accepts_explicit_local_only_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(validate_task(make_task_v2(directory))["schema_version"], 2)

    def test_schema_v2_requires_structured_evidence_policy_and_local_docs_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task_v2(directory)
            task["evidence_policy"] = ["tests"]
            with self.assertRaises(TaskContractError):
                validate_task(task)
            task = make_task_v2(directory)
            task["evidence_policy"]["no_external_docs_reason"] = ""
            with self.assertRaises(TaskContractError):
                validate_task(task)

    def test_schema_v2_requires_unique_versioned_document_references(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task_v2(directory)
            digest = "a" * 64
            task["docs"] = [{"id": "typesafe-score-choice", "project_version": "0.5.0",
                             "content_sha256": digest}]
            task["required_doc_ids"] = ["typesafe-score-choice"]
            self.assertEqual(validate_task(task)["required_doc_ids"], ["typesafe-score-choice"])
            task["docs"].append(dict(task["docs"][0]))
            with self.assertRaises(TaskContractError):
                validate_task(task)

    def test_schema_v2_rejects_review_requirement_at_review_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task_v2(directory)
            task["evidence_policy"]["review"] = ["review"]
            task["evidence_policy"]["final"] = ["review"]
            with self.assertRaises(TaskContractError):
                validate_task(task)

    def test_schema_v2_policy_rejects_unhashable_kind_as_structured_contract_error(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task_v2(directory)
            task["evidence_policy"]["review"] = [["tests"]]
            with self.assertRaises(TaskContractError):
                validate_task(task)


if __name__ == "__main__":
    unittest.main()
