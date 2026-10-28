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

def package(cwd,task_id='hook-task'):
    return {'schema_version':1,'task_id':task_id,'revision':1,'goal':'执行 Hook 测试任务',
            'scope':{'allowed':['tests'],'forbidden':['真实配置']},
            'behavior':{'inputs':['结构化包'],'outputs':['派发状态'],'errors':['非法包拒绝'],'compatibility':['保留可见代理']},
            'acceptance':['验证请求只派发一次'],'evidence_policy':['隔离测试'],'docs':[],
            'working_directory':str(Path(cwd).resolve())}


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
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True,'agent':name,'task_id':'relay-test','task_sha256':'a'*64}), '')
        ev={'session_id':sid,'turn_id':'turn-'+sid,'tool_use_id':'tool-'+sid,
            'cwd':str(Path.cwd().resolve()),'tool_name':'spawn_agent',
            'tool_input':{'task_name':'worker','agent_type':'worker',
                          'message':json.dumps(package(Path.cwd()),ensure_ascii=False)}}
        return invoke(self.root, ev, external or fake)

    def route(self, sid, target):
        calls = []
        def fake(cmd, **kw):
            calls.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, '', '')
        invoke(self.root, {'session_id': sid, 'tool_name': 'send_message', 'tool_input': {'target': target, 'message': '{"control":"status"}'}}, fake)
        return calls

    def test_free_text_spawn_is_denied_before_cli(self):
        calls=[]
        result=invoke_output(self.root,{
            'cwd':str(Path.cwd().resolve()),'tool_name':'spawn_agent',
            'tool_input':{'task_name':'worker','agent_type':'worker','message':'普通自由文本'}},
            lambda cmd,**kw:calls.append(cmd))
        self.assertEqual(calls,[])
        self.assertEqual(result['permissionDecision'],'deny')
        self.assertIn('TASK_PACKAGE_REQUIRED',result['permissionDecisionReason'])

    def test_message_json_task_uses_native_model_fields(self):
        calls=[]
        task=package(Path.cwd(),'native-shape')
        ev={'session_id':'session-native','turn_id':'turn-native','tool_use_id':'use-native',
            'cwd':str(Path.cwd().resolve()),'tool_name':'spawn_agent',
            'tool_input':{'task_name':'worker','agent_type':'worker',
                          'message':json.dumps(task,ensure_ascii=False),
                          'model':'gpt-6-luna','reasoning_effort':'max'}}
        def fake(cmd,**kwargs):
            calls.append(cmd)
            requested=cmd[cmd.index('--name')+1]
            return subprocess.CompletedProcess(cmd,0,json.dumps({
                'accepted':True,'agent':requested,'task_id':'relay-native','task_sha256':'1'*64}), '')
        result=invoke_output(self.root,ev,fake)
        self.assertEqual(result['permissionDecision'],'deny')
        self.assertIn('RELAY_REDIRECT_OK',result['permissionDecisionReason'])
        self.assertEqual(len(calls),1)
        self.assertIn('--model',calls[0]); self.assertEqual(calls[0][calls[0].index('--model')+1],'gpt-6-luna')
        self.assertIn('--reasoning',calls[0]); self.assertEqual(calls[0][calls[0].index('--reasoning')+1],'max')
        self.assertNotIn('--kind',calls[0])

    def test_unmatched_normal_hook_call_emits_no_invalid_allow(self):
        output=io.StringIO()
        with patch.object(Path,'home',return_value=self.root),patch('sys.stdin',io.StringIO(json.dumps({'tool_name':'Bash','tool_input':{'command':'true'}}))),patch('sys.stdout',output):
            try: runpy.run_path(str(HOOK),run_name='__main__')
            except SystemExit: pass
        self.assertEqual(output.getvalue(),'')

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
                barrier.wait(5)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True,'agent':'codex-' + sid,'task_id':'relay-test','task_sha256':'b'*64}), '')
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
        hook_source = HOOK.read_text()
        self.assertEqual(calls[0], 180)
        self.assertIn('INNER_COMMAND_TIMEOUT_SECONDS=180', hook_source)
        self.assertEqual(outer, 240)
        self.assertGreaterEqual(outer, calls[0] + 60)

    def test_same_task_name_in_different_sessions_gets_unique_physical_names(self):
        calls = []

        def fake(cmd, **kw):
            requested = cmd[cmd.index('--name') + 1]
            calls.append(requested)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True,'agent':requested,'task_id':'relay-test','task_sha256':'c'*64}), '')

        for sid, turn, tool_use in (('session-a', 'turn-a', 'tool-a'),
                                    ('session-b', 'turn-b', 'tool-b')):
            invoke(self.root, {
                'session_id': sid, 'turn_id': turn, 'tool_use_id': tool_use,
                'tool_name': 'spawn_agent',
                'cwd':str(Path.cwd().resolve()),
                'tool_input': {'task_name': 'worker','agent_type':'worker',
                               'message':json.dumps(package(Path.cwd(),sid),ensure_ascii=False)},
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
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True,'agent':agent,'task_id':task_id,'task_sha256':'d'*64}), '')

        with patch('os.replace', side_effect=OSError('映射保存失败')):
            reason = self.spawn('session-a', agent, fake)
        self.assertEqual(calls, ['dispatch-task'])
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
                    return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True,'agent':agent,'task_id':invalid_task_id,'task_sha256':'e'*64}), '')

                reason = self.spawn('session-invalid', agent, fake)
                self.assertEqual(calls, ['dispatch-task'])
                self.assertIn('RELAY_REDIRECT_PARTIAL', reason)
                self.assertIn(agent, reason)
                self.assertIn('task_id', reason)
                self.assertNotIn('RELAY_REDIRECT_OK', reason)
                self.assertFalse(self.map.exists())

    def test_safe_name_uses_top_level_event_identity(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd[cmd.index('--name') + 1])
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'accepted': True,'agent':calls[-1],'task_id':'relay-test','task_sha256':'f'*64}), '')

        for sid in ('session-a', 'session-b'):
            invoke(self.root, {
                'session_id': sid,
                'turn_id': 'same-turn',
                'tool_use_id': 'same-tool',
                'cwd':str(Path.cwd().resolve()),
                'tool_name': 'spawn_agent',
                'tool_input': {
                    'task_name': 'worker','agent_type':'worker',
                    'message':json.dumps(package(Path.cwd(),sid),ensure_ascii=False),
                },
            }, fake)

        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][len('codex-worker-'):len('codex-worker-') + 6],
                            calls[1][len('codex-worker-'):len('codex-worker-') + 6])
