#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

VERSION = "0.5.0"
SERVICE = "astra-herdr-relay.service"
REQUIRED_FILES = (
    "install.sh",
    "uninstall.sh",
    "rollback.sh",
    "lib/install.py",
    "lib/relay.py",
    "lib/task_contract.py",
    "lib/doc_registry.py",
    "lib/artifact.py",
    "lib/evidence.py",
    "lib/stages.py",
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
OWNED_START = "<!-- ASTRA-HERDR-RELAY-V050-BEGIN -->"
OWNED_END = "<!-- ASTRA-HERDR-RELAY-V050-END -->"


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
    required = {"LoadState", "ActiveState", "UnitFileState", "MainPID"}
    if not required.issubset(fields):
        raise InstallError("systemd 服务状态字段不完整，按未知状态拒绝操作")
    if fields["LoadState"] not in {"loaded", "not-found"}:
        raise InstallError("systemd Relay unit 状态未知，拒绝操作")
    if fields["ActiveState"] not in {"active", "inactive"}:
        raise InstallError("systemd Relay active 状态未知或正在转换，拒绝操作")
    if fields["UnitFileState"] not in {"enabled", "enabled-runtime", "disabled"}:
        raise InstallError("systemd Relay enabled 状态未知，拒绝操作")
    installed = fields["LoadState"] == "loaded"
    if installed and not fields.get("ActiveState"):
        raise InstallError("systemd 未能确认现有服务状态")
    if fields["ActiveState"] == "active" and (not fields["MainPID"].isdecimal() or fields["MainPID"] == "0"):
        raise InstallError("systemd Relay MainPID 无效，按未知状态拒绝操作")
    if not installed and (fields["ActiveState"] == "active" or fields["UnitFileState"] != "disabled"):
        raise InstallError("systemd 报告 unit 不存在但服务仍 active/enabled，拒绝操作")
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


def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise InstallError(f"Relay 私有目录不安全：{path}")
    os.chmod(path, 0o700)


def _metadata_paths(root):
    state = root / ".local/state/astra-herdr-relay"
    return state, state / "install-ledgers", state / "install-backups", state / "current-install.json"


@contextmanager
def _operation_lock(root):
    state, _, _, _ = _metadata_paths(root)
    path = state / "install.lock"
    _assert_no_symlink_components(root, [state, path])
    _private_dir(state)
    if path.exists() and (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1):
        raise InstallError("Relay 安装锁不是安全的普通文件")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
            raise InstallError("Relay 安装锁权限过宽")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_json(path, value):
    _private_dir(path.parent)
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(), 0o600)


def _load_json_file(path, *, label):
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"{label} 缺失或不是普通文件")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise InstallError(f"{label} 无法安全解析") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{label} 结构无效")
    return value


def _load_current(root):
    state, ledgers, backups, pointer = _metadata_paths(root)
    _assert_no_symlink_components(root, [pointer])
    for private in (ledgers, backups):
        if private.exists() and stat.S_IMODE(private.stat().st_mode) & 0o077:
            raise InstallError("Relay 私有状态目录权限过宽")
    if not pointer.exists():
        if pointer.is_symlink():
            raise InstallError("当前安装指针是符号链接")
        return None
    data = _load_json_file(pointer, label="当前安装指针")
    if stat.S_IMODE(pointer.stat().st_mode) & 0o077:
        raise InstallError("当前安装指针权限过宽")
    install_id = data.get("install_id")
    if not isinstance(install_id, str) or not re.fullmatch(r"[0-9a-f]{32}", install_id):
        raise InstallError("当前安装指针的 ID 无效")
    return install_id


