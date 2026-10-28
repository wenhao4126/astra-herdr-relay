import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DoctorTests(unittest.TestCase):
    def run_doctor(self, systemctl_body):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "astra-relay").write_text("#!/bin/sh\necho 0.5.0\n")
            (fake_bin / "herdr").write_text("#!/bin/sh\nexit 0\n")
            (fake_bin / "systemctl").write_text(systemctl_body)
            for path in fake_bin.iterdir(): path.chmod(0o755)
            env = os.environ.copy()
            env.update(HOME=str(root), PATH=f"{fake_bin}:{env['PATH']}")
            return subprocess.run(["bash", str(ROOT / "doctor.sh")], cwd=ROOT,
                                  env=env, text=True, capture_output=True)

    def test_systemctl_dbus_failure_prints_unknown_service_status(self):
        result = self.run_doctor("#!/bin/sh\necho 'Failed to connect to bus' >&2\nexit 1\n")
        self.assertEqual(result.returncode, 0)
        service_lines = [line for line in result.stdout.splitlines() if line.startswith("service:")]
        self.assertEqual(len(service_lines), 1)
        self.assertIn("UNKNOWN", service_lines[0])
        self.assertIn("Failed to connect to bus", service_lines[0])

    def test_systemctl_inactive_exit_three_preserves_inactive(self):
        result = self.run_doctor("#!/bin/sh\necho inactive\nexit 3\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("service: inactive", result.stdout)
