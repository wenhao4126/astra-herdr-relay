import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
import subprocess
from unittest.mock import patch

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
        self.fail_next_daemon_reload = False
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
            if self.fail_next_daemon_reload:
                self.fail_next_daemon_reload = False
                result.returncode, result.stderr = 1, "reload failed"
            else:
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
        elif command[:3] == ["systemctl", "--user", "disable"]:
            self.enabled = False
        elif command[:3] == ["systemctl", "--user", "enable"]:
            self.enabled = True
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

    def refresh_manifest(self):
        manifest = "".join(f"{hashlib.sha256((self.source / rel).read_bytes()).hexdigest()}  {rel}\n"
                           for rel in self.installer.REQUIRED_FILES)
        (self.source / "MANIFEST.sha256").write_text(manifest, encoding="utf-8")

    def test_install_ledger_is_private_and_list_backups_is_read_only(self):
        result = self.install()
        state = self.home / ".local/state/astra-herdr-relay"
        ledger = state / "install-ledgers" / f"{result['backup_id']}.json"
        backup = state / "install-backups" / result["backup_id"]
        self.assertEqual(state.joinpath("install-ledgers").stat().st_mode & 0o777, 0o700)
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        listing = self.installer.list_backups(self.home)
        after = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(listing["backups"][0]["backup_id"], result["backup_id"])

    def test_uninstall_and_rollback_check_are_read_only(self):
        installed = self.install()
        calls_before = len(self.systemd.calls)
        before = sorted((str(p.relative_to(self.home)), p.is_dir(),
                         p.read_bytes() if p.is_file() else None) for p in self.home.rglob("*"))
        self.assertEqual(self.installer.uninstall(self.home, runner=self.systemd, proc_root=self.proc,
                                                  check=True)["mode"], "check")
        self.assertEqual(self.installer.rollback(self.home, installed["backup_id"], runner=self.systemd,
                                                 proc_root=self.proc, check=True)["mode"], "check")
        after = sorted((str(p.relative_to(self.home)), p.is_dir(),
                        p.read_bytes() if p.is_file() else None) for p in self.home.rglob("*"))
        self.assertEqual(before, after)
        self.assertFalse(any(command[2:3] in (["stop"], ["start"], ["disable"], ["enable"])
                             for command in self.systemd.calls[calls_before:]))

    def test_uninstall_preserves_user_hook_agents_config_state_and_backups(self):
        installed = self.install()
        hook = self.home / ".codex/hooks.json"
        data = json.loads(hook.read_text())
        user_rule = {"matcher": "UserTool", "hooks": [{"type": "command", "command": "echo user"}]}
        data["hooks"].setdefault("PreToolUse", []).append(user_rule)
        relay_rule = data["hooks"]["PreToolUse"][0]
        data["hooks"]["PreToolUse"].append(relay_rule)
        data["hooks"]["Notification"] = [{"matcher": "*", "hooks": [{"type": "command", "command": "notify"}]}]
        hook.write_text(json.dumps(data))
        agents = self.home / ".codex/AGENTS.md"
        with agents.open("a") as stream:
            stream.write("\n用户新增说明\n")
        config = self.home / ".config/astra-herdr-relay/secret.env"
        config.write_text("USER_SECRET=keep\n")
        task = self.home / ".local/state/astra-herdr-relay/tasks/keep.json"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text('{"task_id":"keep"}\n')
        retained = (config.read_bytes(), task.read_bytes())

        result = self.installer.uninstall(self.home, runner=self.systemd, proc_root=self.proc)

        self.assertEqual(result["mode"], "uninstalled")
        self.assertEqual((config.read_bytes(), task.read_bytes()), retained)
        after_hooks = json.loads(hook.read_text())["hooks"]
        self.assertIn(user_rule, after_hooks["PreToolUse"])
        self.assertEqual(after_hooks["PreToolUse"].count(relay_rule), 1)
        self.assertIn("Notification", after_hooks)
        self.assertIn("用户新增说明", agents.read_text())
        self.assertNotIn("ASTRA-HERDR-RELAY-V050-BEGIN", agents.read_text())
        self.assertFalse((self.home / ".local/lib/astra-herdr-relay/relay.py").exists())
        self.assertFalse((self.home / ".config/systemd/user/astra-herdr-relay.service").exists())
        self.assertFalse(self.systemd.active or self.systemd.enabled)
        self.assertTrue((self.home / ".local/state/astra-herdr-relay/install-backups" / installed["backup_id"]).is_dir())

    def test_uninstall_preserves_empty_hooks_objects_and_user_rules(self):
        fragment=json.loads((self.source/"hooks/hooks.fragment.json").read_text())
        relay_only={"hooks":fragment["hooks"]}
        user_rule={"matcher":"UserTool","hooks":[{"type":"command","command":"echo user"}]}
        cases=(
            ("empty_object",{},{}),
            ("relay_only",relay_only,{}),
            ("user_rule",{"hooks":{"PreToolUse":[user_rule]}},{"hooks":{"PreToolUse":[user_rule]}}),
        )
        for label,before,expected in cases:
            with self.subTest(hooks=label):
                path=self.home/".codex/hooks.json"
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(before),encoding="utf-8")
                self.install()
                self.installer.uninstall(self.home,runner=self.systemd,proc_root=self.proc)
                self.assertTrue(path.is_file())
                self.assertEqual(json.loads(path.read_text()),expected)

    def test_first_install_rollback_preserves_preexisting_empty_hooks_object(self):
        path=self.home/".codex/hooks.json"
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("{}\n",encoding="utf-8")
        installed=self.install()
        result=self.installer.rollback(self.home,installed["backup_id"],
                                       runner=self.systemd,proc_root=self.proc)
        self.assertIsNone(result["restored_install_id"])
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text()),{})

    def test_first_install_rollback_does_not_restore_dangling_relay_hooks(self):
        fragment=json.loads((self.source/"hooks/hooks.fragment.json").read_text())
        path=self.home/".codex/hooks.json"
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({"hooks":fragment["hooks"]}),encoding="utf-8")

        installed=self.install()
        self.installer.rollback(self.home,installed["backup_id"],runner=self.systemd,
                                proc_root=self.proc)

        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text()),{})
        self.assertFalse((self.home/".local/lib/astra-herdr-relay/hooks/astra_relay_subagent_redirect.py").exists())

    def test_rollback_upgrade_restores_previous_program_and_keeps_new_user_data(self):
        first = self.install()
        relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        previous = relay.read_bytes()
        hook_path = self.home / ".codex/hooks.json"
        previous_hook = json.loads(hook_path.read_text())["hooks"]["PreToolUse"][0]
        new_source = self.source / "lib/relay.py"
        new_source.write_bytes(new_source.read_bytes() + b"\n# upgrade payload\n")
        fragment_path = self.source / "hooks/hooks.fragment.json"
        fragment = json.loads(fragment_path.read_text())
        fragment["hooks"]["PreToolUse"][0]["hooks"][0]["statusMessage"] = "Updated Relay routing"
        fragment_path.write_text(json.dumps(fragment), encoding="utf-8")
        self.refresh_manifest()
        second = self.install()
        self.assertNotEqual(relay.read_bytes(), previous)
        config = self.home / ".config/astra-herdr-relay/secret.env"
        config.write_text("NEW_SECRET=keep\n")
        task = self.home / ".local/state/astra-herdr-relay/tasks/forward.json"
        task.write_text('{"task_id":"forward"}\n')
        hook = hook_path
        data = json.loads(hook.read_text())
        user_rule = {"matcher": "UserTool", "hooks": [{"type": "command", "command": "echo user"}]}
        data["hooks"].setdefault("PreToolUse", []).append(user_rule)
        hook.write_text(json.dumps(data))
        agents = self.home / ".codex/AGENTS.md"
        with agents.open("a") as stream:
            stream.write("\nupgrade 后用户内容\n")

        result = self.installer.rollback(self.home, second["backup_id"], runner=self.systemd,
                                         proc_root=self.proc)

        self.assertEqual(result["restored_install_id"], first["backup_id"])
        self.assertEqual(relay.read_bytes(), previous)
        self.assertEqual(config.read_text(), "NEW_SECRET=keep\n")
        self.assertEqual(task.read_text(), '{"task_id":"forward"}\n')
        after_hooks = json.loads(hook.read_text())["hooks"]["PreToolUse"]
        self.assertIn(user_rule, after_hooks)
        self.assertIn(previous_hook, after_hooks)
        self.assertNotIn(fragment["hooks"]["PreToolUse"][0], after_hooks)
        self.assertIn("upgrade 后用户内容", agents.read_text())
        self.assertTrue(self.systemd.active and self.systemd.enabled)

    def test_legacy_program_rollback_restores_its_relay_hook(self):
        unit = self.home / ".config/systemd/user/astra-herdr-relay.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("legacy relay unit\n")
        relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        relay.parent.mkdir(parents=True)
        relay.write_text("legacy relay program\n")
        fragment=json.loads((self.source/"hooks/hooks.fragment.json").read_text())
        hook_path=self.home/".codex/hooks.json"
        hook_path.parent.mkdir(parents=True,exist_ok=True)
        hook_path.write_text(json.dumps({"hooks":fragment["hooks"]}),encoding="utf-8")
        self.systemd.installed = True

        installed = self.install()
        self.installer.rollback(self.home, installed["backup_id"], runner=self.systemd,
                                proc_root=self.proc)

        self.assertEqual(relay.read_text(), "legacy relay program\n")
        self.assertEqual(json.loads(hook_path.read_text()), {"hooks":fragment["hooks"]})

    def test_rollback_first_install_restores_uninstalled_program_state_only(self):
        installed = self.install()
        config = self.home / ".config/astra-herdr-relay/secret.env"
        config.write_text("CURRENT_SECRET=keep\n")
        task = self.home / ".local/state/astra-herdr-relay/tasks/keep.json"
        task.write_text('{"task_id":"keep"}\n')

        result = self.installer.rollback(self.home, installed["backup_id"], runner=self.systemd,
                                         proc_root=self.proc)

        self.assertIsNone(result["restored_install_id"])
        self.assertTrue(all(not path.exists() for path in self.installer._installed_targets(self.home)))
        self.assertFalse((self.home / ".codex/hooks.json").exists())
        self.assertFalse((self.home / ".codex/AGENTS.md").exists())
        self.assertEqual(config.read_text(), "CURRENT_SECRET=keep\n")
        self.assertEqual(task.read_text(), '{"task_id":"keep"}\n')
        self.assertFalse(self.systemd.active or self.systemd.enabled)

    def test_uninstall_restores_preexisting_unowned_target_and_mode(self):
        old = self.home / ".local/bin/astra-relay"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"preexisting wrapper\n")
        old.chmod(0o711)
        installed = self.install()
        self.assertNotEqual(old.read_bytes(), b"preexisting wrapper\n")

        self.installer.uninstall(self.home, runner=self.systemd, proc_root=self.proc)

        self.assertEqual(old.read_bytes(), b"preexisting wrapper\n")
        self.assertEqual(old.stat().st_mode & 0o777, 0o711)

    def test_legacy_install_upgrade_creates_owned_ledger_for_uninstall(self):
        unit = self.home / ".config/systemd/user/astra-herdr-relay.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("legacy relay unit\n")
        self.systemd.installed = True
        installed = self.install()
        ledger = self.installer._load_ledger(self.home, installed["backup_id"])
        self.assertTrue(ledger["legacy_install"])

        self.installer.uninstall(self.home, runner=self.systemd, proc_root=self.proc)

        self.assertFalse(unit.exists())
        self.assertFalse(self.systemd.active or self.systemd.enabled)

    def test_uninstall_busy_agent_refuses_without_stopping_service(self):
        self.install()
        state = self.home / ".local/state/astra-herdr-relay"
        (state / "agents.json").write_text(json.dumps({"agents": {"worker": {"lifecycle": "BUSY"}}}))
        before = (self.home / ".local/lib/astra-herdr-relay/relay.py").read_bytes()
        with self.assertRaisesRegex(self.installer.InstallError, "活动代理"):
            self.installer.uninstall(self.home, runner=self.systemd, proc_root=self.proc)
        self.assertEqual((self.home / ".local/lib/astra-herdr-relay/relay.py").read_bytes(), before)
        self.assertFalse(any(command[:3] == ["systemctl", "--user", "stop"] for command in self.systemd.calls))

    def test_uninstall_modified_program_refuses_before_service_change(self):
        self.install()
        relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        relay.write_bytes(b"user modification")
        calls_before = len(self.systemd.calls)
        with self.assertRaisesRegex(self.installer.InstallError, "已被修改"):
            self.installer.uninstall(self.home, runner=self.systemd, proc_root=self.proc)
        self.assertEqual(relay.read_bytes(), b"user modification")
        self.assertFalse(any(command[:3] == ["systemctl", "--user", "stop"]
                             for command in self.systemd.calls[calls_before:]))

    def test_rollback_rejects_tampered_and_escaping_backup_before_changes(self):
        prior_relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        prior_relay.parent.mkdir(parents=True)
        prior_relay.write_bytes(b"previous relay")
        installed = self.install()
        _, ledgers, backups, _ = self.installer._metadata_paths(self.home)
        ledger_path = ledgers / f"{installed['backup_id']}.json"
        ledger = json.loads(ledger_path.read_text())
        backup_rel = ledger["originals"][".local/lib/astra-herdr-relay/relay.py"]["backup_path"]
        outside = self.base / "outside"
        outside.write_bytes(b"untouched")
        (self.home / backup_rel).unlink()
        (self.home / backup_rel).symlink_to(outside)
        with self.assertRaises(self.installer.InstallError):
            self.installer.rollback(self.home, installed["backup_id"], runner=self.systemd,
                                    proc_root=self.proc)
        self.assertEqual(outside.read_bytes(), b"untouched")
        self.assertTrue((self.home / ".local/lib/astra-herdr-relay/relay.py").is_file())
        self.assertFalse(any(command[:3] == ["systemctl", "--user", "stop"] for command in self.systemd.calls))
        self.assertTrue(backups.is_dir())

    def test_rollback_rejects_backup_path_escape_and_unknown_id(self):
        prior = self.home / ".local/lib/astra-herdr-relay/relay.py"
        prior.parent.mkdir(parents=True)
        prior.write_bytes(b"prior")
        installed = self.install()
        _, ledgers, _, _ = self.installer._metadata_paths(self.home)
        ledger_path = ledgers / f"{installed['backup_id']}.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["originals"][".local/lib/astra-herdr-relay/relay.py"]["backup_path"] = "../../outside"
        ledger_path.write_text(json.dumps(ledger))
        outside = self.base / "outside"
        outside.write_bytes(b"outside")
        calls_before = len(self.systemd.calls)
        with self.assertRaisesRegex(self.installer.InstallError, "备份路径"):
            self.installer.rollback(self.home, installed["backup_id"], runner=self.systemd,
                                    proc_root=self.proc)
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertFalse(any(command[:3] == ["systemctl", "--user", "stop"]
                             for command in self.systemd.calls[calls_before:]))
        with self.assertRaisesRegex(self.installer.InstallError, "版本链"):
            self.installer.rollback(self.home, "0" * 32, runner=self.systemd, proc_root=self.proc)

    def test_rollback_rejects_backup_hash_tampering(self):
        prior = self.home / ".local/lib/astra-herdr-relay/relay.py"
        prior.parent.mkdir(parents=True)
        prior.write_bytes(b"prior")
        installed = self.install()
        ledger = self.installer._load_ledger(self.home, installed["backup_id"])
        backup_rel = ledger["originals"][".local/lib/astra-herdr-relay/relay.py"]["backup_path"]
        (self.home / backup_rel).write_bytes(b"tampered")
        with self.assertRaisesRegex(self.installer.InstallError, "哈希校验失败"):
            self.installer.rollback(self.home, installed["backup_id"], runner=self.systemd,
                                    proc_root=self.proc)
        self.assertTrue(self.systemd.active)

    def test_rollback_failure_restores_current_install_and_pointer(self):
        first = self.install()
        relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        previous = relay.read_bytes()
        source_relay = self.source / "lib/relay.py"
        source_relay.write_bytes(source_relay.read_bytes() + b"\n# newer\n")
        self.refresh_manifest()
        second = self.install()
        current = relay.read_bytes()
        self.systemd.fail_next_start = True
        with self.assertRaisesRegex(self.installer.InstallError, "回滚失败"):
            self.installer.rollback(self.home, second["backup_id"], runner=self.systemd,
                                    proc_root=self.proc)
        self.assertEqual(relay.read_bytes(), current)
        self.assertEqual(self.installer._load_current(self.home), second["backup_id"])
        self.assertTrue(self.systemd.active and self.systemd.enabled)
        self.assertNotEqual(relay.read_bytes(), previous)

    def test_uninstall_failure_restores_files_service_hooks_and_config(self):
        self.install()
        relay = self.home / ".local/lib/astra-herdr-relay/relay.py"
        before = relay.read_bytes()
        hooks = (self.home / ".codex/hooks.json").read_bytes()
        config = self.home / ".config/astra-herdr-relay/secret.env"
        config.write_text("KEEP=secret\n")
        self.systemd.fail_next_daemon_reload = True
        with self.assertRaisesRegex(self.installer.InstallError, "卸载失败"):
            self.installer.uninstall(self.home, runner=self.systemd, proc_root=self.proc)
        self.assertEqual(relay.read_bytes(), before)
        self.assertEqual((self.home / ".codex/hooks.json").read_bytes(), hooks)
        self.assertEqual(config.read_text(), "KEEP=secret\n")
        self.assertTrue(self.systemd.active and self.systemd.enabled)
        self.assertTrue((self.home / ".local/state/astra-herdr-relay/install-errors.jsonl").is_file())

    def test_check_mode_is_read_only(self):
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        self.assertEqual(self.install(check=True)["mode"], "check")
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")), before)

    def test_real_systemd_runner_rejects_non_home_root_before_service_query(self):
        with patch.object(self.installer,"_service_state") as service:
            with self.assertRaisesRegex(self.installer.InstallError,"--root 必须等于当前用户 HOME"):
                self.installer._preflight(self.home,subprocess.run,self.proc)
        service.assert_not_called()

    def test_unknown_service_transition_state_refuses_maintenance(self):
        class UnknownService:
            def __call__(self, command, **kwargs):
                class Result:
                    returncode, stdout, stderr = 0, "", ""
                result = Result()
                if command[-1:] == ["show-environment"]:
                    return result
                result.stdout = "LoadState=loaded\nActiveState=activating\nUnitFileState=enabled\nMainPID=123\n"
                return result

        with self.assertRaisesRegex(self.installer.InstallError, "未知或正在转换"):
            self.installer._service_state(UnknownService())

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
