import io
import json
import multiprocessing
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HOOK = Path(__file__).parents[1] / 'hooks/astra_relay_subagent_redirect.py'


def invoke(root, ev, external):
    output = io.StringIO()
    with patch.object(Path, 'home', return_value=root), patch('sys.stdin', io.StringIO(json.dumps(ev))), patch('sys.stdout', output), patch.object(subprocess, 'run', side_effect=external):
        try: runpy.run_path(str(HOOK), run_name='__main__')
        except SystemExit: pass
    return json.loads(output.getvalue())['hookSpecificOutput']['permissionDecisionReason']


def invoke_output(root, ev, external):
    output = io.StringIO()
    with patch.object(Path, 'home', return_value=root), patch('sys.stdin', io.StringIO(json.dumps(ev))), patch('sys.stdout', output), patch.object(subprocess, 'run', side_effect=external):
        try: runpy.run_path(str(HOOK), run_name='__main__')
        except SystemExit: pass
    return json.loads(output.getvalue())['hookSpecificOutput']


class HookReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.map = self.root / '.local/state/astra-herdr-relay/hook-agent-map.json'

    def spawn(self, sid, name, external=None):
        def fake(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'name': name} if cmd[1] == 'spawn' else {'accepted': True, 'task_id': 'relay-test'}), '')
        return invoke(self.root, {'session_id': sid, 'tool_name': 'spawn_agent', 'tool_input': {'task_name': 'worker', 'message': '任务'}}, external or fake)

    def route(self, sid, target):
        calls = []
        def fake(cmd, **kw):
            calls.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, '', '')
        invoke(self.root, {'session_id': sid, 'tool_name': 'send_message', 'tool_input': {'target': target, 'message': '消息'}}, fake)
        return calls

    def test_sessions_and_legacy_names(self):
        self.map.parent.mkdir(parents=True)
        self.map.write_text(json.dumps({'targets': {'worker': 'codex-old', 'codex-old': 'codex-old'}}))
        self.assertEqual(self.route('', 'worker'), ['codex-old'])
        self.assertEqual(self.route('new', 'worker'), [])
        self.spawn('a', 'codex-a')
        self.spawn('b', 'codex-b')
        self.assertEqual(self.route('a', 'worker'), ['codex-a'])
        self.assertEqual(self.route('b', 'worker'), ['codex-b'])
        self.assertEqual(self.route('c', 'worker'), [])
        self.assertEqual(self.route('c', 'codex-a'), ['codex-a'])
        self.assertEqual(self.route('', 'worker'), ['codex-old'])

    def test_concurrent_spawn_keeps_both_records(self):
        ctx = multiprocessing.get_context('fork')
        barrier = ctx.Barrier(2)
        def child(sid):
            def fake(cmd, **kw):
                if cmd[1] == 'spawn': barrier.wait(5)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'name': 'codex-' + sid} if cmd[1] == 'spawn' else {'accepted': True, 'task_id': 'relay-test'}), '')
            self.spawn(sid, 'codex-' + sid, fake)
        processes = [ctx.Process(target=child, args=(sid,)) for sid in ('a', 'b')]
        for p in processes: p.start()
        for p in processes:
            p.join(8)
            if p.is_alive(): p.kill(); p.join(); self.fail('映射锁死锁')
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(self.route('a', 'worker'), ['codex-a'])
        self.assertEqual(self.route('b', 'worker'), ['codex-b'])
        self.assertEqual(list(self.map.parent.glob('*.tmp')), [])

    def test_timeout_budget_and_requested_name(self):
        calls = []
        def fake(cmd, **kw):
            calls.append(kw['timeout'])
            raise subprocess.TimeoutExpired('模拟超时', kw['timeout'])
        reason = self.spawn('a', 'unused', fake)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0], 115)
        self.assertIn('codex-worker', reason)
        config = json.loads(HOOK.with_name('hooks.fragment.json').read_text())
        outer = config['hooks']['PreToolUse'][0]['hooks'][0]['timeout']
        self.assertGreater(outer, calls[0] + 25)

    def test_same_task_name_in_different_sessions_gets_unique_physical_names(self):
        calls = []

        def fake(cmd, **kw):
            if cmd[1] == 'spawn':
                requested = cmd[cmd.index('--name') + 1]
                calls.append(requested)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'name': requested}), '')
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True, 'task_id': 'relay-test'}), '')

        for sid, turn, tool_use in (('session-a', 'turn-a', 'tool-a'),
                                    ('session-b', 'turn-b', 'tool-b')):
            invoke(self.root, {
                'session_id': sid, 'turn_id': turn, 'tool_use_id': tool_use,
                'tool_name': 'spawn_agent',
                'tool_input': {'task_name': 'worker', 'message': '任务'},
            }, fake)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertTrue(all(name.startswith('codex-worker-') for name in calls))
        self.assertTrue(all(len(name) <= 32 for name in calls))
        self.assertEqual(self.route('session-a', 'worker'), [calls[0]])
        self.assertEqual(self.route('session-b', 'worker'), [calls[1]])

    def test_agent_tool_is_denied_and_points_to_visible_relay_agent(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, '', '')

        result = invoke_output(self.root, {
            'tool_name': 'Agent',
            'tool_input': {'description': '不要放行隐藏代理'},
        }, fake)

        self.assertEqual(calls, [])
        self.assertEqual(result['permissionDecision'], 'deny')
        reason = result['permissionDecisionReason']
        self.assertIn('Relay', reason)
        self.assertIn('visible', reason)

    def test_spawn_dispatch_mapping_failure_reports_real_partial_credentials(self):
        calls = []
        agent = 'codex-worker-real'
        task_id = 'relay-task-real'

        def fake(cmd, **kw):
            calls.append(cmd[1])
            if cmd[1] == 'spawn':
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'name': agent}), '')
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True, 'task_id': task_id}), '')

        with patch('os.replace', side_effect=OSError('映射保存失败')):
            reason = self.spawn('session-a', agent, fake)
        self.assertEqual(calls, ['spawn', 'dispatch-opaque'])
        self.assertIn('RELAY_REDIRECT_PARTIAL', reason)
        self.assertIn(agent, reason)
        self.assertIn(task_id, reason)
        self.assertIn('映射保存失败', reason)
        self.assertNotIn('RELAY_REDIRECT_OK', reason)

    def test_spawn_dispatch_invalid_task_id_is_partial_without_mapping(self):
        agent = 'codex-worker-real'
        for invalid_task_id in (None, '', 123):
            with self.subTest(task_id=invalid_task_id):
                calls = []

                def fake(cmd, **kw):
                    calls.append(cmd[1])
                    if cmd[1] == 'spawn':
                        return subprocess.CompletedProcess(cmd, 0, json.dumps({'name': agent}), '')
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True, 'task_id': invalid_task_id}), '')

                reason = self.spawn('session-invalid', agent, fake)
                self.assertEqual(calls, ['spawn', 'dispatch-opaque'])
                self.assertIn('RELAY_REDIRECT_PARTIAL', reason)
                self.assertIn(agent, reason)
                self.assertIn('task_id', reason)
                self.assertNotIn('RELAY_REDIRECT_OK', reason)
                self.assertFalse(self.map.exists())

    def test_safe_name_uses_top_level_event_identity(self):
        calls = []

        def fake(cmd, **kw):
            if cmd[1] == 'spawn':
                calls.append(cmd[cmd.index('--name') + 1])
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'name': calls[-1]}), '')
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True, 'task_id': 'relay-test'}), '')

        with patch('uuid.uuid4', side_effect=[type('UUID', (), {'hex': 'a' * 32})(),
                                               type('UUID', (), {'hex': 'b' * 32})()]):
            for sid in ('session-a', 'session-b'):
                invoke(self.root, {
                    'session_id': sid,
                    'turn_id': 'same-turn',
                    'tool_use_id': 'same-tool',
                    'tool_name': 'spawn_agent',
                    'tool_input': {
                        'task_name': 'worker',
                        'message': '任务',
                        'session_id': 'wrong-input-session',
                        'turn_id': 'wrong-input-turn',
                        'tool_use_id': 'wrong-input-tool',
                    },
                }, fake)

        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][len('codex-worker-'):len('codex-worker-') + 6],
                            calls[1][len('codex-worker-'):len('codex-worker-') + 6])
