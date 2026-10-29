from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path

from artifact import ArtifactError, _file_digest, verify_snapshot_digest
from task_contract import canonical_json


FIELDS = {
    "id", "task_sha256", "artifact_sha256", "kind", "producer_id", "producer_role",
    "session_id", "status", "report_path", "report_sha256", "created_at",
}
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
KINDS = {"tests", "build", "lint", "review", "ocr_review"}
STATUSES = {"tests": {"passed", "completed", "failed", "unknown"},
            "build": {"passed", "completed", "failed", "unknown"},
            "lint": {"passed", "completed", "failed", "unknown"},
            "review": {"completed", "failed", "unknown"},
            "ocr_review": {"completed", "failed", "unknown"}}


class EvidenceError(ValueError):
    code = "EVIDENCE_INVALID"

    def __init__(self, message: str, code: str = "EVIDENCE_INVALID"):
        super().__init__(message)
        self.code = code


def _fail(message: str, code: str = "EVIDENCE_INVALID"):
    raise EvidenceError(message, code)


def _timestamp(value):
    if type(value) is not str:
        _fail("created_at 必须是带时区的 ISO-8601 字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("created_at 日期格式无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("created_at 必须包含时区")


def _validate(value):
    if type(value) is not dict or set(value) != FIELDS:
        _fail("证据记录字段缺失或包含未知字段")
    for key in FIELDS:
        if type(value[key]) is not str:
            _fail(f"{key} 必须是字符串")
    if ID_PATTERN.fullmatch(value["id"]) is None:
        _fail("证据 ID 格式无效")
    for key in ("task_sha256", "artifact_sha256", "report_sha256"):
        if SHA256.fullmatch(value[key]) is None:
            _fail(f"{key} 必须是小写 SHA-256")
    if value["kind"] not in KINDS:
        _fail("证据 kind 无效")
    if value["status"] not in STATUSES[value["kind"]]:
        _fail(f"{value['kind']} 的 status 无效")
    for key in ("producer_id", "producer_role", "session_id", "report_path"):
        if not value[key].strip():
            _fail(f"{key} 不能为空")
    if value["kind"] == "review" and value["producer_role"] != "reviewer":
        _fail("review 证据的 producer_role 必须是 reviewer")
    if value["kind"] == "ocr_review" and value["producer_role"] != "ocr":
        _fail("ocr_review 证据的 producer_role 必须是 ocr")
    _timestamp(value["created_at"])
    return {key: value[key] for key in FIELDS}


def _records_dir(state_dir, *, create):
    state = Path(state_dir)
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        _fail("Relay state 必须是普通目录")
    directory = state / "evidence_records"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        _fail("证据目录必须是普通目录")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    if directory.exists() and stat.S_IMODE(directory.stat().st_mode) & 0o077:
        _fail("证据目录权限过宽")
    return directory


def _load_snapshot_for_record(record, state_dir):
    try:
        snapshot = verify_snapshot_digest(record["task_sha256"], state_dir)
    except ArtifactError as exc:
        raise EvidenceError("证据没有对应的当前制品快照", "EVIDENCE_SNAPSHOT_INVALID") from exc
    if snapshot["artifact_sha256"] != record["artifact_sha256"]:
        _fail("证据绑定的制品摘要不是当前快照", "EVIDENCE_SNAPSHOT_INVALID")
    return snapshot


def _check_report(record, state_dir, snapshot):
    root = Path(snapshot["working_directory"])
    try:
        digest = _file_digest(root, record["report_path"])
    except ArtifactError as exc:
        raise EvidenceError("证据报告不存在或路径不安全", "EVIDENCE_REPORT_INVALID") from exc
    if digest != record["report_sha256"]:
        _fail("证据报告摘要不匹配，旧证据失效", "EVIDENCE_REPORT_HASH_MISMATCH")
    if record["kind"] == "ocr_review":
        try:
            report = _strict_read(root / record["report_path"])
        except Exception as exc:
            raise EvidenceError("OCR 报告必须是合法 JSON", "OCR_REPORT_INVALID") from exc
        if (type(report) is not dict or report.get("status") != "COMPLETE" or
                type(report.get("findings")) is not list):
            _fail("OCR 报告必须是完整 ReviewResult，不能用 partial/error 冒充完成",
                  "OCR_REPORT_INCOMPLETE")


def _check_independent_review(record, snapshot):
    if record["kind"] != "review":
        return
    author = snapshot["author_instance"]
    if (record["producer_id"] == author["producer_id"] or
            record["session_id"] == author["session_id"]):
        _fail("review 必须由不同作者身份和独立 session 产生", "EVIDENCE_REVIEW_NOT_INDEPENDENT")


def _strict_read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON 对象键重复")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("JSON 常量无效")))


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


def record_evidence(value, state_dir):
    record = _validate(value)
    snapshot = _load_snapshot_for_record(record, state_dir)
    _check_report(record, state_dir, snapshot)
    _check_independent_review(record, snapshot)
    directory = _records_dir(state_dir, create=True)
    path = directory / f"{record['id']}.json"
    lock_path = directory / f"{record['id']}.lock"
    if lock_path.is_symlink():
        _fail("证据锁不能是符号链接")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise EvidenceError("无法安全打开证据锁") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("证据锁必须是独立普通文件")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists() or path.is_symlink():
                _fail("证据 ID 已存在，拒绝覆盖", "EVIDENCE_ID_CONFLICT")
            _atomic_write(path, (canonical_json(record) + "\n").encode("utf-8"))
            return record
    finally:
        os.close(fd)


def load_evidence_records(task_sha256, artifact_sha256, state_dir):
    if SHA256.fullmatch(task_sha256) is None or SHA256.fullmatch(artifact_sha256) is None:
        _fail("阶段摘要格式无效")
    directory = _records_dir(state_dir, create=False)
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            if path.name.endswith(".lock") and not path.is_symlink() and path.is_file():
                continue
            _fail("证据目录包含未知条目", "EVIDENCE_RECORD_INVALID")
        if path.is_symlink() or not path.is_file():
            _fail("证据记录不是普通文件", "EVIDENCE_RECORD_INVALID")
        try:
            raw = _strict_read(path)
        except Exception as exc:
            raise EvidenceError("证据记录无法安全解析", "EVIDENCE_RECORD_INVALID") from exc
        if (type(raw) is not dict or raw.get("task_sha256") != task_sha256 or
                raw.get("artifact_sha256") != artifact_sha256):
            continue
        record = _validate(raw)
        if path.name != f"{record['id']}.json":
            _fail("证据记录 ID 与文件名不匹配", "EVIDENCE_RECORD_INVALID")
        snapshot = _load_snapshot_for_record(record, state_dir)
        _check_report(record, state_dir, snapshot)
        _check_independent_review(record, snapshot)
        records.append(record)
    return records


def read_report_excerpt(record, state_dir, *, limit=12000):
    record = _validate(record)
    snapshot = _load_snapshot_for_record(record, state_dir)
    _check_report(record, state_dir, snapshot)
    path = Path(snapshot["working_directory"]) / record["report_path"]
    try:
        with path.open("r", encoding="utf-8") as stream:
            content = stream.read(limit + 1)
    except (OSError, UnicodeError) as exc:
        raise EvidenceError("证据报告不是可读 UTF-8 文本", "EVIDENCE_REPORT_INVALID") from exc
    if len(content) > limit:
        return content[:limit] + "\n[报告摘录已截断]"
    return content
