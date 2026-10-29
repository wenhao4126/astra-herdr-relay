import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
try:
    import doc_registry as registry
except ImportError:
    registry = None


def sample_doc():
    return {
        "id": "typesafe-score-choice-0.5.0",
        "question": "Score 与 Choice 的语义是什么？",
        "library_id": "/typesafe-docs/typesafeai",
        "project_version": "0.5.0",
        "source_url": "https://github.com/typesafe-docs/typesafeai/blob/main/docs/04-primitives/README.md",
        "excerpt": "Score 表示有序程度；Choice 表示有限类别。",
        "conclusion": "等级问题用 Score，分类问题用 Choice。",
        "limitations": "不推导 Relay 的阈值。",
        "retrieved_at": "2026-10-28T23:42:33Z",
        "status": "verified",
    }


class DocumentRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assertIsNotNone(registry, "需要本地 doc_registry 模块")

    def test_record_doc_computes_digest_and_refuses_duplicate_id(self):
        saved = registry.record_doc(sample_doc(), self.root)
        self.assertRegex(saved["content_sha256"], r"^[a-f0-9]{64}$")
        disk = json.loads((self.root / "doc_records" / (saved["id"] + ".json")).read_text())
        self.assertEqual(disk, saved)
        with self.assertRaises(registry.DocRecordError):
            registry.record_doc(sample_doc(), self.root)

    def test_context7_task_example_matches_program_generated_record_digest(self):
        project = Path(__file__).parents[1]
        doc = json.loads((project / "examples/context7-typesafe-score-choice.json").read_text())
        task = json.loads((project / "examples/task-v2-context7.json").read_text())
        saved = registry.record_doc(doc, self.root)
        self.assertEqual(saved["content_sha256"], task["docs"][0]["content_sha256"])
        self.assertEqual(registry.load_required_docs(task, self.root)[0]["id"], saved["id"])

    def test_record_doc_rejects_naive_time_and_wrong_field_types(self):
        value = sample_doc()
        value["retrieved_at"] = "2026-10-28T23:42:33"
        with self.assertRaises(registry.DocRecordError):
            registry.record_doc(value, self.root)
        value = sample_doc()
        value["project_version"] = 1
        with self.assertRaises(registry.DocRecordError):
            registry.record_doc(value, self.root)

    def test_required_docs_reject_missing_conflict_version_and_digest_mismatch(self):
        value = sample_doc()
        saved = registry.record_doc(value, self.root)
        ref = {"id": saved["id"], "project_version": saved["project_version"],
               "content_sha256": saved["content_sha256"]}
        task = {"schema_version": 2, "docs": [ref], "required_doc_ids": [saved["id"]]}
        self.assertEqual(registry.load_required_docs(task, self.root)[0]["id"], saved["id"])
        for status, version, digest in (("missing", "0.5.0", saved["content_sha256"]),
                                        ("conflict", "0.5.0", saved["content_sha256"]),
                                        ("verified", "0.6.0", saved["content_sha256"]),
                                        ("verified", "0.5.0", "0" * 64)):
            with self.subTest(status=status, version=version, digest=digest[:4]):
                changed = copy.deepcopy(value)
                changed["status"] = status
                state = self.root / f"state-{status}-{version}-{digest[:4]}"
                replacement = registry.record_doc(changed, state)
                bad_ref = {"id": replacement["id"], "project_version": version,
                           "content_sha256": digest}
                with self.assertRaises(registry.DocRecordError):
                    registry.load_required_docs({"schema_version": 2, "docs": [bad_ref],
                                                 "required_doc_ids": [replacement["id"]]},
                                                state)


if __name__ == "__main__":
    unittest.main()
