import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
BUS = Path("/run/user") / str(os.getuid()) / "bus"


class InstallCliTests(unittest.TestCase):
    def setUp(self):
        if not BUS.is_socket():
            self.skipTest(f"测试环境没有可推导的 user bus socket: {BUS}")

    def run_check(self, explicit=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            record = root / "systemctl-env.txt"
            fake_bin.joinpath("systemctl").write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"${XDG_RUNTIME_DIR-}\" \"${DBUS_SESSION_BUS_ADDRESS-}\" > \"$RECORD\"\n"
                "case \"$3\" in\n"
                "  show-environment) exit 0;;\n"
                "  show) printf 'LoadState=not-found\\nActiveState=inactive\\nUnitFileState=disabled\\nMainPID=0\\n'; exit 0;;\n"
                "esac\n"
            )
            fake_bin.joinpath("systemctl").chmod(0o755)
            env = os.environ.copy()
            env.update(HOME=str(root), PATH=f"{fake_bin}:{env['PATH']}", RECORD=str(record))
            if explicit:
                env.update(XDG_RUNTIME_DIR="/explicit/runtime", DBUS_SESSION_BUS_ADDRESS="unix:path=/explicit/bus")
            else:
                env.pop("XDG_RUNTIME_DIR", None)
                env.pop("DBUS_SESSION_BUS_ADDRESS", None)
            result = subprocess.run([str(ROOT / "install.sh"), "--check", "--root", str(target)],
                                    cwd=ROOT, env=env, text=True, capture_output=True)
            return result, record.read_text().splitlines() if record.exists() else []

    def test_check_derives_missing_user_bus_environment(self):
        result, values = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, [f"/run/user/{os.getuid()}", f"unix:path=/run/user/{os.getuid()}/bus"])

    def test_check_preserves_explicit_user_bus_environment(self):
        result, values = self.run_check(explicit=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, ["/explicit/runtime", "unix:path=/explicit/bus"])
