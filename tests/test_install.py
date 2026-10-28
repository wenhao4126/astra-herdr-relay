import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).parents[1]
INSTALLER = PROJECT / "lib" / "install.py"


def load_installer(test):
    if not INSTALLER.is_file():
        test.fail("隔离增量安装器 lib/install.py 尚未实现")
    spec = importlib.util.spec_from_file_location("astra_relay_install_test", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSystemd:
    def __init__(self, home, proc):
        self.home, self.proc = home, proc
        self.installed = self.active = self.enabled = self.fail_next_start = self.fail_all_starts = False
        self.fragment_path = str(home / ".config/systemd/user/astra-herdr-relay.service")
        self.wrong_exec_path = False
        self.exec_path_override = None
        self.on_stop = None
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)

        class Result:
            returncode, stdout, stderr = 0, "", ""

        result = Result()
        if command[:3] == ["systemctl", "--user", "show-environment"]:
            return result
        if command[:3] == ["systemctl", "--user", "show"]:
            if "-p" in command and "ExecStart" in command:
                exec_path = (self.exec_path_override if self.exec_path_override is not None else
                             "/tmp/wrong/astra-relayd" if self.wrong_exec_path else
                             f"{self.home}/.local/bin/astra-relayd")
                result.stdout = f"FragmentPath={self.fragment_path}\nExecStart={{ path={exec_path} ; argv[]={{...}} ; }}\n"
                return result
            result.stdout = ("LoadState=not-found\nActiveState=inactive\nUnitFileState=disabled\nMainPID=0\n"
                             if not self.installed else
                             f"LoadState=loaded\nActiveState={'active' if self.active else 'inactive'}\n"
                             f"UnitFileState={'enabled' if self.enabled else 'disabled'}\n"
                             f"MainPID={'123' if self.active else '0'}\n")
        elif command[:3] == ["systemctl", "--user", "is-enabled"]:
            result.stdout = "enabled\n" if self.enabled else "disabled\n"
            result.returncode = 0 if self.enabled else 1
        elif command[:3] == ["systemctl", "--user", "stop"]:
            self.active = False
            cmdline = self.proc / "123" / "cmdline"
            cmdline.unlink(missing_ok=True)
            try:
                cmdline.parent.rmdir()
            except OSError:
                pass
            if self.on_stop:
                self.on_stop()
        elif command[:3] == ["systemctl", "--user", "daemon-reload"]:
            self.installed = Path(self.fragment_path).is_file()
        elif command[:3] == ["systemctl", "--user", "start"]:
            self.installed = True
            if self.fail_next_start or self.fail_all_starts:
                self.fail_next_start = False
                result.returncode, result.stderr = 1, "start failed"
            else:
                self.active = True
                self._set_process()
        elif command[:4] == ["systemctl", "--user", "enable", "--now"]:
            self.installed = True
            if self.fail_all_starts:
                result.returncode, result.stderr = 1, "enable start failed"
            else:
                self.active = self.enabled = True
                self._set_process()
        elif command[:4] == ["systemctl", "--user", "disable", "--now"]:
            self.active = self.enabled = False
        return result

    def _set_process(self):
        proc = self.proc / "123"
        proc.mkdir(exist_ok=True)
        relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        (proc / "cmdline").write_bytes(b"python3\0" + str(relay).encode() + b"\0daemon\0")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source, self.home, self.proc = (self.base / n for n in ("package", "home", "proc"))
        self.source.mkdir()
        self.home.mkdir()
        self.proc.mkdir()
        self.installer = load_installer(self)
        for rel in self.installer.REQUIRED_FILES:
            src, dst = PROJECT / rel, self.source / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            dst.chmod(src.stat().st_mode & 0o777)
        manifest = "".join(f"{hashlib.sha256((self.source / rel).read_bytes()).hexdigest()}  {rel}\n"
                           for rel in self.installer.REQUIRED_FILES)
        (self.source / "MANIFEST.sha256").write_text(manifest, encoding="utf-8")
        self.systemd = FakeSystemd(self.home, self.proc)

    def install(self, **kwargs):
        return self.installer.install(self.source, self.home, proc_root=self.proc,
                                      runner=self.systemd, **kwargs)

    def test_check_mode_is_read_only(self):
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        self.assertEqual(self.install(check=True)["mode"], "check")
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")), before)

    def test_fresh_install_creates_program_and_secret_defaults(self):
        self.install()
        target = self.home / ".local/lib/astra-herdr-relay"
        self.assertEqual((target / "relay.py").read_bytes(), (self.source / "lib/relay.py").read_bytes())
        self.assertTrue((self.home / ".config/astra-herdr-relay/relay.env").is_file())
        self.assertEqual((self.home / ".config/astra-herdr-relay/secret.env").stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.home / ".local/state/astra-herdr-relay/agents.json").is_file())
        self.assertTrue(self.systemd.active and self.systemd.enabled)

    def test_upgrade_repeat_preserves_config_and_all_state_files(self):
        config, state = self.home / ".config/astra-herdr-relay", self.home / ".local/state/astra-herdr-relay"
        files = {config / "relay.env": b"user config\n", config / "secret.env": b"secret bytes",
                 config / "unknown.conf": b"unknown config", state / "agents.json": b'{"version":"0.5.0","agents":{}}',
                 state / "tasks/old.json": b"task", state / "reviews/old.json": b"review",
                 state / "events.jsonl": b"event\n", state / "unknown.marker": b"retained"}
        for path, data in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.install()
        first = {p: p.read_bytes() for p in files}
        self.install()
        self.assertEqual(first, {p: p.read_bytes() for p in files})
        self.assertTrue(self.systemd.active and self.systemd.enabled)

    def test_busy_registry_refuses_before_system_changes(self):
        state = self.home / ".local/state/astra-herdr-relay"
        state.mkdir(parents=True)
        (state / "agents.json").write_text(json.dumps({"agents": {"worker": {"lifecycle": "BUSY"}}}))
        with self.assertRaises(self.installer.InstallError):
            self.install()
        self.assertEqual(self.systemd.calls, [])

    def test_settled_busy_agent_with_matching_task_and_lock_is_preserved(self):
        state = self.home / ".local/state/astra-herdr-relay"
        tasks, locks = state / "tasks", state / "locks"
        tasks.mkdir(parents=True); locks.mkdir()
        task_id = "relay-settled"
        (state / "agents.json").write_text(json.dumps({"agents": {
            "worker": {"name": "worker", "lifecycle": "BUSY", "current_task_id": task_id}
        }}))
        task = {"task_id": task_id, "agent": "worker", "phase": "HUMAN_REVIEW", "final_state": "HUMAN_REVIEW"}
        (tasks / (task_id + ".json")).write_text(json.dumps(task))
        lock = locks / "worker.lock"
        lock.write_text(task_id)
        before = (state / "agents.json").read_bytes(), (tasks / (task_id + ".json")).read_bytes(), lock.read_bytes()
        self.assertEqual(self.install(check=True)["mode"], "check")
        self.assertEqual(before, ((state / "agents.json").read_bytes(), (tasks / (task_id + ".json")).read_bytes(), lock.read_bytes()))

    def test_running_busy_agent_still_refuses(self):
        state = self.home / ".local/state/astra-herdr-relay"
        tasks, locks = state / "tasks", state / "locks"
        tasks.mkdir(parents=True); locks.mkdir()
        task_id = "relay-running"
        (state / "agents.json").write_text(json.dumps({"agents": {
            "worker": {"name": "worker", "lifecycle": "BUSY", "current_task_id": task_id}
        }}))
        (tasks / (task_id + ".json")).write_text(json.dumps({"task_id": task_id, "agent": "worker", "phase": "RUNNING", "final_state": "RUNNING"}))
        (locks / "worker.lock").write_text(task_id)
        with self.assertRaisesRegex(self.installer.InstallError, "活动代理"):
            self.install(check=True)

    def test_absolute_agent_name_cannot_bypass_lock_preflight(self):
        state = self.home / ".local/state/astra-herdr-relay"
        tasks, locks = state / "tasks", state / "locks"
        tasks.mkdir(parents=True)
        locks.mkdir()
        task_id = "relay-foreign-lock"
        agent_name = str(self.base / "foreign-agent")
        (state / "agents.json").write_text(json.dumps({"agents": {
            agent_name: {"name": agent_name, "lifecycle": "BUSY", "current_task_id": task_id}
        }}))
        (tasks / (task_id + ".json")).write_text(json.dumps({
            "task_id": task_id, "agent": agent_name, "phase": "BLOCKED", "final_state": "BLOCKED"
        }))
        foreign_lock = Path(agent_name + ".lock")
        foreign_lock.write_text(task_id)
        with self.assertRaises(self.installer.InstallError):
            self.install(check=True)
        self.assertEqual(foreign_lock.read_text(), task_id)

    def test_lock_directory_entry_refuses(self):
        rogue = self.home / ".local/state/astra-herdr-relay/locks/rogue.lock"
        rogue.mkdir(parents=True)
        with self.assertRaises(self.installer.InstallError):
            self.install(check=True)

    def test_lock_fifo_refuses(self):
        locks = self.home / ".local/state/astra-herdr-relay/locks"
        locks.mkdir(parents=True)
        os.mkfifo(locks / "rogue.lock")
        with self.assertRaises(self.installer.InstallError):
            self.install(check=True)

    def test_lock_refuses(self):
        locks = self.home / ".local/state/astra-herdr-relay/locks"
        locks.mkdir(parents=True)
        (locks / "active.lock").write_text("held")
        with self.assertRaises(self.installer.InstallError):
            self.install()

    def test_registry_change_after_preflight_is_caught_after_gate(self):
        state = self.home / ".local/state/astra-herdr-relay"
        state.mkdir(parents=True)
        registry = state / "agents.json"
        registry.write_text('{"agents":{}}')
        old_relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        old_relay.parent.mkdir(parents=True)
        old_relay.write_bytes(b"old relay")
        self.systemd.installed = self.systemd.active = True
        self.systemd._set_process()
        self.systemd.on_stop = lambda: registry.write_text(
            '{"agents":{"concurrent":{"lifecycle":"BUSY"}}}')
        with self.assertRaisesRegex(self.installer.InstallError, "活动代理"):
            self.install()
        self.assertEqual(old_relay.read_bytes(), b"old relay")
        self.assertIn("BUSY", registry.read_text())
        self.assertTrue(self.systemd.active)

    def test_lock_created_after_preflight_is_caught_after_gate(self):
        state = self.home / ".local/state/astra-herdr-relay"
        state.mkdir(parents=True)
        (state / "agents.json").write_text('{"agents":{}}')
        old_relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        old_relay.parent.mkdir(parents=True)
        old_relay.write_bytes(b"old relay")
        self.systemd.installed = self.systemd.active = True
        self.systemd._set_process()
        def add_racing_lock():
            locks = state / "locks"
            locks.mkdir(parents=True, exist_ok=True)
            (locks / "new.lock").write_text("held")
        self.systemd.on_stop = add_racing_lock
        with self.assertRaisesRegex(self.installer.InstallError, "task lock"):
            self.install()
        self.assertEqual(old_relay.read_bytes(), b"old relay")
        self.assertEqual((state / "locks/new.lock").read_text(), "held")
        self.assertTrue(self.systemd.active)

    def test_unowned_orphan_daemon_is_refused_when_service_inactive(self):
        orphan = self.proc / "987"
        orphan.mkdir()
        relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        (orphan / "cmdline").write_bytes(b"python3\0" + str(relay).encode() + b"\0daemon\0")
        self.systemd.installed = True
        with self.assertRaisesRegex(self.installer.InstallError, "已有 Relay CLI"):
            self.install()
        self.assertFalse(any(command[2:3] == ["stop"] for command in self.systemd.calls))

    def test_daemon_not_matching_active_mainpid_is_refused(self):
        orphan = self.proc / "987"
        orphan.mkdir()
        relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        (orphan / "cmdline").write_bytes(b"python3\0" + str(relay).encode() + b"\0daemon\0")
        self.systemd.installed = self.systemd.active = True
        self.systemd._set_process()  # PID 123 是 systemd MainPID，987 不属于它。
        with self.assertRaisesRegex(self.installer.InstallError, "已有 Relay CLI"):
            self.install()

    def test_service_start_failure_rolls_back_program_and_service(self):
        old = self.home / ".local/lib/astra-herdr-relay/relay.py"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old relay")
        self.systemd.installed = self.systemd.active = self.systemd.enabled = True
        self.systemd._set_process()
        self.systemd.fail_next_start = True
        with self.assertRaises(self.installer.InstallError):
            self.install()
        self.assertEqual(old.read_bytes(), b"old relay")
        self.assertTrue(self.systemd.active and self.systemd.enabled)
        self.assertGreaterEqual(sum(c[:3] == ["systemctl", "--user", "start"] for c in self.systemd.calls), 2)

    def test_failure_rollback_never_changes_existing_state(self):
        state = self.home / ".local/state/astra-herdr-relay"
        state.mkdir(parents=True)
        registry = state / "agents.json"
        registry.write_bytes(b'{"agents":{"x":{"lifecycle":"IDLE"}}}')
        extra = state / "events.jsonl"
        extra.write_bytes(b"old event\n")
        before = {registry: registry.read_bytes(), extra: extra.read_bytes()}
        self.systemd.fail_all_starts = True
        with self.assertRaises(self.installer.InstallError):
            self.install()
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_all_touched_destination_symlinks_refuse_preflight(self):
        candidates = (".config/astra-herdr-relay/relay.env", ".config/astra-herdr-relay/secret.env",
                      ".codex/hooks.json", ".codex/AGENTS.md",
                      ".config/systemd/user/astra-herdr-relay.service",
                      ".local/bin/astra-relay", ".local/lib/astra-herdr-relay/relay.py")
        for relative in candidates:
            with self.subTest(relative=relative):
                home = self.base / ("home-" + str(len(list(self.base.iterdir()))))
                home.mkdir()
                link = home / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                external = self.base / (link.name + ".external")
                external.write_bytes(b"preserve")
                link.symlink_to(external)
                with self.assertRaises(self.installer.InstallError):
                    self.installer.install(self.source, home, proc_root=self.proc,
                                           runner=FakeSystemd(home, self.proc))
                self.assertEqual(external.read_bytes(), b"preserve")

    def test_existing_unit_is_updated_and_service_exec_path_verified(self):
        unit = self.home / ".config/systemd/user/astra-herdr-relay.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/tmp/obsolete/astra-relayd\n")
        self.install()
        self.assertIn("ExecStart=%h/.local/bin/astra-relayd", unit.read_text())
        self.assertIn(["systemctl", "--user", "show", "astra-herdr-relay.service",
                       "-p", "FragmentPath", "-p", "ExecStart"], self.systemd.calls)

    def test_inactive_enabled_service_state_is_preserved(self):
        unit = self.home / ".config/systemd/user/astra-herdr-relay.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("old unit")
        self.systemd.installed, self.systemd.enabled = True, True
        self.install()
        self.assertFalse(self.systemd.active)
        self.assertTrue(self.systemd.enabled)

    def test_wrong_effective_service_path_rolls_back(self):
        self.systemd.wrong_exec_path = True
        with self.assertRaisesRegex(self.installer.InstallError, "服务配置未指向"):
            self.install()

    def test_prefix_similar_service_path_is_rejected(self):
        expected = self.home / ".local/bin/astra-relayd"
        self.systemd.exec_path_override = str(expected) + "-evil"
        with self.assertRaisesRegex(self.installer.InstallError, "服务配置未指向"):
            self.install()

    def test_rollback_systemctl_failure_is_reported(self):
        old = self.home / ".local/lib/astra-herdr-relay/relay.py"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old relay")
        self.systemd.installed = self.systemd.active = self.systemd.enabled = True
        self.systemd._set_process()
        self.systemd.fail_next_start = True
        self.systemd.fail_all_starts = True
        with self.assertRaisesRegex(self.installer.InstallError, "回滚错误"):
            self.install()

    def test_manifest_tampering_refuses(self):
        (self.source / "lib/relay.py").write_bytes(b"tampered")
        with self.assertRaises(self.installer.InstallError):
            self.install(check=True)

    def test_source_symlink_refuses(self):
        path = self.source / "hooks/astra_relay_subagent_redirect.py"
        path.unlink()
        path.symlink_to(self.source / "lib/relay.py")
        with self.assertRaises(self.installer.InstallError):
            self.install(check=True)

    def test_destination_symlink_refuses_before_changes(self):
        outside = self.base / "outside"
        outside.write_text("do not touch")
        local = self.home / ".local"
        local.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(self.installer.InstallError):
            self.install()
        self.assertEqual(outside.read_text(), "do not touch")
        self.assertEqual(self.systemd.calls, [])

    def test_installed_main_pid_must_use_installed_relay_path(self):
        pid_dir = self.proc / "4321"
        pid_dir.mkdir()
        (pid_dir / "cmdline").write_bytes(b"/usr/bin/python3\0/other/relay.py\0daemon\0")
        with self.assertRaises(self.installer.InstallError):
            self.installer.verify_main_pid(self.home, "4321", proc_root=self.proc)


if __name__ == "__main__":
    unittest.main()
