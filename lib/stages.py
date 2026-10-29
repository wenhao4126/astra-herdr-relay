from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import artifact
import doc_registry
import evidence
from task_contract import TaskContractError, canonical_digest, canonical_json, docs_digest, validate_task


ROUTES = {"PASS", "ACCEPT_WITH_NOTES", "REWORK", "DEEP_REVIEW", "BLOCKED"}
DECISION_KEYS = {"schema_version", "task_sha256", "artifact_sha256", "evidence_sha256",
                 "status", "route", "error_code", "created_at"}
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class StageError(ValueError):
    code = "STAGE_INVALID"

    def __init__(self, message: str, code: str = "STAGE_INVALID"):
        super().__init__(message)
        self.code = code


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _error(exc):
    return {"code": getattr(exc, "code", "STAGE_INVALID"), "message": str(exc)}


def _private_dir(state_dir, name, *, create=False):
    state = Path(state_dir)
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        raise StageError("Relay state 必须是普通目录", "STAGE_STATE_INVALID")
    directory = state / name
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise StageError("阶段状态路径必须是普通目录", "STAGE_STATE_INVALID")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    if directory.exists() and stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise StageError("阶段状态目录权限过宽", "STAGE_STATE_INVALID")
    return directory


def _evidence_digest(records):
    included = [{key: record[key] for key in (
        "id", "task_sha256", "artifact_sha256", "kind", "producer_id", "producer_role",
        "session_id", "status", "report_path", "report_sha256", "created_at")}
        for record in sorted(records, key=lambda item: item["id"])]
    return hashlib.sha256(canonical_json(included).encode("utf-8")).hexdigest()


def _decision_path(task_sha256, artifact_sha256, evidence_sha256, state_dir, *, create=False):
    identity = {"task_sha256": task_sha256, "artifact_sha256": artifact_sha256,
                "evidence_sha256": evidence_sha256}
    key = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    directory = _private_dir(state_dir, "final_jev", create=create)
    return directory, directory / f"{key}.json", directory / f"{key}.lock"


def _read_decision(task_sha256, artifact_sha256, evidence_sha256, state_dir):
    directory, path, _ = _decision_path(task_sha256, artifact_sha256, evidence_sha256, state_dir)
    if not directory.exists():
        return None
    if path.is_symlink():
        raise StageError("Jev 最终判断记录不能是符号链接", "JEV_RECORD_INVALID")
    if not path.exists():
        return None
    if not path.is_file():
        raise StageError("Jev 最终判断记录不是普通文件", "JEV_RECORD_INVALID")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageError("Jev 最终判断记录无法解析", "JEV_RECORD_INVALID") from exc
    if (type(record) is not dict or set(record) != DECISION_KEYS or
            record.get("schema_version") != 1 or
            record.get("task_sha256") != task_sha256 or
            record.get("artifact_sha256") != artifact_sha256 or
            record.get("evidence_sha256") != evidence_sha256 or
            record.get("status") not in {"evaluated", "failed"} or
            record.get("error_code") not in {"", "JEV_RESPONSE_INVALID", "JEV_EVALUATION_FAILED"} or
            record.get("route") not in ROUTES | {"UNKNOWN"}):
        raise StageError("Jev 最终判断摘要或字段无效", "JEV_RECORD_INVALID")
    if ((record["status"] == "evaluated" and
         (record["route"] not in ROUTES or record["error_code"] != "")) or
            (record["status"] == "failed" and
             (record["route"] != "UNKNOWN" or record["error_code"] not in {
                 "JEV_RESPONSE_INVALID", "JEV_EVALUATION_FAILED"}))):
        raise StageError("Jev 最终判断状态无效", "JEV_RECORD_INVALID")
    try:
        timestamp = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StageError("Jev 最终判断时间无效", "JEV_RECORD_INVALID") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StageError("Jev 最终判断时间缺少时区", "JEV_RECORD_INVALID")
    return record


