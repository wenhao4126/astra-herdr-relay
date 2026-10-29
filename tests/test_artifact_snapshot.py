import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
try:
    import artifact as snapshots
except ImportError:
    snapshots = None

from test_task_contract import make_task


def author():
    return {"producer_id": "codex-worker", "producer_role": "executor",
            "session_id": "session-worker", "tab_id": "tab-1", "pane_id": "pane-1",
            "agent_session_value": "herdr-agent-1"}


class ArtifactSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.state = self.base / "state"
        self.task = make_task(self.root)
        self.assertIsNotNone(snapshots, "需要本地 artifact 模块")

    def test_snapshot_hash_is_sorted_and_includes_declared_untracked_delivery_files(self):
        (self.root / "src").mkdir()
        (self.root / "src/a.py").write_text("print('a')\n")
        (self.root / "new-untracked.txt").write_text("交付文件\n")
        first = snapshots.capture_artifact(self.task, ["new-untracked.txt", "src/a.py"],
                                           self.state, author(), created_at="2026-10-28T23:00:00Z")
        second = snapshots.capture_artifact(self.task, ["src/a.py", "new-untracked.txt"],
                                            self.state, author(), created_at="2026-10-28T23:01:00Z")
        self.assertEqual(first["artifact_sha256"], second["artifact_sha256"])
        self.assertEqual([item["path"] for item in second["files"]], ["new-untracked.txt", "src/a.py"])
        self.assertEqual(second["author_instance"]["producer_id"], "codex-worker")
        self.assertEqual(snapshots.verify_current_snapshot(self.task, self.state)["artifact_sha256"],
                         second["artifact_sha256"])

    def test_snapshot_rejects_escape_and_symlink_and_invalidates_changed_content(self):
        (self.root / "deliverable.txt").write_text("before\n")
        outside = self.base / "outside.txt"
        outside.write_text("outside\n")
        with self.assertRaises(snapshots.ArtifactError):
            snapshots.capture_artifact(self.task, ["../outside.txt"], self.state, author())
        (self.root / "link.txt").symlink_to(outside)
        with self.assertRaises(snapshots.ArtifactError):
            snapshots.capture_artifact(self.task, ["link.txt"], self.state, author())
        snapshots.capture_artifact(self.task, ["deliverable.txt"], self.state, author())
        (self.root / "deliverable.txt").write_text("after\n")
        with self.assertRaises(snapshots.ArtifactError):
            snapshots.verify_current_snapshot(self.task, self.state)


if __name__ == "__main__":
    unittest.main()