def _load_ledger(root, install_id):
    if not isinstance(install_id, str) or not re.fullmatch(r"[0-9a-f]{32}", install_id):
        raise InstallError("备份 ID 格式无效")
    _, ledgers, _, _ = _metadata_paths(root)
    path = ledgers / (install_id + ".json")
    _assert_no_symlink_components(root, [path])
    ledger = _load_json_file(path, label="安装账本")
    if (ledger.get("install_id") != install_id or ledger.get("backup_id") != install_id or
            ledger.get("schema_version") != 1):
        raise InstallError("安装账本身份或版本无效")
    if stat.S_IMODE(ledgers.stat().st_mode) & 0o077 or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise InstallError("安装账本权限过宽")
    expected_paths = {p.relative_to(root).as_posix() for p in _installed_targets(root)}
    hashes = ledger.get("post_sha256")
    originals = ledger.get("originals")
    if (not isinstance(hashes, dict) or set(hashes) != expected_paths or
            not isinstance(originals, dict) or set(originals) != expected_paths or
            ledger.get("managed_programs") != sorted(expected_paths)):
        raise InstallError("安装账本受管程序清单不完整")
    for digest in hashes.values():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InstallError("安装账本程序 SHA-256 无效")
    for record in originals.values():
        if not isinstance(record, dict) or type(record.get("existed")) is not bool:
            raise InstallError("安装账本原文件记录无效")
        if record["existed"]:
            if (not isinstance(record.get("sha256"), str) or
                    not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) or
                    type(record.get("mode")) is not int or not 0 <= record["mode"] <= 0o777 or
                    not isinstance(record.get("backup_path"), str)):
                raise InstallError("安装账本原文件快照字段无效")
        elif any(record.get(key) is not None for key in ("sha256", "mode", "backup_path")):
            raise InstallError("不存在的原文件不应包含快照数据")
    parent = ledger.get("parent_install_id")
    if parent is not None and (not isinstance(parent, str) or not re.fullmatch(r"[0-9a-f]{32}", parent)):
        raise InstallError("安装账本版本链 ID 无效")
    if parent == install_id:
        raise InstallError("安装账本不能将自身设为父版本")
    if type(ledger.get("legacy_install")) is not bool:
        raise InstallError("安装账本兼容状态无效")
    service = ledger.get("service_before")
    if (not isinstance(service, dict) or any(type(service.get(k)) is not bool for k in ("installed", "active", "enabled")) or
            service.get("unit_state") not in {"enabled", "enabled-runtime", "disabled"}):
        raise InstallError("安装账本服务状态无效")
    return ledger


def _record_error(root, operation, exc):
    state, _, _, _ = _metadata_paths(root)
    try:
        _private_dir(state)
        path = state / "install-errors.jsonl"
        _assert_no_symlink_components(root, [path])
        if path.exists() and (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1):
            raise InstallError("Relay 错误日志不是安全的普通文件")
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "operation": operation, "error_type": type(exc).__name__, "error": str(exc)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _checked_backup(root, ledger, rel, record):
    if not record.get("existed"):
        return None
    backup_id = ledger.get("backup_id")
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}", backup_id):
        raise InstallError("账本备份 ID 无效")
    backup_rel = record.get("backup_path")
    expected_rel = (_metadata_paths(root)[2] / backup_id / rel).relative_to(root).as_posix()
    if backup_rel != expected_rel:
        raise InstallError("备份路径与账本 ID 不匹配")
    path = root / backup_rel
    _assert_no_symlink_components(root, [path])
    backup_root = _metadata_paths(root)[2].resolve(strict=True)
    if stat.S_IMODE(backup_root.stat().st_mode) & 0o077 or stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise InstallError("备份目录权限过宽")
    try:
        path.resolve(strict=True).relative_to(backup_root)
    except (OSError, ValueError) as exc:
        raise InstallError("备份路径越界或不存在") from exc
    if path.is_symlink() or not path.is_file():
        raise InstallError("备份文件缺失或不是普通文件")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise InstallError("备份文件权限过宽")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != record.get("sha256"):
        raise InstallError("备份文件哈希校验失败")
    return data


def _restore_records(root, ledger):
    result = {}
    records = ledger.get("originals")
    if not isinstance(records, dict):
        raise InstallError("安装账本缺少原文件快照")
    for rel, record in records.items():
        path_rel = PurePosixPath(rel)
        if path_rel.is_absolute() or ".." in path_rel.parts or not isinstance(record, dict):
            raise InstallError("原文件快照路径无效")
        path = root.joinpath(*path_rel.parts)
        _assert_no_symlink_components(root, [path])
        data = _checked_backup(root, ledger, rel, record)
        result[path] = (data, record.get("mode"))
    return result


