import multiprocessing
import subprocess
import unittest
from unittest.mock import patch

import test_event_regression
from test_relay import approved_test_task, m, seed_agent


class FinalizeRegressionTests(unittest.TestCase):
    setUp = test_event_regression.EventRegressionTests.setUp

    def prepare(self):
        seed_agent(tab_id='tab',pane_id='pane',created_at=123)
        self.output = ''
        self.runtime = 'done'
        def external(cmd, **kwargs):
            if 'prompt' in cmd:
                lines = cmd[cmd.index('prompt') + 2].splitlines()
                marker = next(line for line in lines if line.startswith('[RELAY TASK '))
                task_id = marker[len('[RELAY TASK '):-1]
                self.output = ('前一个任务的秘密\n' + marker + '\n' +
                                f'[RELAY FINISHED {task_id}]\n本任务验证通过')
                return subprocess.CompletedProcess(cmd, 0, '', '')
            return subprocess.CompletedProcess(cmd, 0, self.output, '')
        self.enterContext(patch.object(m, 'run', side_effect=external))
        self.enterContext(patch.object(m, 'agent_status_obj', side_effect=lambda name, **kwargs: {
            'result': {'agent': {'name': name, 'tab_id': 'tab', 'pane_id': 'pane', 'status': self.runtime}}
        }))
        self.task_id = m.dispatch_opaque('worker', approved_test_task(m.STATE))['task_id']
        self.path = m.TASKS / (self.task_id + '.json')

    def require_finalize(self):
        self.assertTrue(callable(getattr(m, 'finalize', None)), '缺少显式 finalize 入口')

    def test_dispatch_persists_marker_and_instance(self):
        self.prepare()
        state = m.read_json(self.path, {})
        self.assertEqual(state.get('marker'), '[RELAY TASK ' + self.task_id + ']')
        self.assertEqual(state.get('completion_marker'), '[RELAY FINISHED ' + self.task_id + ']')
        self.assertEqual(state.get('instance'), {'herdr_session': 'relay-test-session',
                                                 'tab_id': 'tab', 'pane_id': 'pane', 'created_at': 123})

    def test_short_task_routes_are_persisted_once_without_rework(self):
        self.require_finalize()
        for route in ('PASS', 'REWORK', 'BLOCKED', 'DEEP_REVIEW'):
            with self.subTest(route=route):
                self.prepare()
                with patch.object(m, 'review', return_value={'route': route}) as review:
                    result = m.finalize(self.task_id)
                    self.assertEqual(result['final_state'], 'DONE' if route == 'PASS' else route)
                    self.assertEqual(m.finalize(self.task_id), result)
                    review.assert_called_once()
                    evidence = review.call_args.args[3]
                    self.assertIn('本任务验证通过', evidence)
                    self.assertNotIn('前一个任务的秘密', evidence)
                    self.assertEqual(m.read_json(self.path, {})['final_state'], result['final_state'])
                m.release_lock(m.LOCKS / 'worker.lock', self.task_id)

    def test_running_refuses_review_and_preserves_lock(self):
        self.require_finalize()
        self.prepare()
        self.runtime = 'working'
        with patch.object(m, 'review') as review:
            m.finalize(self.task_id)
            review.assert_not_called()
        self.assertEqual((m.LOCKS / 'worker.lock').read_text(), self.task_id)
        self.assertNotEqual(m.read_json(self.path, {}).get('final_state'), 'DONE')

    def test_blocked_runtime_never_reviews_or_releases_task(self):
        self.require_finalize()
        self.prepare()
        self.runtime = 'blocked'
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'BLOCKED')
        review.assert_not_called()
        self.assertTrue((m.LOCKS / 'worker.lock').exists())
        self.assertEqual(m.registry()['agents']['worker']['current_task_id'], self.task_id)

    def test_missing_runtime_identity_fails_closed_without_review(self):
        self.require_finalize()
        self.prepare()
        with patch.object(m, 'agent_status_obj', return_value={
            'result': {'agent': {'name': 'worker', 'status': 'done'}}
        }), patch.object(m, 'review') as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'HUMAN_REVIEW')
        review.assert_not_called()
        self.assertTrue((m.LOCKS / 'worker.lock').exists())

    def test_dispatch_echo_without_result_never_reviews_stale_done(self):
        self.require_finalize()
        self.prepare()
        marker = m.read_json(self.path, {})['marker']
        self.output = f'{marker}\n任务'
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'HUMAN_REVIEW')
        self.assertIn('实际任务结果', result['error'])
        review.assert_not_called()

    def test_dispatch_echo_with_old_result_never_reviews(self):
        self.require_finalize()
        self.prepare()
        marker = m.read_json(self.path, {})['marker']
        self.output = f'{marker}\n任务\n旧任务已完成'
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'HUMAN_REVIEW')
        review.assert_not_called()

    def test_dispatch_echo_with_unrelated_text_never_reviews(self):
        self.require_finalize()
        self.prepare()
        marker = m.read_json(self.path, {})['marker']
        self.output = f'{marker}\n任务\n无关追加文本'
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'HUMAN_REVIEW')
        review.assert_not_called()

    def test_completion_marker_with_result_is_the_only_reviewable_evidence(self):
        self.require_finalize()
        self.prepare()
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'DONE')
        self.assertEqual(review.call_args.args[3],
                         f'[RELAY FINISHED {self.task_id}]\n本任务验证通过')

    def test_bullet_prefixed_completion_marker_is_current_assistant_reply(self):
        self.require_finalize()
        self.prepare()
        marker = m.read_json(self.path, {})['marker']
        completion = m.read_json(self.path, {})['completion_marker']
        self.output = f'{marker}\n任务\n• {completion}\n本任务验证通过'
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'DONE')
        self.assertEqual(review.call_args.args[3], f'• {completion}\n本任务验证通过')

    def test_prompt_echo_prefix_cannot_be_completion_marker(self):
        self.require_finalize()
        self.prepare()
        marker = m.read_json(self.path, {})['marker']
        completion = m.read_json(self.path, {})['completion_marker']
        self.output = f'{marker}\n任务\n› {completion}\n本任务验证通过'
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'HUMAN_REVIEW')
        review.assert_not_called()

    def test_completion_marker_without_result_never_reviews(self):
        self.require_finalize()
        self.prepare()
        marker = m.read_json(self.path, {})['marker']
        completion = m.read_json(self.path, {})['completion_marker']
        self.output = f'{marker}\n任务\n{completion}'
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'HUMAN_REVIEW')
        review.assert_not_called()

    def test_bullet_completion_marker_without_result_never_reviews(self):
        self.require_finalize()
        self.prepare()
        marker = m.read_json(self.path, {})['marker']
        completion = m.read_json(self.path, {})['completion_marker']
        self.output = f'{marker}\n任务\n• {completion}'
        with patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
            result = m.finalize(self.task_id)
        self.assertEqual(result['final_state'], 'HUMAN_REVIEW')
        review.assert_not_called()

    def test_old_done_without_current_marker_requires_human(self):
        self.require_finalize()
        self.prepare()
        self.output = '[RELAY TASK old-task]\n旧任务已完成'
        with patch.object(m, 'review') as review:
            self.assertEqual(m.finalize(self.task_id)['final_state'], 'HUMAN_REVIEW')
            review.assert_not_called()

    def test_api_failure_fails_closed(self):
        self.require_finalize()
        self.prepare()
        with patch.object(m, 'jev_call', side_effect=RuntimeError('接口不可用')):
            self.assertEqual(m.finalize(self.task_id)['final_state'], 'DEEP_REVIEW')

    def test_changed_output_invalidates_review(self):
        self.require_finalize()
        self.prepare()
        def review(*args):
            self.output += '\n继续工作'
            return {'route': 'PASS'}
        with patch.object(m, 'review', side_effect=review):
            self.assertNotEqual(m.finalize(self.task_id)['final_state'], 'DONE')

    def test_concurrent_commands_cannot_pollute_review(self):
        self.require_finalize()
        self.prepare()
        def review(*args):
            for action in (lambda: m.finalize(self.task_id),
                           lambda: m.dispatch_opaque('worker', approved_test_task(m.STATE,task_id='new-task')),
                           lambda: m.send_opaque('worker', '干扰消息')):
                with self.assertRaises((SystemExit, RuntimeError, ValueError)):
                    action()
            return {'route': 'PASS'}
        with patch.object(m, 'review', side_effect=review):
            self.assertEqual(m.finalize(self.task_id)['final_state'], 'DONE')

    def test_released_instance_cannot_be_revived(self):
        self.require_finalize()
        self.prepare()
        def review(*args):
            with patch.object(m, 'herdr_json', return_value={}):
                m.release('worker')
            return {'route': 'PASS'}
        with patch.object(m, 'review', side_effect=review):
            self.assertNotEqual(m.finalize(self.task_id)['final_state'], 'DONE')
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'STOPPED')
        self.assertNotIn('operation', m.registry()['agents']['worker'])
        self.assertNotIn('operation_kind', m.registry()['agents']['worker'])
        self.assertNotIn('finalize_owner', m.read_json(self.path, {}))

    def test_cross_process_release_during_review_cleans_finalize_claim(self):
        self.require_finalize()
        self.prepare()
        ctx = multiprocessing.get_context('fork')
        entered, resume = ctx.Event(), ctx.Event()

        def child():
            def review(*args):
                entered.set()
                if not resume.wait(8):
                    raise RuntimeError('等待审查恢复超时')
                return {'route': 'PASS'}
            with patch.object(m, 'review', side_effect=review):
                m.finalize(self.task_id)

        process = ctx.Process(target=child)
        process.start()
        self.addCleanup(lambda: process.kill() if process.is_alive() else None)
        self.assertTrue(entered.wait(3), 'finalize 未进入审查阶段')
        with patch.object(m, 'herdr_json', return_value={}):
            released = m.release('worker')
        self.assertEqual(released['lifecycle'], 'STOPPED')
        record = m.registry()['agents']['worker']
        self.assertNotIn('operation', record)
        self.assertNotIn('operation_kind', record)
        self.assertEqual(m.read_json(self.path, {}).get('final_state'), 'HUMAN_REVIEW')
        self.assertNotIn('finalize_owner', m.read_json(self.path, {}))
        self.assertFalse((m.LOCKS / 'worker.lock').exists())
        resume.set()
        process.join(4)
        self.assertFalse(process.is_alive(), 'finalize 子进程未退出')
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'STOPPED')

    def test_reused_agent_is_not_reviewed(self):
        self.require_finalize()
        self.prepare()
        with m.registry_transaction() as registry:
            registry['agents']['worker']['current_task_id'] = 'new-task'
        with patch.object(m, 'review') as review:
            self.assertEqual(m.finalize(self.task_id)['final_state'], 'HUMAN_REVIEW')
            review.assert_not_called()
