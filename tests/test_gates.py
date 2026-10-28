import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "lib"))
from gates import (
    BLOCKER_DIMENSIONS,
    RULES_VERSION,
    SCORE_DIMENSIONS,
    approval_matches,
    build_request,
    evaluate_once,
    parse_response,
)
from test_task_contract import make_task


def valid_response(scores=None, blockers=None):
    scores = scores or {key: 4 for key, weight in SCORE_DIMENSIONS}
    blockers = blockers or {key: "absent" for key in BLOCKER_DIMENSIONS}
    answers = {}
    for key, _ in SCORE_DIMENSIONS:
        score = scores[key]
        answers["score_" + key] = {
            "type": "score",
            "score": score,
            "confidence": 0.99,
            "legend": {str(i): f"等级{i}" for i in range(5)},
            "probabilities": {str(i): (1.0 if i == score else 0.0) for i in range(5)},
        }
    for key in BLOCKER_DIMENSIONS:
        choice = blockers[key]
        answers["blocker_" + key] = {
            "type": "choice",
            "choice": choice,
            "confidence": 0.99,
            "probabilities": {item: (1.0 if item == choice else 0.0)
                              for item in ("present", "absent", "unknown")},
        }
    return {"model": "jev-test-1", "answers": answers}


class GateTests(unittest.TestCase):
    def test_request_uses_documented_score_and_choice_http_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task(directory)
            payload = build_request(task, "jev-test")
        self.assertEqual(payload["model"], "jev-test")
        self.assertEqual(set(payload["questions"]),
                         {"score_" + key for key, _ in SCORE_DIMENSIONS} |
                         {"blocker_" + key for key in BLOCKER_DIMENSIONS})
        self.assertTrue(all(question["type"] != "noul" for question in payload["questions"].values()))
        for key, _ in SCORE_DIMENSIONS:
            question = payload["questions"]["score_" + key]
            self.assertEqual(question["type"], "score")
            self.assertEqual(len(question["criteria"]), 5)
        for key in BLOCKER_DIMENSIONS:
            question = payload["questions"]["blocker_" + key]
            self.assertEqual(question["type"], "choice")
            self.assertEqual(set(question["criteria"]), {"present", "absent", "unknown"})

    def test_score_threshold_and_absent_blockers_approve(self):
        scores = {"goal": 4, "scope": 4, "behavior": 4, "sources": 4, "acceptance": 3}
        result = parse_response(valid_response(scores=scores))
        self.assertEqual(result["total_score"], 95)
        self.assertTrue(result["approved"])

    def test_any_present_or_unknown_blocker_rejects(self):
        blockers = {key: "absent" for key in BLOCKER_DIMENSIONS}
        blockers[BLOCKER_DIMENSIONS[0]] = "unknown"
        result = parse_response(valid_response(blockers=blockers))
        self.assertFalse(result["approved"])

    def test_boolean_score_low_choice_confidence_and_bad_probability_reject(self):
        response = valid_response()
        response["answers"]["score_goal"]["score"] = True
        with self.assertRaises(ValueError):
            parse_response(response)
        response = valid_response()
        response["answers"]["blocker_" + BLOCKER_DIMENSIONS[0]]["confidence"] = 0.94
        with self.assertRaises(ValueError):
            parse_response(response)
        response = valid_response()
        response["answers"]["score_goal"]["probabilities"] = {"0": 0.4}
        with self.assertRaises(ValueError):
            parse_response(response)

    def test_evaluation_caches_one_attempt_and_binds_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task(directory)
            state = Path(directory) / "state"
            calls = []

            def requester(payload, api_key, timeout):
                calls.append((payload, api_key, timeout))
                return valid_response()

            first = evaluate_once(task, "jev-test", "secret-for-test", state,
                                  requester=requester)
            second = evaluate_once(task, "jev-test", "secret-for-test", state,
                                   requester=requester)
            self.assertTrue(first["approved"])
            self.assertTrue(second["cached"])
            self.assertEqual(len(calls), 1)
            self.assertTrue(approval_matches(task, "jev-test", state))

    def test_timeout_becomes_unknown_without_automatic_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            task = make_task(directory)
            state = Path(directory) / "state"
            calls = []

            def timeout(payload, api_key, timeout):
                calls.append(payload)
                raise TimeoutError("模拟超时")

            first = evaluate_once(task, "jev-test", "secret-for-test", state,
                                  requester=timeout)
            second = evaluate_once(task, "jev-test", "secret-for-test", state,
                                   requester=timeout)
            self.assertEqual(first["status"], "UNKNOWN")
            self.assertEqual(second["status"], "UNKNOWN")
            self.assertEqual(len(calls), 1)
            self.assertFalse(approval_matches(task, "jev-test", state))

    def test_corrupt_attempt_record_fails_closed_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            task=make_task(directory)
            state=Path(directory)/"state"
            fingerprint,_,_=__import__("gates").cache_fingerprint(task,"jev-test")
            record=state/"gates"/(fingerprint+".json")
            record.parent.mkdir(parents=True)
            record.write_text("not-json",encoding="utf-8")
            calls=[]
            result=evaluate_once(task,"jev-test","unit-test-only",state,
                                 requester=lambda *args:calls.append(args))
            self.assertEqual(result["status"],"UNKNOWN")
            self.assertEqual(result["error_code"],"GATE_RECORD_INVALID")
            self.assertEqual(calls,[])


if __name__ == "__main__":
    unittest.main()
