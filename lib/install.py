#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

VERSION = "0.5.0"
SERVICE = "astra-herdr-relay.service"
REQUIRED_FILES = (
    "install.sh",
    "lib/install.py",
    "lib/relay.py",
    "lib/task_contract.py",
    "lib/gates.py",
    "hooks/astra_relay_subagent_redirect.py",
    "hooks/hooks.fragment.json",
    "systemd/astra-herdr-relay.service",
    "config/relay.env",
    "AGENTS.relay.block.md",
    "bin/astra-relay",
    "bin/astra-relayd",
    "bin/astra-delegate",
    "bin/astra-agent-list",
    "bin/astra-agent-release",
    "bin/astra-agent-spawn",
    "bin/astra-agent-status",
)
ENTRYPOINTS = tuple(Path(p).name for p in REQUIRED_FILES if p.startswith("bin/"))


class InstallError(RuntimeError):
    pass


def _run(runner, command, *, cwd=None):
    try:
        result = runner(command, text=True, capture_output=True, cwd=cwd, timeout=30)
    except Exception as exc:
        raise InstallError(f"命令执行失败：{command[0]}：{exc}") from exc
    return result


def _check_source(source: Path):
    if source.is_symlink() or not source.is_dir():
        raise InstallError("安装源必须是普通目录，不能是符号链接")
    manifest_path = source / "MANIFEST.sha256"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InstallError("发行包缺少普通文件 MANIFEST.sha256")
    entries = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  ([A-Za-z0-9._/-]+)", line)
        if not match:
            raise InstallError("MANIFEST.sha256 格式无效")
        digest, name = match.groups()
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name in entries:
            raise InstallError(f"MANIFEST.sha256 路径无效：{name}")
        entries[name] = digest.lower()
    for name in REQUIRED_FILES:
        if name not in entries:
            raise InstallError(f"MANIFEST.sha256 缺少关键文件：{name}")
    for name, digest in entries.items():
        path = source / name
        current = source
        parent_link = False
        for part in PurePosixPath(name).parts[:-1]:
            current = current / part
            parent_link = parent_link or current.is_symlink()
        if path.is_symlink() or parent_link or not path.is_file():
            raise InstallError(f"清单文件缺失或为符号链接：{name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise InstallError(f"清单校验失败：{name}")
    for name in REQUIRED_FILES:
        if name.endswith(".py"):
            try:
                compile((source / name).read_bytes(), name, "exec")
            except (SyntaxError, UnicodeDecodeError) as exc:
                raise InstallError(f"Python 语法检查失败：{name}: {exc}") from exc
    return entries


def _safe_root(root: Path):
    if root.is_symlink() or not root.is_dir():
        raise InstallError("目标根目录必须存在且不能是符号链接")
    root = root.resolve(strict=True)
    paths = [root / part for part in (
        ".local", ".local/bin", ".local/lib", ".local/lib/astra-herdr-relay",
        ".local/lib/astra-herdr-relay/hooks", ".local/state",
        ".local/state/astra-herdr-relay", ".local/state/astra-herdr-relay/locks",
        ".config", ".config/systemd", ".config/systemd/user",
        ".config/astra-herdr-relay", ".codex",
    )]
    for path in paths:
        if path.is_symlink():
            raise InstallError(f"目标路径包含符号链接：{path}")
    return root


def _assert_no_symlink_components(root: Path, paths):
    for path in paths:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise InstallError(f"目标路径越出安装根目录：{path}") from exc
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise InstallError(f"目标路径包含符号链接：{current}")


def _service_state(runner):
    probe = _run(runner, ["systemctl", "--user", "show-environment"])
    if probe.returncode:
        raise InstallError(f"systemd --user 不可操作：{probe.stderr.strip()}")
    result = _run(runner, ["systemctl", "--user", "show", SERVICE,
                           "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState", "-p", "MainPID"])
    if result.returncode:
        raise InstallError(f"无法查询 Relay 服务状态：{result.stderr.strip()}")
    fields = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    installed = fields.get("LoadState") not in {None, "not-found"}
    if installed and not fields.get("ActiveState"):
        raise InstallError("systemd 未能确认现有服务状态")
    return {
        "installed": installed,
        "active": fields.get("ActiveState") == "active",
        "enabled": fields.get("UnitFileState") in {"enabled", "enabled-runtime"},
        "unit_state": fields.get("UnitFileState", "disabled"),
        "main_pid": fields.get("MainPID", "0"),
    }


def _registry_preflight(root):
    state = root / ".local/state/astra-herdr-relay"
    registry = state / "agents.json"
    allowed_locks = {}
    if registry.is_symlink():
        raise InstallError("Relay registry 是符号链接，拒绝安装")
    if registry.exists() and not registry.is_file():
        raise InstallError("Relay registry 不是普通文件，拒绝安装")
    if registry.exists():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            agents = data["agents"]
            if not isinstance(agents, dict):
                raise ValueError("agents 不是对象")
        except Exception as exc:
            raise InstallError(f"Relay registry 无法安全解析：{exc}") from exc
        if any(not isinstance(record, dict) for record in agents.values()):
            raise InstallError("Relay registry 中 agent 记录格式无效")
        for name, record in agents.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
                raise InstallError("Relay registry 中 agent 名称无效")
            lifecycle = record.get("lifecycle")
            if lifecycle in {"CREATING", "STOPPING"}:
                raise InstallError("存在活动代理，拒绝安装：" + name)
            if lifecycle != "BUSY":
                continue
            task_id = record.get("current_task_id")
            if (not isinstance(task_id, str) or
                    not re.fullmatch(r"relay-[a-zA-Z0-9_-]+", task_id) or
                    any(key in record for key in ("operation", "operation_kind", "finalize_owner"))):
                raise InstallError("存在活动代理，拒绝安装：" + name)
            task_path = state / "tasks" / (task_id + ".json")
            if task_path.is_symlink() or not task_path.is_file():
                raise InstallError("存在活动代理，拒绝安装：" + name)
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise InstallError(f"Relay task 无法安全解析：{task_id}: {exc}") from exc
            settled = {"HUMAN_REVIEW", "BLOCKED"}
            if (not isinstance(task, dict) or task.get("task_id") != task_id or
                    task.get("agent") != name or task.get("phase") not in settled or
                    task.get("final_state") != task.get("phase")):
                raise InstallError("存在活动代理，拒绝安装：" + name)
            lock = state / "locks" / (name + ".lock")
            if lock.is_symlink() or not lock.is_file() or lock.read_text(encoding="utf-8") != task_id:
                raise InstallError("存在活动代理，拒绝安装：" + name)
            allowed_locks[lock] = task_id
    locks = state / "locks"
    if locks.is_symlink() or (locks.exists() and not locks.is_dir()):
        raise InstallError("Relay locks 路径不是普通目录，拒绝安装")
    seen_locks = set()
    if locks.exists():
        for path in locks.iterdir():
            if path not in allowed_locks or path.is_symlink() or not path.is_file():
                raise InstallError("存在未知或格式无效的 Relay task lock，拒绝安装")
            if path.read_text(encoding="utf-8") != allowed_locks[path]:
                raise InstallError("Relay task lock 与 settled task 不匹配，拒绝安装")
            seen_locks.add(path)
    if seen_locks != set(allowed_locks):
        raise InstallError("settled Relay task lock 在预检期间发生变化，拒绝安装")


def _relay_processes(proc_root: Path, root: Path, *, managed_pid=None):
    matches = []
    installed_relay = str(root / ".local/lib/astra-herdr-relay/relay.py").encode()
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except (OSError, PermissionError):
            continue
        if any(installed_relay in arg for arg in args):
            is_managed_daemon = b"daemon" in args and entry.name == str(managed_pid)
            if not is_managed_daemon:
                matches.append(entry.name)
    return matches


def _atomic_write(path: Path, data: bytes, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify_main_pid(root, pid, *, proc_root=Path("/proc")):
    if not pid or pid == "0" or not pid.isdecimal():
        raise InstallError("systemd 服务没有有效 MainPID")
    try:
        cmdline = (Path(proc_root) / pid / "cmdline").read_bytes().split(b"\0")
    except OSError as exc:
        raise InstallError(f"无法读取服务 MainPID 命令行：{exc}") from exc
    expected = str(Path(root) / ".local/lib/astra-herdr-relay/relay.py").encode()
    if expected not in cmdline:
        raise InstallError("systemd MainPID 未运行实际安装目录中的 relay.py")


def _load_runtime_status(path):
    spec = importlib.util.spec_from_file_location("astra_relay_installed_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.runtime_status({"agent_status": "done"}) != "done":
        raise InstallError("安装版 runtime_status 未识别 agent_status")


def install(source, root, *, runner=subprocess.run, proc_root=Path("/proc"), check=False,
            drain_timeout=5):
    source = Path(source)
    root = _safe_root(Path(root))
    entries = _check_source(source)
    _registry_preflight(root)
    service = _service_state(runner)
    processes = _relay_processes(Path(proc_root), root,
                                 managed_pid=service["main_pid"] if service["active"] else None)
    if processes:
        raise InstallError("已有 Relay CLI 正在执行：" + ", ".join(processes))
    binary = root / ".local/bin"
    library = root / ".local/lib/astra-herdr-relay"
    config = root / ".config/astra-herdr-relay"
    state = root / ".local/state/astra-herdr-relay"
    user_systemd = root / ".config/systemd/user"
    codex = root / ".codex"
    unit = user_systemd / SERVICE
    hook_config = codex / "hooks.json"
    agents = codex / "AGENTS.md"
    targets = {library / "relay.py": source / "lib/relay.py",
               library / "install.py": source / "lib/install.py",
               library / "task_contract.py": source / "lib/task_contract.py",
               library / "gates.py": source / "lib/gates.py",
               library / "hooks/astra_relay_subagent_redirect.py": source / "hooks/astra_relay_subagent_redirect.py"}
    for name in ENTRYPOINTS:
        targets[binary / name] = source / "bin" / name
    targets[unit] = source / "systemd/astra-herdr-relay.service"
    _assert_no_symlink_components(root, [*targets, hook_config, agents,
        config / "relay.env", config / "secret.env", state / "agents.json",
        state / "install-backups", binary, library, library / "hooks", config,
        state, state / "tasks", state / "semantic", state / "reviews", state / "locks",
        user_systemd, codex])
    if check:
        return {"mode": "check", "service": service, "active_cli": processes,
                "required_files": list(REQUIRED_FILES), "manifest_entries": len(entries)}
    originals = {}
    backup = state / "install-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(backup, 0o700)
    # Keep a private program/config backup. The live registry, task, lock and event
    # files are not copied back on rollback because they may advance during service restart.
    mutable = list(targets) + [hook_config, agents, config / "relay.env", config / "secret.env"]
    for path in mutable:
        originals[path] = path.read_bytes() if path.is_file() else None
        if originals[path] is not None:
            rel = path.relative_to(root)
            saved = backup / rel
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)

    try:
        if service["active"]:
            result = _run(runner, ["systemctl", "--user", "stop", SERVICE])
            if result.returncode:
                raise InstallError(f"无法安全停止 Relay：{result.stderr.strip()}")
        # Gate known entrypoints atomically while draining calls already in flight.
        maintenance = b'#!/usr/bin/env bash\necho "Relay installation in progress" >&2\nexit 75\n'
        for path in sorted((root / ".local/bin").glob("astra-*")):
            if path.is_file() and path.name in ENTRYPOINTS:
                mode = stat.S_IMODE(path.stat().st_mode)
                _atomic_write(path, maintenance, mode)
        deadline = time.monotonic() + drain_timeout
        while True:
            active = _relay_processes(Path(proc_root), root)
            if not active: break
            if time.monotonic() >= deadline:
                raise InstallError("Relay CLI 未能在维护窗口内退出：" + ", ".join(active))
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

        # 门禁和排空之间仍可能有并发任务建立；在任何程序/state写入前复核。
        _registry_preflight(root)
        late_processes = _relay_processes(Path(proc_root), root)
        if late_processes:
            raise InstallError("维护门禁后仍有 Relay 进程：" + ", ".join(late_processes))

        for directory in (binary, library, library / "hooks", config, state, state / "tasks",
                          state / "semantic", state / "reviews", state / "locks", user_systemd, codex):
            directory.mkdir(parents=True, exist_ok=True)
        for target, src in targets.items():
            old_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else (0o755 if target in targets and (target.parent == binary or target.name.endswith(".py")) else 0o644)
            _atomic_write(target, src.read_bytes(), old_mode)
        hook_dst = library / "hooks/astra_relay_subagent_redirect.py"
        _atomic_write(hook_dst, (source / "hooks/astra_relay_subagent_redirect.py").read_bytes(),
                      stat.S_IMODE(hook_dst.stat().st_mode) or 0o755)
        if not (config / "relay.env").exists():
            _atomic_write(config / "relay.env", (source / "config/relay.env").read_bytes(), 0o600)
        if not (config / "secret.env").exists():
            _atomic_write(config / "secret.env", b"", 0o600)
        if not (state / "agents.json").exists():
            _atomic_write(state / "agents.json", b'{"version":"0.5.0","agents":{}}\n', 0o600)

        # 将 Relay Hook 合并进用户配置，保留其他事件与 Hook。
        fragment = json.loads((source / "hooks/hooks.fragment.json").read_text(encoding="utf-8"))
        if hook_config.exists():
            existing = json.loads(hook_config.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise InstallError("Codex hooks.json 顶层必须是对象")
            hooks = existing.setdefault("hooks", {})
            if not isinstance(hooks, dict):
                raise InstallError("Codex hooks.json hooks 字段必须是对象")
            for event, rules in fragment["hooks"].items():
                current = hooks.setdefault(event, [])
                if not isinstance(current, list):
                    raise InstallError(f"Codex hooks.json {event} 必须是数组")
                for rule in rules:
                    if rule not in current:
                        current.append(rule)
            _atomic_write(hook_config, json.dumps(existing, ensure_ascii=False, indent=2).encode() + b"\n",
                          stat.S_IMODE(hook_config.stat().st_mode))
        else:
            _atomic_write(hook_config, json.dumps(fragment, ensure_ascii=False, indent=2).encode() + b"\n", 0o600)
        relay_block = (source / "AGENTS.relay.block.md").read_text(encoding="utf-8").strip()
        if agents.exists():
            text = agents.read_text(encoding="utf-8")
            start, end = "<!-- ASTRA-HERDR-RELAY-V050-BEGIN -->", "<!-- ASTRA-HERDR-RELAY-V050-END -->"
            if start in text and end in text:
                before = text.split(start, 1)[0]
                after = text.split(end, 1)[1]
                text = before.rstrip() + "\n" + relay_block + "\n" + after.lstrip()
            elif start in text or end in text:
                raise InstallError("AGENTS.md 中 Relay 标记不完整")
            else:
                text = text.rstrip() + "\n\n" + relay_block + "\n"
            _atomic_write(agents, text.encode("utf-8"), stat.S_IMODE(agents.stat().st_mode))
        else:
            _atomic_write(agents, relay_block.encode("utf-8") + b"\n", 0o600)

        result = _run(runner, ["systemctl", "--user", "daemon-reload"])
        if result.returncode: raise InstallError(f"systemd daemon-reload 失败：{result.stderr.strip()}")
        _verify_service_path(runner, root, unit)
        if service["installed"] and service["active"]:
            command = ["systemctl", "--user", "start", SERVICE]
        elif not service["installed"]:
            command = ["systemctl", "--user", "enable", "--now", SERVICE]
        else:
            command = None
        if command:
            result = _run(runner, command)
            if result.returncode: raise InstallError(f"Relay 启动/启用失败：{result.stderr.strip()}")

        for target, src in targets.items():
            if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(src.read_bytes()).digest():
                raise InstallError(f"安装文件哈希不匹配：{target}")
        compile((library / "relay.py").read_bytes(), str(library / "relay.py"), "exec")
        _load_runtime_status(library / "relay.py")
        if service["active"] or not service["installed"]:
            state_after = _service_state(runner)
            if not state_after["active"]:
                raise InstallError("安装后的 Relay 服务未运行")
            verify_main_pid(root, state_after["main_pid"], proc_root=proc_root)
        state_after = _service_state(runner)
        if state_after["active"] != (service["active"] or not service["installed"]):
            raise InstallError("安装后服务 active 状态与预期不符")
        if service["installed"] and state_after["unit_state"] != service["unit_state"]:
            raise InstallError("安装后服务 enabled 状态与原状态不符")
        return {"mode": "installed", "backup": str(backup), "service_before": service,
                "files": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in targets}}
    except Exception as exc:
        rollback_errors = []

        def rollback_command(args):
            try:
                result = _run(runner, ["systemctl", "--user", *args])
                if result.returncode:
                    rollback_errors.append(f"systemctl {' '.join(args)}: {result.stderr.strip() or result.returncode}")
                return result.returncode == 0
            except Exception as rollback_exc:
                rollback_errors.append(f"systemctl {' '.join(args)}: {rollback_exc}")
                return False

        # 停止/禁用新服务必须在回滚移除新 unit 之前完成。
        try:
            current_service = _service_state(runner)
        except Exception as rollback_exc:
            rollback_errors.append(f"读取回滚前 service 状态：{rollback_exc}")
            current_service = None
        if current_service and current_service["active"]:
            rollback_command(["stop", SERVICE])
        if not service["installed"] and current_service and current_service["enabled"]:
            rollback_command(["disable", SERVICE])
        for path, data in originals.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
                    _atomic_write(path, data, mode)
            except OSError as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        rollback_command(["daemon-reload"])
        if service["installed"]:
            try:
                current_service = _service_state(runner)
            except Exception as rollback_exc:
                rollback_errors.append(f"读取回滚后 service 状态：{rollback_exc}")
                current_service = None
            if current_service:
                if current_service["enabled"] != service["enabled"]:
                    action = "enable" if service["enabled"] else "disable"
                    if service["unit_state"] == "enabled-runtime" and service["enabled"]:
                        rollback_command([action, "--runtime", SERVICE])
                    else:
                        rollback_command([action, SERVICE])
                if current_service["active"] != service["active"]:
                    rollback_command(["start" if service["active"] else "stop", SERVICE])
                try:
                    final_service = _service_state(runner)
                    if final_service["active"] != service["active"]:
                        rollback_errors.append("回滚后的 active 状态与安装前不一致")
                    if final_service["enabled"] != service["enabled"]:
                        rollback_errors.append("回滚后的 enabled 状态与安装前不一致")
                except Exception as rollback_exc:
                    rollback_errors.append(f"验证回滚 service 状态：{rollback_exc}")
        else:
            try:
                final_service = _service_state(runner)
                if final_service["active"] or final_service["enabled"]:
                    rollback_errors.append("回滚后的新 service 仍 active 或 enabled")
            except Exception as rollback_exc:
                rollback_errors.append(f"验证新 service 已恢复未安装状态：{rollback_exc}")
        if isinstance(exc, InstallError) and not rollback_errors:
            raise
        detail = "; 回滚错误：" + "; ".join(rollback_errors) if rollback_errors else ""
        prefix = str(exc) if isinstance(exc, InstallError) else f"增量安装失败：{exc}"
        raise InstallError(f"{prefix}；已尝试回滚{detail}") from exc


def _verify_service_path(runner, root, unit_path):
    result = _run(runner, ["systemctl", "--user", "show", SERVICE, "-p", "FragmentPath", "-p", "ExecStart"])
    if result.returncode:
        raise InstallError(f"无法核验 systemd 服务路径：{result.stderr.strip()}")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    expected_unit = str(unit_path)
    expected_exec = str(root / ".local/bin/astra-relayd")
    exec_paths = re.findall(r"(?:^|[;{])\s*path=([^;]*?)\s*(?=;|$)", fields.get("ExecStart", ""))
    if (fields.get("FragmentPath") != expected_unit or len(exec_paths) != 1 or
            exec_paths[0].strip() != expected_exec):
        raise InstallError("systemd 服务配置未指向本次安装的 unit 与 astra-relayd")


def main(argv=None):
    parser = argparse.ArgumentParser(description="保留 Relay 配置与状态的安全安装器")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--root", type=Path, default=Path.home())
    parser.add_argument("--check", action="store_true", help="只读预检，不进行安装")
    args = parser.parse_args(argv)
    try:
        result = install(args.source, args.root, check=args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