def _collect(task, stage, state_dir, *, approved=False):
    errors = []
    gaps = []
    try:
        task = validate_task(task)
    except TaskContractError as exc:
        return {"valid": False, "ready_for_next_stage": False,
                "errors": [_error(exc)], "gaps": [], "task_sha256": None,
                "artifact_sha256": None, "evidence_sha256": None, "docs_sha256": None}
    task_sha = canonical_digest(task)
    docs_sha = docs_digest(task)
    result = {"stage": stage, "task_sha256": task_sha, "artifact_sha256": None,
              "evidence_sha256": None, "docs_sha256": docs_sha,
              "valid": False, "ready_for_next_stage": False,
              "errors": errors, "gaps": gaps}
    if stage not in {"dispatch", "review", "final"}:
        errors.append({"code": "STAGE_NAME_INVALID", "message": "stage 必须是 dispatch、review 或 final"})
        return result
    if stage != "dispatch" and task["schema_version"] != 2:
        errors.append({"code": "SCHEMA_V1_STAGE_UNSUPPORTED",
                       "message": "schema_version=1 可做旧式校验，不能通过新协议 review/final 阶段"})
        return result
    try:
        doc_registry.load_required_docs(task, state_dir)
    except doc_registry.DocRecordError as exc:
        errors.append(_error(exc))
        gaps.append("required_docs")
    if stage == "dispatch":
        if type(approved) is not bool or not approved:
            errors.append({"code": "TASK_GATE_REQUIRED", "message": "缺少当前任务的真实 Jev 批准"})
            gaps.append("task_jev_approval")
        result["valid"] = not errors
        result["ready_for_next_stage"] = not errors
        return result
    try:
        snapshot = artifact.verify_current_snapshot(task, state_dir)
        result["artifact_sha256"] = snapshot["artifact_sha256"]
        records = evidence.load_evidence_records(task_sha, snapshot["artifact_sha256"], state_dir)
        result["evidence_sha256"] = _evidence_digest(records)
    except artifact.ArtifactError as exc:
        errors.append(_error(exc))
        gaps.append("current_artifact_snapshot")
        result["valid"] = False
        return result
    except evidence.EvidenceError as exc:
        errors.append(_error(exc))
        gaps.append("current_evidence")
        result["valid"] = False
        return result
    if stage == "review":
        required = list(task["evidence_policy"]["review"])
    else:
        required = list(task["evidence_policy"]["final"])
    for kind in required:
        matching = [item for item in records if item["kind"] == kind]
        accepted_status = "passed" if kind in {"tests", "build", "lint"} else "completed"
        if not any(item["status"] == accepted_status for item in matching):
            gaps.append(kind)
            errors.append({"code": "STAGE_EVIDENCE_REQUIRED",
                           "message": f"缺少当前快照的 {kind} {accepted_status} 证据"})
    result["valid"] = not errors
    if stage == "review":
        result["ready_for_next_stage"] = result["valid"]
        return result
    if errors:
        return result
    decision = _read_decision(task_sha, snapshot["artifact_sha256"],
                              result["evidence_sha256"], state_dir)
    if decision is None:
        errors.append({"code": "JEV_DECISION_REQUIRED",
                       "message": "缺少绑定当前任务、制品和证据摘要的 Relay Jev 判断"})
        gaps.append("jev_decision")
        result["valid"] = False
        return result
    result["decision"] = decision["route"]
    result["decision_status"] = decision["status"]
    if decision["status"] != "evaluated":
        errors.append({"code": "JEV_EVALUATION_FAILED", "message": "当前 Jev 判断失败，不得放行"})
        result["valid"] = False
        return result
    result["valid"] = True
    result["ready_for_next_stage"] = decision["route"] in {"PASS", "ACCEPT_WITH_NOTES"}
    if not result["ready_for_next_stage"]:
        gaps.append("jev_route_" + str(decision["route"]))
    else:
        result["next_stage"] = "principal_decision"
    return result


def validate_stage(task, stage, state_dir, *, approved=False):
    return _collect(task, stage, state_dir, approved=approved)


