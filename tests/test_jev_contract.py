import json
import unittest
from unittest.mock import patch, MagicMock
from test_relay import m


class JevContractTests(unittest.TestCase):
    def request(self, mode, response):
        stream = MagicMock()
        stream.__enter__.return_value.read.return_value = json.dumps(response).encode()
        with patch.object(m, 'api_key', return_value='test-key'), patch.object(m.urllib.request, 'urlopen', return_value=stream) as call:
            result = m.jev_call('', '测试证据', mode=mode)
        return result, json.loads(call.call_args.args[0].data)

    def test_sensor_uses_typed_choice(self):
        result, payload = self.request('sensor', {'answers': {'semantic_status': {
            'type': 'choice', 'choice': 'WAITING', 'confidence': .95}}})
        self.assertEqual(payload['state'], '测试证据')
        self.assertNotIn('messages', payload)
        self.assertEqual(payload['questions']['semantic_status']['type'], 'choice')
        self.assertEqual(result['semantic_status'], 'WAITING')

    def test_review_requires_all_typed_probabilities(self):
        answers = {key: {'type': 'noul', 'noul': .95} for key in m.JEV_REVIEW_QUESTIONS}
        result, payload = self.request('review', {'answers': answers})
        self.assertEqual(set(payload['questions']), set(m.JEV_REVIEW_QUESTIONS))
        self.assertEqual(result['completion'], .95)
        self.assertEqual(result['route'], 'BLOCKED')

    def test_final_review_adds_only_final_gate_question(self):
        answers = {key: {'type': 'noul', 'noul': .1} for key in m.JEV_FINAL_REVIEW_QUESTIONS}
        result, payload = self.request('final_review', {'answers': answers})
        self.assertIn('accept_with_notes', payload['questions'])
        self.assertNotIn('accept_with_notes', m.JEV_REVIEW_QUESTIONS)
        self.assertEqual(result['accept_with_notes'], .1)

    def test_final_route_follows_jev_and_blocking_scores(self):
        scores={key:.1 for key in m.JEV_FINAL_REVIEW_QUESTIONS}
        scores.update(completion=.95,requirement_match=.95,result_confidence=.95,
                      evidence_quality=.95,confidence=.95,accept_with_notes=.9,route='PASS')
        with patch.object(m,'jev_call',return_value=scores), \
             patch.object(m,'write_json'), patch.object(m,'event'):
            notes=m.review('relay-notes','worker','任务','证据',final_gate=True,has_ocr_findings=True)
            plain=m.review('relay-plain','worker','任务','证据',final_gate=True,has_ocr_findings=False)
            scores['implementation_defect']=.9
            blocking=m.review('relay-blocking','worker','任务','证据',final_gate=True,has_ocr_findings=True)
        self.assertEqual(notes['route'],'ACCEPT_WITH_NOTES')
        self.assertEqual(plain['route'],'PASS')
        self.assertEqual(blocking['route'],'REWORK')

    def test_malformed_answers_fail_closed(self):
        for response in ({}, {'answers': {}}, {'answers': {'semantic_status': {
            'type': 'choice', 'choice': 'EXECUTE', 'confidence': True}}}):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.request('sensor', response)

    def test_review_rejects_missing_boolean_and_nonfinite_scores(self):
        for value in (None, True, float('nan'), float('inf'), -1, 2):
            answers = {key: {'type': 'noul', 'noul': .95} for key in m.JEV_REVIEW_QUESTIONS}
            answers['completion']['noul'] = value
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.request('review', {'answers': answers})