def _read_owned_agents(path):
    if not path.exists():
        return None, None
    if path.is_symlink() or not path.is_file():
        raise InstallError("AGENTS.md 不是普通文件")
    text = path.read_text(encoding="utf-8")
    if text.count(OWNED_START) != text.count(OWNED_END) or text.count(OWNED_START) > 1:
        raise InstallError("AGENTS.md 中 Relay 标记缺失、重复或不完整")
    if OWNED_START not in text:
        return text, None
    body = text[text.index(OWNED_START):text.index(OWNED_END) + len(OWNED_END)]
    return text, body


def _replace_agents_block(path, expected, replacement, *, preserve_empty=True):
    text, current = _read_owned_agents(path)
    if current != expected:
        raise InstallError("AGENTS.md 的 Relay 自有块已变化，拒绝覆盖")
    if text is None:
        text = ""
    if current is not None:
        before, _, tail = text.partition(current)
        text = before + tail
    if replacement is not None:
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n" if text.strip() else "") + replacement + "\n"
    if text.strip():
        _atomic_write(path, text.encode("utf-8"), stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
    elif path.exists():
        if preserve_empty:
            _atomic_write(path, text.encode("utf-8"), stat.S_IMODE(path.stat().st_mode))
        else:
            path.unlink()


def _mutate_owned_hooks(path, expected, replacement, *, preserve_empty=True):
    existed = path.exists()
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise InstallError("Codex hooks.json 不是普通文件")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise InstallError("Codex hooks.json 无法安全解析") from exc
        if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
            raise InstallError("Codex hooks.json 结构无效")
    else:
        data = {"hooks": {}}
    hooks = data.setdefault("hooks", {})
    for event, rules in expected.items():
        current = hooks.get(event, [])
        if not isinstance(current, list) or any(current.count(rule) < rules.count(rule) for rule in rules):
            raise InstallError("Codex hooks.json 的 Relay 自有规则已变化，拒绝覆盖")
    for event, rules in expected.items():
        current = hooks.get(event, [])
        for rule in rules:
            current.remove(rule)
        hooks[event] = current
        if not hooks[event]:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    added = {}
    for event, rules in replacement.items():
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise InstallError(f"Codex hooks.json {event} 必须是数组")
        for rule in rules:
            if rule in expected.get(event, []) or rule not in current:
                current.append(rule)
                added.setdefault(event, []).append(rule)
    if hooks:
        data["hooks"] = hooks
    if data:
        _atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(),
                      stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
    elif path.exists():
        if preserve_empty and existed:
            _atomic_write(path, b"{}\n", stat.S_IMODE(path.stat().st_mode))
        else:
            path.unlink()
    return added


def _verify_owned_programs(root, ledger):
    hashes = ledger.get("post_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise InstallError("当前安装账本缺少程序哈希")
    expected_paths = {p.relative_to(root).as_posix() for p in _installed_targets(root)}
    if set(hashes) != expected_paths or set(ledger.get("managed_programs", [])) != expected_paths:
        raise InstallError("安装账本程序清单不完整或包含未知路径")
    if set(ledger.get("originals", {})) != expected_paths:
        raise InstallError("安装账本原始快照清单不完整")
    for rel, digest in hashes.items():
        p = PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts:
            raise InstallError("受管程序路径无效")
        path = root.joinpath(*p.parts)
        _assert_no_symlink_components(root, [path])
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise InstallError(f"受管文件已被修改或缺失：{rel}")


def _preflight(root, runner, proc_root):
    if runner is subprocess.run and Path(root).expanduser().resolve() != Path.home().resolve():
        raise InstallError("真实 user systemd 操作的 --root 必须等于当前用户 HOME；非 HOME root 仅支持隔离 fake runner")
    _registry_preflight(root)
    service = _service_state(runner)
    processes = _relay_processes(Path(proc_root), root,
                                 managed_pid=service["main_pid"] if service["active"] else None)
    if processes:
        raise InstallError("已有 Relay CLI 正在执行：" + ", ".join(processes))
    return service


def _restore_service(runner, before):
    current = _service_state(runner)
    if current["active"]:
        _run_checked(runner, ["systemctl", "--user", "stop", SERVICE], "停止 Relay 服务")
    if before["installed"]:
        _run_checked(runner, ["systemctl", "--user", "daemon-reload"], "重载 user systemd")
        current = _service_state(runner)
        if current["enabled"] != before["enabled"]:
            args = ["systemctl", "--user", "enable" if before["enabled"] else "disable"]
            if before.get("unit_state") == "enabled-runtime" and before["enabled"]:
                args.append("--runtime")
            args.append(SERVICE)
            _run_checked(runner, args, "恢复 Relay enabled 状态")
        if before["active"]:
            _run_checked(runner, ["systemctl", "--user", "start", SERVICE], "恢复 Relay active 状态")
    else:
        if current["enabled"]:
            _run_checked(runner, ["systemctl", "--user", "disable", SERVICE], "禁用 Relay 服务")
        _run_checked(runner, ["systemctl", "--user", "daemon-reload"], "重载 user systemd")
    final = _service_state(runner)
    if final["active"] != before["active"] or final["enabled"] != before["enabled"]:
        raise InstallError("恢复后的 Relay active/enabled 状态与操作前不一致")


def _run_checked(runner, command, action):
    result = _run(runner, command)
    if result.returncode:
        raise InstallError(f"{action}失败：{result.stderr.strip()}")


def _drain(root, runner, proc_root, service, drain_timeout):
    if service["active"]:
        _run_checked(runner, ["systemctl", "--user", "stop", SERVICE], "停止 Relay 服务")
    maintenance = b'#!/usr/bin/env bash\necho "Relay maintenance in progress" >&2\nexit 75\n'
    for name in ENTRYPOINTS:
        path = root / ".local/bin" / name
        if path.is_file():
            _atomic_write(path, maintenance, stat.S_IMODE(path.stat().st_mode))
    deadline = time.monotonic() + drain_timeout
    while True:
        active = _relay_processes(Path(proc_root), root)
        if not active:
            break
        if time.monotonic() >= deadline:
            raise InstallError("Relay CLI 未能在维护窗口内退出：" + ", ".join(active))
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    _registry_preflight(root)
    late = _relay_processes(Path(proc_root), root)
    if late:
        raise InstallError("维护门禁后仍有 Relay 进程：" + ", ".join(late))


def _restore_files(files):
    errors = []
    for path, (data, mode) in files.items():
        try:
            if data is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, data, mode if mode is not None else 0o600)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise InstallError("恢复操作前文件失败：" + "; ".join(errors))


def _installed_targets(root):
    binary = root / ".local/bin"
    library = root / ".local/lib/astra-herdr-relay"
    targets = {library / "relay.py", library / "install.py", library / "task_contract.py",
               library / "doc_registry.py", library / "artifact.py", library / "evidence.py",
               library / "stages.py", library / "gates.py",
               library / "hooks/astra_relay_subagent_redirect.py"}
    targets.update(binary / name for name in ENTRYPOINTS)
    targets.add(root / ".config/systemd/user" / SERVICE)
    return sorted(targets)


def _verify_owned_snippets(root, ledger):
    hook_path = root / ".codex/hooks.json"
    expected_hooks = ledger.get("owned_hooks_after", {})
    if not isinstance(expected_hooks, dict):
        raise InstallError("安装账本 Hook 所有权记录无效")
    if expected_hooks:
        data = _load_json_file(hook_path, label="Codex hooks.json")
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            raise InstallError("Codex hooks.json 结构无效")
        for event, rules in expected_hooks.items():
            current = hooks.get(event)
            if (not isinstance(rules, list) or not isinstance(current, list) or
                    any(current.count(rule) < rules.count(rule) for rule in rules)):
                raise InstallError("Codex hooks.json 的 Relay 自有规则已变化，拒绝操作")
    agents_path = root / ".codex/AGENTS.md"
    _, current = _read_owned_agents(agents_path)
    if current != ledger.get("owned_agents_after"):
        raise InstallError("AGENTS.md 的 Relay 自有块已变化，拒绝操作")


def _read_transaction_files(root):
    paths = _installed_targets(root) + [root / ".codex/hooks.json", root / ".codex/AGENTS.md",
        root / ".config/astra-herdr-relay/relay.env", root / ".config/astra-herdr-relay/secret.env"]
    result = {}
    for path in paths:
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise InstallError(f"操作目标不是普通文件：{path.relative_to(root)}")
            result[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        else:
            result[path] = (None, None)
    return result


def _operation_check(root, runner, proc_root, *, require_install=True):
    root = _safe_root(Path(root))
    service = _preflight(root, runner, proc_root)
    current_id = _load_current(root)
    if require_install and not current_id:
        raise InstallError("没有可管理的 Relay 安装账本")
    ledger = _load_ledger(root, current_id) if current_id else None
    if ledger:
        _verify_owned_programs(root, ledger)
        _verify_owned_snippets(root, ledger)
    paths = _installed_targets(root) + [root / ".codex/hooks.json", root / ".codex/AGENTS.md",
        root / ".config/astra-herdr-relay/relay.env", root / ".config/astra-herdr-relay/secret.env"]
    _assert_no_symlink_components(root, paths)
    return root, service, current_id, ledger


def _apply_operation(root, runner, proc_root, service_before, snapshot, mutate, operation,
                     *, drain_timeout=5):
    try:
        _drain(root, runner, proc_root, service_before, drain_timeout)
        mutate()
    except Exception as exc:
        recovery_errors = []
        try:
            _restore_files(snapshot)
        except Exception as restore_exc:
            recovery_errors.append(str(restore_exc))
        try:
            _restore_service(runner, service_before)
        except Exception as restore_exc:
            recovery_errors.append(str(restore_exc))
        detail = "；恢复错误：" + "；".join(recovery_errors) if recovery_errors else "；已恢复操作前状态"
        label = {"uninstall": "卸载", "rollback": "回滚"}.get(operation, operation)
        error = InstallError(f"{label}失败：{exc}{detail}")
        raise error from exc


def uninstall(root, *, runner=subprocess.run, proc_root=Path("/proc"), check=False, drain_timeout=5):
    root = _safe_root(Path(root))
    try:
        if check:
            root, service, current_id, ledger = _operation_check(root, runner, proc_root)
            return {"mode": "check", "operation": "uninstall", "install_id": current_id,
                    "service": {k: service[k] for k in ("installed", "active", "enabled", "unit_state")}}
        with _operation_lock(root):
            root, service, current_id, ledger = _operation_check(root, runner, proc_root)
            snapshot = _read_transaction_files(root)
            _, _, _, pointer = _metadata_paths(root)
            pointer_before = pointer.read_bytes()

            def mutate():
                if ledger.get("parent_install_id") is None and not ledger.get("legacy_install", False):
                    restore_records = _restore_records(root, ledger)
                    for path, (data, mode) in restore_records.items():
                        if data is None:
                            path.unlink(missing_ok=True)
                        else:
                            _atomic_write(path, data, mode if mode is not None else 0o600)
                else:
                    if service["enabled"]:
                        _run_checked(runner, ["systemctl", "--user", "disable", SERVICE], "禁用 Relay 服务")
                    for path in _installed_targets(root):
                        path.unlink(missing_ok=True)
                _mutate_owned_hooks(root / ".codex/hooks.json", ledger.get("owned_hooks_after", {}), {},
                                    preserve_empty=ledger.get("hook_file_origin_exists", True))
                _replace_agents_block(root / ".codex/AGENTS.md", ledger.get("owned_agents_after"), None,
                                      preserve_empty=ledger.get("agents_file_origin_exists", True))
                if ledger.get("parent_install_id") is None and not ledger.get("legacy_install", False):
                    _restore_service(runner, ledger["service_before"])
                else:
                    _run_checked(runner, ["systemctl", "--user", "daemon-reload"], "重载 user systemd")
                final = _service_state(runner)
                if (ledger.get("parent_install_id") is not None or ledger.get("legacy_install", False)) and (final["installed"] or final["active"] or final["enabled"]):
                    raise InstallError("卸载后 Relay 服务仍处于安装或启用状态")
                pointer.unlink(missing_ok=True)

            try:
                _apply_operation(root, runner, proc_root, service, snapshot, mutate, "uninstall",
                                 drain_timeout=drain_timeout)
            except Exception:
                _atomic_write(pointer, pointer_before, 0o600)
                raise
            return {"mode": "uninstalled", "install_id": current_id,
                    "retained": ["config", "state", "install-backups"]}
    except Exception as exc:
        if not check:
            _record_error(root, "uninstall", exc)
        raise


def list_backups(root):
    root = _safe_root(Path(root))
    _, ledgers, _, _ = _metadata_paths(root)
    if ledgers.is_symlink():
        raise InstallError("安装账本目录是符号链接")
    if not ledgers.exists():
        return {"mode": "backups", "backups": []}
    if not ledgers.is_dir():
        raise InstallError("安装账本路径不是目录")
    current = _load_current(root)
    result = []
    for path in sorted(ledgers.iterdir()):
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
            raise InstallError("安装账本目录包含未知条目")
        ledger = _load_ledger(root, path.stem)
        result.append({"backup_id": path.stem, "version": ledger.get("version"),
                       "parent_install_id": ledger.get("parent_install_id"),
                       "is_current": path.stem == current})
    return {"mode": "backups", "backups": result}


def rollback(root, backup_id, *, runner=subprocess.run, proc_root=Path("/proc"), check=False,
             drain_timeout=5):
    root = _safe_root(Path(root))
    try:
        if check:
            root, service, current_id, current = _operation_check(root, runner, proc_root)
            if backup_id != current_id:
                raise InstallError("备份不属于当前版本链；只能选择当前安装账本对应的回滚点")
            records = _restore_records(root, current)
            return {"mode": "check", "operation": "rollback", "backup_id": backup_id,
                    "restore_files": len(records)}
        with _operation_lock(root):
            root, service, current_id, current = _operation_check(root, runner, proc_root)
            if backup_id != current_id:
                raise InstallError("备份不属于当前版本链；只能选择当前安装账本对应的回滚点")
            restore_records = _restore_records(root, current)
            parent_id = current.get("parent_install_id")
            parent = _load_ledger(root, parent_id) if parent_id else None
            if parent:
                expected = parent.get("post_sha256", {})
                for rel, digest in expected.items():
                    record = current["originals"].get(rel)
                    if not isinstance(record, dict) or not record.get("existed") or record.get("sha256") != digest:
                        raise InstallError("版本链快照与父安装哈希不匹配，拒绝回滚")
                rollback_hooks_before = parent.get("owned_hooks_after", {})
            elif (current.get("legacy_install") and
                  current.get("originals", {}).get(".local/lib/astra-herdr-relay/relay.py", {}).get("existed") is True):
                rollback_hooks_before = current.get("rollback_hooks_before",
                                                     current.get("owned_hooks_before", {}))
            else:
                rollback_hooks_before = {}
            if (not isinstance(rollback_hooks_before, dict) or
                    any(not isinstance(event, str) or not isinstance(rules, list)
                        for event, rules in rollback_hooks_before.items())):
                raise InstallError("安装账本可恢复 Hook 记录无效")
            snapshot = _read_transaction_files(root)
            _, _, _, pointer = _metadata_paths(root)
            pointer_before = pointer.read_bytes()

            def mutate():
                for path, (data, mode) in restore_records.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, data, mode if mode is not None else 0o600)
                _mutate_owned_hooks(root / ".codex/hooks.json", current.get("owned_hooks_after", {}),
                                   rollback_hooks_before,
                                   preserve_empty=current.get("hook_file_before_exists", True))
                _replace_agents_block(root / ".codex/AGENTS.md", current.get("owned_agents_after"),
                                      current.get("owned_agents_before"),
                                      preserve_empty=current.get("agents_file_before_exists", True))
                _restore_service(runner, current["service_before"])
                if parent_id:
                    _write_json(pointer, {"install_id": parent_id})
                else:
                    pointer.unlink(missing_ok=True)

            try:
                _apply_operation(root, runner, proc_root, service, snapshot, mutate, "rollback",
                                 drain_timeout=drain_timeout)
            except Exception:
                _atomic_write(pointer, pointer_before, 0o600)
                raise
            return {"mode": "rolled-back", "backup_id": backup_id,
                    "restored_install_id": parent_id, "service": current["service_before"]}
    except Exception as exc:
        if not check:
            _record_error(root, "rollback", exc)
        raise


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


def _install(source, root, *, runner=subprocess.run, proc_root=Path("/proc"), check=False,
            drain_timeout=5):
    source = Path(source)
    root = _safe_root(Path(root))
    entries = _check_source(source)
    service = _preflight(root, runner, proc_root)
    binary = root / ".local/bin"
    library = root / ".local/lib/astra-herdr-relay"
    config = root / ".config/astra-herdr-relay"
    state = root / ".local/state/astra-herdr-relay"
    user_systemd = root / ".config/systemd/user"
    codex = root / ".codex"
    unit = user_systemd / SERVICE
    hook_config = codex / "hooks.json"
    agents = codex / "AGENTS.md"
    state_dir, ledger_dir, backup_dir, current_pointer = _metadata_paths(root)
    current_id = _load_current(root)
    current_ledger = _load_ledger(root, current_id) if current_id else None
    if current_ledger:
        _verify_owned_programs(root, current_ledger)
        _verify_owned_snippets(root, current_ledger)
    legacy_install = bool(not current_ledger and service["installed"] and unit.is_file())
    targets = {library / "relay.py": source / "lib/relay.py",
               library / "install.py": source / "lib/install.py",
               library / "task_contract.py": source / "lib/task_contract.py",
               library / "doc_registry.py": source / "lib/doc_registry.py",
               library / "artifact.py": source / "lib/artifact.py",
               library / "evidence.py": source / "lib/evidence.py",
               library / "stages.py": source / "lib/stages.py",
               library / "gates.py": source / "lib/gates.py",
               library / "hooks/astra_relay_subagent_redirect.py": source / "hooks/astra_relay_subagent_redirect.py"}
    for name in ENTRYPOINTS:
        targets[binary / name] = source / "bin" / name
    targets[unit] = source / "systemd/astra-herdr-relay.service"
    _assert_no_symlink_components(root, [*targets, hook_config, agents,
        config / "relay.env", config / "secret.env", state / "agents.json",
        state / "install-backups", binary, library, library / "hooks", config,
        state, state / "tasks", state / "semantic", state / "reviews", state / "locks",
        state_dir / "install-ledgers", state_dir / "install.lock", current_pointer,
        user_systemd, codex])
    for path in [*targets, hook_config, agents, config / "relay.env", config / "secret.env"]:
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise InstallError(f"安装目标不是普通文件：{path.relative_to(root)}")
    if check:
        return {"mode": "check", "service": service, "active_cli": [],
                "required_files": list(REQUIRED_FILES), "manifest_entries": len(entries)}
    originals = {}
    original_modes = {}
    backup_id = uuid.uuid4().hex
    _private_dir(backup_dir)
    backup = backup_dir / backup_id
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(backup, 0o700)
    # Keep a private program/config backup. The live registry, task, lock and event
    # files are not copied back on rollback because they may advance during service restart.
    mutable = list(targets) + [hook_config, agents, config / "relay.env", config / "secret.env"]
    for path in mutable:
        originals[path] = path.read_bytes() if path.is_file() else None
        original_modes[path] = stat.S_IMODE(path.stat().st_mode) if originals[path] is not None else None
        if originals[path] is not None and path not in {config / "relay.env", config / "secret.env"}:
            rel = path.relative_to(root)
            saved = backup / rel
            _private_dir(saved.parent)
            _atomic_write(saved, originals[path], 0o600)

    fragment = json.loads((source / "hooks/hooks.fragment.json").read_text(encoding="utf-8"))
    desired_hooks = fragment["hooks"]
    if current_ledger:
        owned_hooks_before = current_ledger.get("owned_hooks_after", {})
        owned_agents_before = current_ledger.get("owned_agents_after")
    else:
        owned_hooks_before = {}
        if hook_config.exists():
            old_data = json.loads(hook_config.read_text(encoding="utf-8"))
            if not isinstance(old_data, dict) or not isinstance(old_data.get("hooks", {}), dict):
                raise InstallError("Codex hooks.json 结构无效")
            for event, rules in desired_hooks.items():
                current_rules = old_data.get("hooks", {}).get(event, [])
                if not isinstance(current_rules, list):
                    raise InstallError(f"Codex hooks.json {event} 必须是数组")
                matches = [rule for rule in rules if rule in current_rules]
                if matches:
                    owned_hooks_before[event] = matches
        _, owned_agents_before = _read_owned_agents(agents)
    if not isinstance(owned_hooks_before, dict) or any(not isinstance(v, list) for v in owned_hooks_before.values()):
        raise InstallError("账本中的 Hook 所有权记录无效")
    if (current_ledger or
            (legacy_install and originals.get(library / "relay.py") is not None)):
        rollback_hooks_before = owned_hooks_before
    else:
        rollback_hooks_before = {}
    agents_text = (source / "AGENTS.relay.block.md").read_text(encoding="utf-8").strip()
    _, owned_agents_after = _read_owned_agents_text(agents_text)

    try:
        _drain(root, runner, proc_root, service, drain_timeout)

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
        owned_hooks_after = _mutate_owned_hooks(hook_config, owned_hooks_before, desired_hooks)
        _replace_agents_block(agents, owned_agents_before, owned_agents_after)

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
        post_hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in targets}
        ledger = {
            "schema_version": 1, "install_id": backup_id, "backup_id": backup_id,
            "version": VERSION, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parent_install_id": current_id,
            "legacy_install": legacy_install,
            "managed_programs": sorted(post_hashes), "post_sha256": post_hashes,
            "originals": {path.relative_to(root).as_posix(): {
                "existed": data is not None,
                "sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
                "mode": original_modes[path],
                "backup_path": (backup / path.relative_to(root)).relative_to(root).as_posix() if data is not None else None,
            } for path, data in originals.items() if path in targets},
            "service_before": {k: service[k] for k in ("installed", "active", "enabled", "unit_state")},
            "owned_hooks_before": owned_hooks_before, "owned_hooks_after": owned_hooks_after,
            "rollback_hooks_before": rollback_hooks_before,
            "owned_agents_before": owned_agents_before, "owned_agents_after": owned_agents_after,
            "hook_file_before_exists": originals[hook_config] is not None,
            "agents_file_before_exists": originals[agents] is not None,
            "hook_file_origin_exists": (current_ledger.get("hook_file_origin_exists", current_ledger.get("hook_file_before_exists"))
                                         if current_ledger else originals[hook_config] is not None),
            "agents_file_origin_exists": (current_ledger.get("agents_file_origin_exists", current_ledger.get("agents_file_before_exists"))
                                           if current_ledger else originals[agents] is not None),
        }
        _private_dir(ledger_dir)
        _write_json(ledger_dir / (backup_id + ".json"), ledger)
        _write_json(current_pointer, {"install_id": backup_id})
        return {"mode": "installed", "backup_id": backup_id,
                "service_before": ledger["service_before"], "files": post_hashes}
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
            (ledger_dir / (backup_id + ".json")).unlink(missing_ok=True)
            raise
        detail = "; 回滚错误：" + "; ".join(rollback_errors) if rollback_errors else ""
        prefix = str(exc) if isinstance(exc, InstallError) else f"增量安装失败：{exc}"
        (ledger_dir / (backup_id + ".json")).unlink(missing_ok=True)
        raise InstallError(f"{prefix}；已尝试回滚{detail}") from exc


