from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from task_contract import RULES_VERSION, canonical_digest, canonical_json, docs_digest, validate_task


SCORE_DIMENSIONS = (
    ("goal", 20),
    ("scope", 20),
    ("behavior", 25),
    ("sources", 15),
    ("acceptance", 20),
)
BLOCKER_DIMENSIONS = (
    "ambiguity",
    "requirement_conflict",
    "unverifiable_acceptance",
    "missing_source",
    "authorization_overreach",
)
BLOCKER_CHOICES = ("present", "absent", "unknown")
LEVELS = (
    "缺失，无法执行或判断",
    "严重不足，关键内容尚未说明",
    "存在影响实现方向的重要未决项",
    "只有不影响方向的小缺口",
    "明确、可执行且与任务包一致",
)
BLOCKER_LABELS = {
    "present": "存在此阻断项",
    "absent": "确认不存在此阻断项",
    "unknown": "证据不足，无法判断",
}
CONFIDENCE_THRESHOLD = 0.95
SCORE_THRESHOLD = 95.0
DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"


class GateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _score_question(key: str):
    return {
        "type": "score",
        "instructions": f"只根据任务包，对 {key} 维度按 0 到 4 评分。",
        "criteria": list(LEVELS),
    }


def _blocker_question(key: str):
    return {
        "type": "choice",
        "instructions": f"判断任务包是否存在 {key} 阻断项；证据不足必须选择 unknown。",
        "criteria": dict(BLOCKER_LABELS),
    }


def build_request(task, model: str, *, doc_context=None):
    task = validate_task(task)
    if type(model) is not str or not model.strip():
        raise GateError("TASK_GATE_CONFIG_INVALID", "TypeSafe model 必须是非空字符串")
    digest = canonical_digest(task)
    questions = {"score_" + key: _score_question(key) for key, _ in SCORE_DIMENSIONS}
    questions.update({"blocker_" + key: _blocker_question(key)
                      for key in BLOCKER_DIMENSIONS})
    state = {
        "task_package": task,
        "task_sha256": digest,
        "rules_version": RULES_VERSION,
    }
    if task["schema_version"] == 2:
        if doc_context is None and task["required_doc_ids"]:
            raise GateError("DOC_REQUIRED_MISSING", "TypeSafe 评估缺少已登记的必需资料摘录")
        state["context_documents"] = doc_context or []
    return {
        "state": state,
        "model": model,
        "questions": questions,
    }