def evaluate_final(task, state_dir, jev_evaluator):
    collected = _collect(task, "final", state_dir)
    errors = [item for item in collected["errors"] if item["code"] != "JEV_DECISION_REQUIRED"]
    if errors:
        result = {"valid": False, "ready_for_next_stage": False,
                  "errors": errors, "gaps": collected["gaps"],
                  "task_sha256": collected.get("task_sha256"),
                  "artifact_sha256": collected.get("artifact_sha256"),
                  "evidence_sha256": collected.get("evidence_sha256")}
        if ([item["code"] for item in errors] == ["JEV_EVALUATION_FAILED"] and
                collected.get("decision_status") == "failed"):
            result.update(route="UNKNOWN", cached=True)
        return result
    task_sha = collected["task_sha256"]
    artifact_sha = collected["artifact_sha256"]
    evidence_sha = collected["evidence_sha256"]
    directory, path, lock_path = _decision_path(task_sha, artifact_sha, evidence_sha,
                                                state_dir, create=True)
    if lock_path.is_symlink():
        return {"valid": False, "ready_for_next_stage": False,
                "errors": [{"code": "JEV_RECORD_INVALID", "message": "Jev 判断锁不能是符号链接"}],
                "gaps": ["jev_decision"]}
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return {"valid": False, "ready_for_next_stage": False,
                "errors": [{"code": "JEV_RECORD_INVALID", "message": "无法安全打开 Jev 判断锁"}],
                "gaps": ["jev_decision"]}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StageError("Jev 判断锁必须是独立普通文件", "JEV_RECORD_INVALID")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = _read_decision(task_sha, artifact_sha, evidence_sha, state_dir)
            cached = record is not None
            if record is None:
                try:
                    snapshot = artifact.verify_current_snapshot(task, state_dir)
                    records = evidence.load_evidence_records(task_sha, artifact_sha, state_dir)
                    documents = doc_registry.load_required_docs(task, state_dir)
                    reports = {item["id"]: evidence.read_report_excerpt(item, state_dir)
                               for item in records}
                    context = {"task_package": task, "task_sha256": task_sha,
                               "artifact_sha256": artifact_sha, "evidence_sha256": evidence_sha,
                               "artifact": {"files": snapshot["files"],
                                            "author_instance": snapshot["author_instance"],
                                            "delivery_scope": snapshot["delivery_scope"]},
                               "documents": documents, "evidence_records": records,
                               "report_excerpts": reports}
                except (artifact.ArtifactError, evidence.EvidenceError,
                        doc_registry.DocRecordError) as exc:
                    return {"valid": False, "ready_for_next_stage": False,
                            "errors": [_error(exc)], "gaps": ["current_final_inputs"]}
                try:
                    raw = jev_evaluator(context)
                    route = raw.get("route") if type(raw) is dict else None
                    if type(route) is not str or route not in ROUTES:
                        raise StageError("现有 Jev review 接口返回无效 route", "JEV_RESPONSE_INVALID")
                    status, error_code = "evaluated", ""
                except Exception as exc:
                    error_code = getattr(exc, "code", "JEV_EVALUATION_FAILED")
                    if error_code not in {"JEV_RESPONSE_INVALID", "JEV_EVALUATION_FAILED"}:
                        error_code = "JEV_EVALUATION_FAILED"
                    route, status = "UNKNOWN", "failed"
                record = {"schema_version": 1, "task_sha256": task_sha,
                          "artifact_sha256": artifact_sha, "evidence_sha256": evidence_sha,
                          "status": status, "route": route, "error_code": error_code,
                          "created_at": _timestamp()}
                fd_tmp, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=directory)
                try:
                    os.fchmod(fd_tmp, 0o600)
                    with os.fdopen(fd_tmp, "w", encoding="utf-8") as stream:
                        stream.write(canonical_json(record) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp_name, path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
    except StageError as exc:
        return {"valid": False, "ready_for_next_stage": False,
                "errors": [_error(exc)], "gaps": ["jev_decision"]}
    finally:
        os.close(fd)
    return {"valid": record["status"] == "evaluated",
            "ready_for_next_stage": record["status"] == "evaluated" and
                record["route"] in {"PASS", "ACCEPT_WITH_NOTES"},
            "route": record["route"], "cached": cached,
            "task_sha256": task_sha, "artifact_sha256": artifact_sha,
            "evidence_sha256": evidence_sha,
            "errors": ([] if record["status"] == "evaluated" else
                       [{"code": record["error_code"], "message": "Jev 评估失败；相同摘要不会自动重试"}]),
            "gaps": ([] if record["status"] == "evaluated" and
                     record["route"] in {"PASS", "ACCEPT_WITH_NOTES"} else ["jev_decision"])}
