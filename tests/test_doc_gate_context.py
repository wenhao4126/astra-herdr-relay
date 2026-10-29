import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
import doc_registry
import gates
from test_task_contract import make_task_v2


class DocumentGateContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = make_task_v2(self.root)

    def doc(self, doc_id, question):
        return {"id": doc_id, "question": question,
                "library_id": "/typesafe-docs/typesafeai", "project_version": "0.5.0",
                "source_url": "https://github.com/typesafe-docs/typesafeai/blob/main/docs/04-primitives/README.md",
                "excerpt": question + " 的相关官方摘录。", "conclusion": "只作本任务的技术依据。",
                "limitations": "不定义 Relay 的门槛。", "retrieved_at": "2026-10-28T23:42:33Z",
                "status": "verified"}

    def test_score_gate_receives_only_task_required_document_excerpts(self):
        first = doc_registry.record_doc(self.doc("required-doc", "Score 与 Choice"), self.root)
        doc_registry.record_doc(self.doc("optional-doc", "Noul"), self.root)
        self.task["docs"] = [
            {"id": first["id"], "project_version": first["project_version"],
             "content_sha256": first["content_sha256"]},
        ]
        self.task["required_doc_ids"] = [first["id"]]
        context = doc_registry.load_required_docs(self.task, self.root)
        payload = gates.build_request(self.task, "jev-test", doc_context=context)
        self.assertEqual([item["id"] for item in payload["state"]["context_documents"]],
                         [first["id"]])
        self.assertNotIn("optional-doc", str(payload))

    def test_missing_required_doc_stops_before_typesafe_request(self):
        self.task["docs"] = [{"id": "missing-doc", "project_version": "0.5.0",
                              "content_sha256": "a" * 64}]
        self.task["required_doc_ids"] = ["missing-doc"]
        calls = []
        with self.assertRaises(gates.GateError):
            gates.evaluate_once(self.task, "jev-test", "mock-key", self.root,
                                requester=lambda *args: calls.append(args))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