def _number(value, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 必须是有限数值")
    number = float(value)
    if not minimum <= number <= maximum:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 越界")
    return number


def _probabilities(value, expected_keys, name: str):
    if type(value) is not dict or set(value) != set(expected_keys):
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 概率字段缺失或多余")
    parsed = {key: _number(item, f"{name}.probabilities.{key}", 0, 1)
              for key, item in value.items()}
    if abs(sum(parsed.values()) - 1.0) > 0.02:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 概率总和不为 1")
    return parsed


def _score_answer(answer, name: str):
    if type(answer) is not dict or answer.get("type") != "score":
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 不是 Score 答案")
    for key in ("score", "confidence", "legend", "probabilities"):
        if key not in answer:
            raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.{key} 缺失")
    score = _number(answer["score"], f"{name}.score", 0, 4)
    confidence = _number(answer["confidence"], f"{name}.confidence", 0, 1)
    levels = tuple(str(i) for i in range(5))
    legend = answer["legend"]
    if type(legend) is not dict or set(legend) != set(levels) or any(
            type(label) is not str or not label.strip() for label in legend.values()):
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.legend 不符合五级量表")
    probabilities = _probabilities(answer["probabilities"], levels, name)
    return {"score": score, "confidence": confidence, "probabilities": probabilities}


def _choice_answer(answer, name: str):
    if type(answer) is not dict or answer.get("type") != "choice":
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 不是 Choice 答案")
    for key in ("choice", "confidence", "probabilities"):
        if key not in answer:
            raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.{key} 缺失")
    choice = answer["choice"]
    if type(choice) is not str or choice not in BLOCKER_CHOICES:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.choice 非法")
    confidence = _number(answer["confidence"], f"{name}.confidence", 0, 1)
    if confidence < CONFIDENCE_THRESHOLD:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.confidence 低于门槛")
    probabilities = _probabilities(answer["probabilities"], BLOCKER_CHOICES, name)
    if probabilities[choice] != max(probabilities.values()):
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.choice 不是最高概率类别")
    return {"choice": choice, "confidence": confidence, "probabilities": probabilities}


def parse_response(response):
    if type(response) is not dict or type(response.get("model")) is not str or not response["model"]:
        raise GateError("TASK_GATE_RESPONSE_INVALID", "TypeSafe 响应缺少 model")
    answers = response.get("answers")
    expected = ({"score_" + key for key, _ in SCORE_DIMENSIONS} |
                {"blocker_" + key for key in BLOCKER_DIMENSIONS})
    if type(answers) is not dict or set(answers) != expected:
        raise GateError("TASK_GATE_RESPONSE_INVALID", "TypeSafe answers 缺少或包含未知题目")
    scores = {}
    weighted = 0.0
    for key, weight in SCORE_DIMENSIONS:
        answer = _score_answer(answers["score_" + key], "score_" + key)
        points = weight * answer["score"] / 4
        weighted += points
        scores[key] = {**answer, "weight": weight, "points": points}
    blockers = {key: _choice_answer(answers["blocker_" + key], "blocker_" + key)
                for key in BLOCKER_DIMENSIONS}
    approved = weighted >= SCORE_THRESHOLD and all(
        answer["choice"] == "absent" and answer["confidence"] >= CONFIDENCE_THRESHOLD
        for answer in blockers.values())
    return {
        "approved": approved,
        "total_score": weighted,
        "scores": scores,
        "blockers": blockers,
        "response_model": response["model"],
    }


def cache_fingerprint(task, model: str) -> tuple[str, str, str]:
    task_digest = canonical_digest(task)
    evidence_digest = docs_digest(task)
    identity = {
        "task_sha256": task_digest,
        "rules_version": RULES_VERSION,
        "model": model,
        "docs_sha256": evidence_digest,
    }
    fingerprint = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return fingerprint, task_digest, evidence_digest


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if type(value) is dict else None


def http_request(payload, api_key: str, timeout: float, url: str = DEFAULT_URL):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def evaluate_once(task, model: str, api_key: str, state_dir, *, requester=None,
                  timeout=15, url=DEFAULT_URL):
    if type(api_key) is not str or not api_key.strip():
        raise GateError("TASK_GATE_CONFIG_INVALID", "TypeSafe API key 未配置")
    if type(timeout) not in {int, float} or type(timeout) is bool or timeout <= 0:
        raise GateError("TASK_GATE_CONFIG_INVALID", "TypeSafe timeout 必须大于零")
    doc_context = None
    if type(task) is dict and task.get("schema_version") == 2:
        try:
            from doc_registry import load_required_docs
            doc_context = load_required_docs(task, state_dir)
        except Exception as exc:
            raise GateError(getattr(exc, "code", "DOC_RECORD_INVALID"), str(exc)) from exc
    payload = build_request(task, model, doc_context=doc_context)
    fingerprint, task_digest, evidence_digest = cache_fingerprint(task, model)
    directory = Path(state_dir) / "gates"
    record_path = directory / (fingerprint + ".json")
    lock_path = directory / (fingerprint + ".lock")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    provider = requester or http_request
    with lock_path.open("a", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = _read_json(record_path)
        if previous is None and record_path.exists():
            return {
                "request_id": "jev-" + uuid.uuid4().hex,
                "fingerprint": fingerprint,
                "task_sha256": task_digest,
                "docs_sha256": evidence_digest,
                "rules_version": RULES_VERSION,
                "model": model,
                "status": "UNKNOWN",
                "error_code": "GATE_RECORD_INVALID",
                "cached": True,
            }
        if previous is not None:
            if previous.get("status") == "REQUESTED":
                previous.update(status="UNKNOWN", error_code="EVALUATION_INTERRUPTED",
                                completed_at=time.time(), cached=True)
                _write_json(record_path, previous)
            else:
                previous["cached"] = True
            return previous
        record = {
            "request_id": "jev-" + uuid.uuid4().hex,
            "fingerprint": fingerprint,
            "task_sha256": task_digest,
            "docs_sha256": evidence_digest,
            "rules_version": RULES_VERSION,
            "model": model,
            "status": "REQUESTED",
            "requested_at": time.time(),
        }
        _write_json(record_path, record)
        try:
            response = (provider(payload, api_key, float(timeout)) if requester is not None
                        else provider(payload, api_key, float(timeout), url=url))
        except (TimeoutError, urllib.error.URLError, OSError):
            record.update(status="UNKNOWN", error_code="JEV_TRANSPORT_UNKNOWN",
                          completed_at=time.time())
            _write_json(record_path, record)
            return record
        except Exception:
            record.update(status="FAILED", error_code="JEV_REQUEST_FAILED",
                          completed_at=time.time())
            _write_json(record_path, record)
            return record
        try:
            decision = parse_response(response)
        except GateError:
            record.update(status="FAILED", error_code="JEV_RESPONSE_INVALID",
                          completed_at=time.time())
            _write_json(record_path, record)
            return record
        record.update(status="APPROVED" if decision["approved"] else "REJECTED",
                      completed_at=time.time(), **decision)
        _write_json(record_path, record)
        return record


def approval_matches(task, model: str, state_dir) -> bool:
    try:
        fingerprint, task_digest, evidence_digest = cache_fingerprint(task, model)
        if task["schema_version"] == 2:
            from doc_registry import load_required_docs
            load_required_docs(task, state_dir)
    except (ValueError, TypeError):
        return False
    except Exception:
        return False
    record = _read_json(Path(state_dir) / "gates" / (fingerprint + ".json"))
    return bool(record and record.get("status") == "APPROVED" and
                record.get("approved") is True and record.get("fingerprint") == fingerprint and
                record.get("task_sha256") == task_digest and
                record.get("docs_sha256") == evidence_digest and
                record.get("rules_version") == RULES_VERSION and
                record.get("model") == model)


def require_approval(task, model: str, state_dir) -> str:
    if type(task) is dict and task.get("schema_version") == 2:
        try:
            from doc_registry import load_required_docs
            load_required_docs(task, state_dir)
        except Exception as exc:
            raise GateError(getattr(exc, "code", "DOC_RECORD_INVALID"), str(exc)) from exc
    if not approval_matches(task, model, state_dir):
        raise GateError("TASK_GATE_REQUIRED", "没有与当前任务摘要、规则、模型和资料版本匹配的 Jev 批准")
    return canonical_digest(task)
