import io
import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_relay import approved_test_task, m, seed_agent


class FlowRegressionTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for key in ('STATE', 'REGISTRY', 'TASKS', 'EVENTS', 'SEMANTIC', 'REVIEWS', 'LOCKS'):
            self.enterContext(patch.object(m, key, root / key))
        self.enterContext(patch.object(m, 'HERDR_SESSION', 'relay-test-session'))
        seed_agent()
        m.ensure()
        self.enterContext(patch.object(m, 'agent_status_obj', return_value={'result': {'agent': {
            'name':'worker','tab_id':'tab-worker','pane_id':'pane-worker','status':'done'}}}))
        self.enterContext(patch.object(m, 'sensor', return_value={'semantic_status': 'COMPLETED'}))
        self.markers = []
        self.enterContext(patch.object(m, 'run', side_effect=self.fake_run))
        self.enterContext(patch.object(m.subprocess, 'Popen'))

    def fake_run(self, cmd, **kwargs):
        if 'prompt' in cmd:
            import re
            prompt = cmd[cmd.index('prompt') + 2]
            self.markers.append(re.search(r'^\[RELAY TASK [^\]]+\]$',prompt,re.M).group(0))
            return subprocess.CompletedProcess(cmd, 0, '', '')
        return subprocess.CompletedProcess(cmd, 0, '\n'.join(self.markers + ['验证证据', self.markers[-1]]), '')

    def test_review_errors_end_in_deep_review(self):
        for value in (RuntimeError('接口异常'), {'completion': float('nan')}):
            with self.subTest(value=value), patch.object(m, 'jev_call', side_effect=value if isinstance(value, Exception) else None, return_value=value):
                state = m.delegate('worker', approved_test_task(m.STATE))
                self.assertEqual(state['final_state'], 'DEEP_REVIEW')
                self.assertIn('error', state['last_review'])

    def test_blocked_is_not_rework(self):
        with patch.object(m, 'jev_call', return_value={'route': 'BLOCKED'}):
            self.assertEqual(m.review('t', 'worker', '任务', '证据')['route'], 'BLOCKED')

    def test_semantic_completion_does_not_unlock_running_agent(self):
        for runtime in ('working', 'unknown'):
            with self.subTest(runtime=runtime), patch.object(m, 'agent_status_obj', return_value={'result': {'agent': {
                    'name':'worker','tab_id':'tab-worker','pane_id':'pane-worker','status':runtime}}}), patch.object(m, 'review', return_value={'route': 'PASS'}) as review:
                state = m.delegate('worker', approved_test_task(m.STATE))
                self.assertEqual(state['final_state'], 'UNKNOWN')
                review.assert_not_called()
                self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'BUSY')
                self.assertTrue((m.LOCKS / 'worker.lock').exists())
            (m.LOCKS / 'worker.lock').unlink()
            seed_agent()

    def test_rework_is_returned_without_synchronous_redispatch(self):
        with patch.object(m, 'review', return_value={'route': 'REWORK'}) as review:
            self.assertEqual(m.delegate('worker', approved_test_task(m.STATE))['final_state'], 'REWORK')
        self.assertEqual(len(self.markers), 1)
        self.assertIn('验证证据', review.call_args.args[3])

    def test_missing_marker_never_reviews_history(self):
        with patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 0, '其他任务', '')), patch.object(m, 'review') as review:
            self.assertEqual(m.delegate('worker', approved_test_task(m.STATE))['final_state'], 'HUMAN_REVIEW')
            review.assert_not_called()

    def test_timeout_preserves_lock(self):
        with patch.object(m, 'run', side_effect=subprocess.TimeoutExpired('herdr', 1)):
            state = m.delegate('worker', approved_test_task(m.STATE))
        self.assertEqual(state['final_state'], 'UNKNOWN')
        self.assertTrue((m.LOCKS / 'worker.lock').exists())

    def test_review_exception_is_saved_in_task(self):
        with patch.object(m, 'review', side_effect=RuntimeError('审查异常')):
            state = m.delegate('worker', approved_test_task(m.STATE))
        self.assertEqual(state['final_state'], 'DEEP_REVIEW')
        self.assertEqual(m.read_json(m.TASKS / (state['task_id'] + '.json'), {})['error'], '审查异常')

    def test_opaque_stale_done_keeps_lock(self):
        m.dispatch_opaque('worker', approved_test_task(m.STATE))
        with patch.object(m, 'herdr_json', return_value={'result': {'agents': [{'name': 'worker', 'status': 'done','tab_id':'tab-worker','pane_id':'pane-worker'}]}}):
            m.sync()
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'BUSY')
        self.assertTrue((m.LOCKS / 'worker.lock').exists())

    def test_removed_agent_is_not_revived(self):
        def review(*args):
            m.save_registry({'agents': {'worker': {'lifecycle': 'REMOVED'}}})
            return {'route': 'PASS'}
        with patch.object(m, 'review', side_effect=review):
            m.delegate('worker', approved_test_task(m.STATE))
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'REMOVED')

    def test_opaque_send_failure_is_not_accepted(self):
        with patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 1, '', '发送失败')):
            result = m.dispatch_opaque('worker', approved_test_task(m.STATE))
        self.assertFalse(result['accepted'])
        self.assertTrue((m.LOCKS / 'worker.lock').exists())

    def test_opaque_lock_and_explicit_settlement(self):
        result = m.dispatch_opaque('worker', approved_test_task(m.STATE))
        self.assertTrue(result['accepted'])
        self.assertTrue((m.LOCKS / 'worker.lock').exists())
        with self.assertRaises(SystemExit):
            m.dispatch_opaque('worker', approved_test_task(m.STATE,task_id='reentry'))
        with patch.object(m, 'herdr_json', return_value={'result': {'agents': [{'name': 'worker', 'status': 'working','tab_id':'tab-worker','pane_id':'pane-worker'}]}}):
            m.sync()
        with patch.object(m, 'herdr_json', return_value={'result': {'agents': [{'name': 'worker', 'status': 'done','tab_id':'tab-worker','pane_id':'pane-worker'}]}}), patch.object(m, 'review') as review:
            m.sync(settle=False)
            self.assertTrue((m.LOCKS / 'worker.lock').exists())
            m.sync()
            review.assert_not_called()
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'IDLE')
        self.assertFalse((m.LOCKS / 'worker.lock').exists())
        self.assertEqual(m.read_json(m.TASKS / (result['task_id'] + '.json'), {})['phase'], 'AWAITING_REVIEW')

    def test_dispatch_cli_failure_exits_nonzero(self):
        prompt = m.STATE / 'prompt.txt'
        prompt.write_text('任务')
        output = io.StringIO()
        with patch.object(m.sys, 'argv', ['relay', 'dispatch-opaque', 'worker', '--prompt-file', str(prompt)]), patch.object(m.sys, 'stdout', output), patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 1, '', '发送失败')):
            result=m.main()
        self.assertEqual(result,1)
        self.assertEqual(json.loads(output.getvalue())['error']['code'],'TASK_PACKAGE_REQUIRED')

    def test_hook_rejects_false_acceptance_even_with_zero_exit(self):
        hook = Path(__file__).parents[1] / 'hooks' / 'astra_relay_subagent_redirect.py'
        task=approved_test_task(m.STATE)
        event = {'cwd':task['working_directory'],'tool_name': 'spawn_agent', 'tool_input': {'task_name': 'worker', 'agent_type':'worker','message':json.dumps(task)}}
        output = io.StringIO()
        with patch.object(Path, 'home', return_value=m.STATE), patch.object(m.sys, 'stdin', io.StringIO(json.dumps(event))), patch.object(m.sys, 'stdout', output), patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '{"accepted":false,"error":{"code":"TASK_GATE_REQUIRED","message":"无批准"}}', '')):
            with self.assertRaises(SystemExit):
                runpy.run_path(str(hook), run_name='__main__')
        reason = json.loads(output.getvalue())['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn('RELAY_REDIRECT_FAILED', reason)
        self.assertNotIn('RELAY_REDIRECT_OK', reason)
        self.assertFalse((m.STATE / '.local/state/astra-herdr-relay/hook-agent-map.json').exists())

    def prepare_release(self):
        m.save_registry({'agents': {'worker': {'name':'worker','lifecycle': 'BUSY', 'tab_id': 'old-tab','pane_id':'old-pane','herdr_session':'relay-test-session','current_task_id': 'old-task'}}})
        return m.acquire_agent_lock('worker', 'old-task')

    def test_release_clears_own_lock_and_allows_same_name(self):
        lock = self.prepare_release()
        with patch.object(m, 'herdr_json', return_value={}):
            m.release('worker')
        self.assertFalse(lock.exists())
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'STOPPED')
        m.acquire_agent_lock('worker', 'new-task')
        self.assertEqual(lock.read_text(), 'new-task')

    def test_failed_close_preserves_lock(self):
        lock = self.prepare_release()
        with patch.object(m, 'herdr_json', side_effect=RuntimeError('关闭失败')):
            with self.assertRaises(RuntimeError):
                m.release('worker')
        self.assertEqual(lock.read_text(), 'old-task')

    def test_release_preserves_replacement_lock_and_record(self):
        lock = self.prepare_release()
        def replaced(*args, **kwargs):
            lock.write_text('new-task')
            m.save_registry({'agents': {'worker': {'lifecycle': 'BUSY', 'tab_id': 'new-tab', 'current_task_id': 'new-task'}}})
        with patch.object(m, 'herdr_json', side_effect=replaced):
            m.release('worker')
        self.assertEqual(lock.read_text(), 'new-task')
        self.assertEqual(m.registry()['agents']['worker']['tab_id'], 'new-tab')
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'BUSY')

    def test_release_does_not_delete_another_task_lock(self):
        lock = self.prepare_release()
        with patch.object(m, 'herdr_json', side_effect=lambda *args,**kwargs: lock.write_text('new-task')):
            m.release('worker')
        self.assertEqual(lock.read_text(), 'new-task')

    def test_release_rejects_running_accepted_opaque_task_before_mutation(self):
        accepted = m.dispatch_opaque('worker', approved_test_task(m.STATE))
        self.assertTrue(accepted['accepted'])
        before_registry = m.registry()
        task_path = m.TASKS / (accepted['task_id'] + '.json')
        before_task = m.read_json(task_path, {})
        lock = m.LOCKS / 'worker.lock'
        before_lock = lock.read_text()
        with patch.object(m, 'herdr_json') as herdr:
            with self.assertRaises(RuntimeError):
                m.release('worker')
        herdr.assert_not_called()
        self.assertEqual(m.registry(), before_registry)
        self.assertEqual(m.read_json(task_path, {}), before_task)
        self.assertTrue(lock.exists())
        self.assertEqual(lock.read_text(), before_lock)

    def test_release_allows_running_finalize_task_to_clean_claim(self):
        task_id = 'relay-finalize-running'
        token = 'finalize-token'
        m.save_registry({'agents': {'worker': {
            'name':'worker','lifecycle': 'BUSY', 'tab_id': 'old-tab','pane_id':'old-pane',
            'herdr_session':'relay-test-session','current_task_id': task_id,
            'operation': token, 'operation_kind': 'finalize',
        }}})
        lock = m.acquire_agent_lock('worker', task_id)
        m.write_json(m.TASKS / (task_id + '.json'), {
            'task_id': task_id, 'agent': 'worker', 'phase': 'RUNNING',
            'finalize_owner': token,
        })
        with patch.object(m, 'herdr_json', return_value={}):
            result = m.release('worker')
        self.assertEqual(result['lifecycle'], 'STOPPED')
        self.assertFalse(lock.exists())
        task = m.read_json(m.TASKS / (task_id + '.json'), {})
        self.assertEqual(task['phase'], 'HUMAN_REVIEW')
        self.assertNotIn('finalize_owner', task)