def _read_owned_agents_text(text):
    if text.count(OWNED_START) != 1 or text.count(OWNED_END) != 1:
        raise InstallError("发行包 AGENTS Relay 标记不完整")
    start = text.index(OWNED_START)
    end = text.index(OWNED_END) + len(OWNED_END)
    return text, text[start:end]


def install(source, root, *, runner=subprocess.run, proc_root=Path("/proc"), check=False,
            drain_timeout=5):
    root = _safe_root(Path(root))
    try:
        if check:
            return _install(source, root, runner=runner, proc_root=proc_root, check=True,
                            drain_timeout=drain_timeout)
        with _operation_lock(root):
            return _install(source, root, runner=runner, proc_root=proc_root, check=False,
                            drain_timeout=drain_timeout)
    except Exception as exc:
        _record_error(root, "install", exc)
        raise


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
    parser = argparse.ArgumentParser(description="Relay 安装、卸载与指定备份回滚")
    parser.add_argument("operation", nargs="?", choices=("install", "uninstall", "rollback", "list-backups"),
                        default="install")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--root", type=Path, default=Path.home())
    parser.add_argument("--check", action="store_true", help="只读预检，不进行变更")
    parser.add_argument("--backup-id", help="rollback 必须显式指定 list-backups 给出的 ID")
    args = parser.parse_args(argv)
    try:
        if args.operation == "install":
            result = install(args.source, args.root, check=args.check)
        elif args.operation == "uninstall":
            result = uninstall(args.root, check=args.check)
        elif args.operation == "rollback":
            if not args.backup_id:
                parser.error("rollback 必须指定 --backup-id")
            result = rollback(args.root, args.backup_id, check=args.check)
        else:
            result = list_backups(args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
