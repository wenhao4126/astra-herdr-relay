from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from task_contract import canonical_digest, canonical_json, validate_task


SHA256 = re.compile(r"[a-f0-9]{64}\Z")
AUTHOR_KEYS = {"producer_id", "producer_role", "session_id", "tab_id", "pane_id"}
OPTIONAL_AUTHOR_KEYS = {"agent_session_value"}


class ArtifactError(ValueError):
    code = "ARTIFACT_INVALID"

    def __init__(self, message: str, code: str = "ARTIFACT_INVALID"):
        super().__init__(message)
        self.code = code


def _fail(message: str, code: str = "ARTIFACT_INVALID"):
    raise ArtifactError(message, code)


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value):
    if type(value) is not str:
        _fail("created_at 必须是带时区的 ISO-8601 字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactError("created_at 日期格式无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("created_at 必须包含时区")


def _state_dir(state_dir, *, create):
    state = Path(state_dir)
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        _fail("Relay state 必须是普通目录")
    directory = state / "artifacts"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        _fail("制品快照目录必须是普通目录")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    if directory.exists() and stat.S_IMODE(directory.stat().st_mode) & 0o077:
        _fail("制品快照目录权限过宽")
    return directory


def load_files_file(filename):
    path = Path(filename)
    if path.is_symlink() or not path.is_file():
        _fail("files-file 必须是普通 JSON 文件")
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("JSON 对象键重复")
                result[key] = value
            return result
        files = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("JSON 常量无效")))
    except Exception as exc:
        raise ArtifactError("files-file JSON 无法安全解析") from exc
    if type(files) is not list or not files or any(type(item) is not str or not item for item in files):
        _fail("files-file 顶层必须是非空相对路径字符串数组")
    return files


def _safe_relative(value):
    if type(value) is not str or not value or "\\" in value:
        _fail("交付文件路径必须是规范相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != value:
        _fail("交付文件路径越界或非规范")
    return path


def _file_digest(root: Path, relative: str):
    path = _safe_relative(relative)
    current = root
    for index, part in enumerate(path.parts):
        current = current / part
        if current.is_symlink():
            _fail("交付文件路径包含符号链接", "ARTIFACT_SYMLINK")
        try:
            info = current.lstat()
        except OSError as exc:
            raise ArtifactError(f"交付文件缺失：{relative}", "ARTIFACT_FILE_MISSING") from exc
        if index < len(path.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            _fail("交付文件父路径不是目录")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
        fd = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail("交付制品必须是独立普通文件")
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb", closefd=False) as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"交付文件路径无法安全读取：{relative}", "ARTIFACT_PATH_INVALID") from exc


def _author(value):
    if type(value) is not dict or not AUTHOR_KEYS.issubset(value) or set(value) - AUTHOR_KEYS - OPTIONAL_AUTHOR_KEYS:
        _fail("作者实例身份字段不完整")
    for key in AUTHOR_KEYS | (set(value) & OPTIONAL_AUTHOR_KEYS):
        if type(value[key]) is not str or not value[key].strip():
            _fail(f"作者实例 {key} 必须是非空字符串")
    return {key: value[key] for key in sorted(value)}


def _aggregate(files):
    return hashlib.sha256(canonical_json(files).encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes):
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def capture_artifact(task, files, state_dir, author_instance, *, created_at=None):
    task = validate_task(task)
    digest = canonical_digest(task)
    if type(files) is not list or not files:
        _fail("交付清单必须是非空路径数组")
    normalized = [_safe_relative(item).as_posix() for item in files]
    if len(normalized) != len(set(normalized)):
        _fail("交付清单不能重复列出路径")
    root = Path(task["working_directory"])
    entries = [{"path": path, "sha256": _file_digest(root, path)}
               for path in sorted(normalized)]
    author = _author(author_instance)
    created_at = _iso_now() if created_at is None else created_at
    _timestamp(created_at)
    record = {"schema_version": 1, "task_sha256": digest,
              "artifact_sha256": _aggregate(entries), "files": entries,
              "author_instance": author, "created_at": created_at,
              "working_directory": task["working_directory"],
              "delivery_scope": "only_declared_files_not_repository_correctness"}
    directory = _state_dir(state_dir, create=True)
    path = directory / f"{digest}.json"
    lock_path = directory / f"{digest}.lock"
    if lock_path.is_symlink():
        _fail("制品快照锁不能是符号链接")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise ArtifactError("无法安全打开制品快照锁") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("制品快照锁必须是独立普通文件")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _atomic_write(path, (canonical_json(record) + "\n").encode("utf-8"))
            return record
    finally:
        os.close(fd)


def verify_snapshot_digest(digest, state_dir, *, working_directory=None):
    if type(digest) is not str or SHA256.fullmatch(digest) is None:
        _fail("任务摘要格式无效", "ARTIFACT_RECORD_INVALID")
    directory = _state_dir(state_dir, create=False)
    path = directory / f"{digest}.json"
    if path.is_symlink() or not path.is_file():
        _fail("当前任务缺少制品快照", "ARTIFACT_SNAPSHOT_MISSING")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ArtifactError("制品快照记录无效", "ARTIFACT_RECORD_INVALID") from exc
    if (type(record) is not dict or record.get("schema_version") != 1 or
            record.get("task_sha256") != digest or type(record.get("files")) is not list or
            not record.get("files") or type(record.get("author_instance")) is not dict or
            type(record.get("working_directory")) is not str):
        _fail("制品快照记录字段无效", "ARTIFACT_RECORD_INVALID")
    try:
        root = Path(record["working_directory"])
        if (not root.is_absolute() or root.is_symlink() or not root.is_dir() or
                str(root.resolve(strict=True)) != record["working_directory"]):
            _fail("快照工作目录不是规范真实目录", "ARTIFACT_RECORD_INVALID")
        if working_directory is not None and working_directory != record["working_directory"]:
            _fail("快照不属于当前任务工作目录", "ARTIFACT_STALE")
        expected = []
        for item in record["files"]:
            if (type(item) is not dict or set(item) != {"path", "sha256"} or
                    type(item["sha256"]) is not str or SHA256.fullmatch(item["sha256"]) is None):
                _fail("制品快照文件条目无效", "ARTIFACT_RECORD_INVALID")
            expected.append({"path": item["path"], "sha256": _file_digest(root, item["path"])})
        if (expected != record["files"] or type(record.get("artifact_sha256")) is not str or
                SHA256.fullmatch(record["artifact_sha256"]) is None or
                _aggregate(expected) != record.get("artifact_sha256")):
            _fail("制品内容变化，旧证据已失效", "ARTIFACT_STALE")
        _author(record["author_instance"])
        _timestamp(record.get("created_at"))
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError("制品快照记录无法验证", "ARTIFACT_RECORD_INVALID") from exc
    return record


def verify_current_snapshot(task, state_dir):
    task = validate_task(task)
    digest = canonical_digest(task)
    return verify_snapshot_digest(digest, state_dir,
                                  working_directory=task["working_directory"])
