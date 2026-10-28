import json
import subprocess
import unittest
from unittest.mock import patch
import test_event_regression
from test_relay import m


class ReliabilityRegressionTests(unittest.TestCase):
    setUp = test_event_regression.EventRegressionTests.setUp

    def seed(self):
        m.save_registry({'agents': {'worker': {'lifecycle': 'BUSY', 'created_at': 1,
                                              'pane_id': 'p', 'current_task_id': 'new'}}})

    def seed_removed_orphan(self, task_id='relay-f134263f88', lock_task_id=None):
        m.save_registry({'agents': {'reviewer-relay-last-pass-0927': {
            'name':'reviewer-relay-last-pass-0927','lifecycle':'REMOVED','created_at':1,
            'tab_id':'tab-orphan','pane_id':'pane-orphan','current_task_id':task_id,
            'operation':'stale-operation','operation_kind':'finalize'}}})
        m.write_json(m.TASKS/f'{task_id}.json', {'task_id':task_id,
            'agent':'reviewer-relay-last-pass-0927','phase':'RUNNING','final_state':None,
            'evidence':'保留的审查证据'})
        (m.LOCKS/'reviewer-relay-last-pass-0927.lock').write_text(lock_task_id or task_id)

    def test_removed_legacy_orphan_is_recovered_only_after_authoritative_absence(self):
        task_id='relay-f134263f88'
        self.seed_removed_orphan(task_id)
        with patch.object(m,'herdr_json',return_value={'result':{'agents':[]}}), \
             patch.object(m,'agent_status_obj',return_value={'error':{'code':'agent_not_found'}}) as get_status:
            result=m.sync()
        entry=result['agents']['reviewer-relay-last-pass-0927']
        task=m.read_json(m.TASKS/f'{task_id}.json',{})
        self.assertEqual(entry['lifecycle'],'REMOVED')
        self.assertIsNone(entry.get('current_task_id'))
        self.assertNotIn('operation',entry)
        self.assertNotIn('operation_kind',entry)
        self.assertEqual(task['final_state'],'HUMAN_REVIEW')
        self.assertEqual(task['phase'],'HUMAN_REVIEW')
        self.assertEqual(task['evidence'],'保留的审查证据')
        self.assertFalse((m.LOCKS/'reviewer-relay-last-pass-0927.lock').exists())
        get_status.assert_called_once_with('reviewer-relay-last-pass-0927')

    def test_removed_legacy_orphan_cleans_owned_lock_without_rewriting_final_task(self):
        task_id='relay-f134263f88'
        self.seed_removed_orphan(task_id)
        task_path=m.TASKS/f'{task_id}.json'
        final_task={'task_id':task_id,'agent':'reviewer-relay-last-pass-0927',
                    'phase':'DONE','final_state':'DONE','evidence':'已结算的原始证据'}
        m.write_json(task_path,final_task)
        with patch.object(m,'herdr_json',return_value={'result':{'agents':[]}}), \
             patch.object(m,'agent_status_obj',return_value={'error':{'code':'agent_not_found'}}):
            result=m.sync()
        entry=result['agents']['reviewer-relay-last-pass-0927']
        task=m.read_json(task_path,{})
        self.assertEqual(entry['lifecycle'],'REMOVED')
        self.assertIsNone(entry.get('current_task_id'))
        self.assertNotIn('operation',entry)
        self.assertNotIn('operation_kind',entry)
        self.assertFalse((m.LOCKS/'reviewer-relay-last-pass-0927.lock').exists())
        self.assertEqual(task['phase'],'DONE')
        self.assertEqual(task['final_state'],'DONE')
        self.assertEqual(task['evidence'],'已结算的原始证据')

    def test_removed_legacy_orphan_keeps_state_when_task_or_lock_mismatches(self):
        for mismatch in ('task','lock'):
            with self.subTest(mismatch=mismatch):
                task_id='relay-f134263f88'
                self.seed_removed_orphan(task_id, 'relay-other' if mismatch=='lock' else None)
                if mismatch=='task':
                    m.write_json(m.TASKS/f'{task_id}.json', {'task_id':'relay-other',
                        'agent':'reviewer-relay-last-pass-0927','phase':'RUNNING','evidence':'原证据'})
                lock=m.LOCKS/'reviewer-relay-last-pass-0927.lock'
                old_lock=lock.read_text()
                with patch.object(m,'herdr_json',return_value={'result':{'agents':[]}}), \
                     patch.object(m,'agent_status_obj',return_value={'error':{'code':'agent_not_found'}}):
                    result=m.sync()
                entry=result['agents']['reviewer-relay-last-pass-0927']
                task=m.read_json(m.TASKS/f'{task_id}.json',{})
                self.assertEqual(entry.get('current_task_id'),task_id)
                self.assertEqual(entry.get('operation'),'stale-operation')
                self.assertEqual(task['phase'],'RUNNING')
                self.assertEqual(lock.read_text(),old_lock)

    def test_removed_legacy_orphan_keeps_state_when_agent_lookup_is_transient(self):
        task_id='relay-f134263f88'
        self.seed_removed_orphan(task_id)
        with patch.object(m,'herdr_json',return_value={'result':{'agents':[]}}), \
             patch.object(m,'agent_status_obj',return_value=None):
            result=m.sync()
        entry=result['agents']['reviewer-relay-last-pass-0927']
        self.assertEqual(entry.get('current_task_id'),task_id)
        self.assertTrue((m.LOCKS/'reviewer-relay-last-pass-0927.lock').exists())
        self.assertEqual(m.read_json(m.TASKS/f'{task_id}.json',{})['phase'],'RUNNING')

    def test_removed_legacy_orphan_is_not_recovered_if_instance_is_in_valid_list(self):
        task_id='relay-f134263f88'
        self.seed_removed_orphan(task_id)
        snapshot={'result':{'agents':[{'name':'reviewer-relay-last-pass-0927',
            'tab_id':'tab-orphan','pane_id':'pane-orphan','agent_status':'working'}]}}
        with patch.object(m,'herdr_json',return_value=snapshot), \
             patch.object(m,'agent_status_obj') as get_status:
            result=m.sync()
        self.assertEqual(result['agents']['reviewer-relay-last-pass-0927'].get('current_task_id'),task_id)
        self.assertTrue((m.LOCKS/'reviewer-relay-last-pass-0927.lock').exists())
        get_status.assert_not_called()

    def test_removed_legacy_orphan_is_not_recovered_from_invalid_list(self):
        task_id='relay-f134263f88'
        self.seed_removed_orphan(task_id)
        with patch.object(m,'herdr_json',return_value={'result':{'agents':None}}), \
             patch.object(m,'agent_status_obj') as get_status:
            result=m.sync()
        entry=result['agents']['reviewer-relay-last-pass-0927']
        self.assertEqual(entry.get('current_task_id'),task_id)
        self.assertTrue((m.LOCKS/'reviewer-relay-last-pass-0927.lock').exists())
        self.assertEqual(m.read_json(m.TASKS/f'{task_id}.json',{})['phase'],'RUNNING')
        get_status.assert_not_called()

    def test_bad_registry_is_preserved(self):
        m.ensure()
        for raw in ('{', '[]', '{"agents":[]}', '{"agents":{"x":null}}'):
            m.REGISTRY.write_text(raw)
            with self.assertRaises((ValueError, RuntimeError)):
                m.registry()
            self.assertEqual(m.REGISTRY.read_text(), raw)
        with patch.object(m.Path, 'read_text', side_effect=PermissionError('禁止读取')):
            with self.assertRaises(PermissionError): m.registry()

    def test_transient_get_failure_never_means_removed(self):
        self.seed()
        with patch.object(m, 'herdr_json', side_effect=[{'result': {'agents': []}}, subprocess.TimeoutExpired('herdr', 15)]):
            self.assertEqual(m.sync()['agents']['worker']['lifecycle'], 'BUSY')

    def test_invalid_agent_list_shapes_are_not_deletion_evidence(self):
        """不可信的列表不能作为删除证据，否则会误删忙碌代理及其任务状态。"""
        for result in ({'agents': None}, {}):
            with self.subTest(result=result):
                self.seed()
                m.write_json(m.TASKS / 'new.json', {'task_id': 'new', 'phase': 'RUNNING',
                                                    'dispatch_mode': 'opaque_async'})
                (m.LOCKS / 'worker.lock').parent.mkdir(parents=True, exist_ok=True)
                (m.LOCKS / 'worker.lock').write_text('new')
                with patch.object(m, 'herdr_json', return_value={'result': result}), \
                        patch.object(m, 'agent_status_obj', return_value={'error': {'code': 'agent_not_found'}}) as get_status:
                    output = m.sync()
                self.assertEqual(output['agents']['worker']['lifecycle'], 'BUSY')
                self.assertEqual(output['agents']['worker']['current_task_id'], 'new')
                self.assertEqual(m.read_json(m.TASKS / 'new.json', {})['phase'], 'RUNNING')
                self.assertTrue((m.LOCKS / 'worker.lock').exists())
                get_status.assert_not_called()

    def test_live_agents_accepts_only_explicit_empty_lists(self):
        """合法空列表表示确实没有代理，不能与异常响应形状混淆。"""
        self.assertEqual(m.live_agents({'result': []}), [])
        self.assertEqual(m.live_agents({'result': {'agents': []}}), [])
        unnamed = {'agent': 'codex', 'tab_id': 'root-tab', 'pane_id': 'root-pane',
                   'agent_status': 'working'}
        self.assertEqual(m.live_agents({'result': [unnamed]}), [unnamed])
        for snapshot in ({'result': {'agents': None}}, {'result': {}}):
            with self.subTest(snapshot=snapshot):
                with self.assertRaisesRegex((ValueError, RuntimeError), 'agent-list'):
                    m.live_agents(snapshot)

    def test_invalid_agent_list_elements_are_not_deletion_evidence(self):
        """列表中的坏元素不能被过滤成空列表，否则忙碌代理会被误删。"""
        invalid_lists = ([None], [{}], [{'pane_id': 'p'}], [{'name': ''}], [{'name': '  '}],
                         [{'name': 'worker'}, {'name': 'worker'}])
        for agents in invalid_lists:
            for result in (agents, {'agents': agents}):
                with self.subTest(result=result):
                    self.seed()
                    m.write_json(m.TASKS / 'new.json', {'task_id': 'new', 'phase': 'RUNNING'})
                    (m.LOCKS / 'worker.lock').parent.mkdir(parents=True, exist_ok=True)
                    (m.LOCKS / 'worker.lock').write_text('new')
                    with patch.object(m, 'herdr_json', return_value={'result': result}), \
                            patch.object(m, 'agent_status_obj', return_value={'error': {'code': 'agent_not_found'}}) as get_status:
                        output = m.sync()
                    self.assertEqual(output['agents']['worker']['lifecycle'], 'BUSY')
                    self.assertEqual(output['agents']['worker']['current_task_id'], 'new')
                    self.assertEqual(m.read_json(m.TASKS / 'new.json', {})['phase'], 'RUNNING')
                    self.assertTrue((m.LOCKS / 'worker.lock').exists())
                    get_status.assert_not_called()

    def test_confirmed_absence_and_startup_grace(self):
        self.seed()
        missing = {'error': {'code': 'agent_not_found'}, 'id': 'cli:agent:get'}
        def external(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0 if cmd[-1] == 'list' else 1,
                json.dumps({'result': {'agents': []}}) if cmd[-1] == 'list' else '',
                '' if cmd[-1] == 'list' else json.dumps(missing))
        with patch.object(m, 'run', side_effect=external), patch.object(m, 'now', return_value=2):
            self.assertEqual(m.sync()['agents']['worker']['lifecycle'], 'BUSY')
        with patch.object(m, 'run', side_effect=external):
            self.assertEqual(m.sync()['agents']['worker']['lifecycle'], 'REMOVED')

    def test_confirmed_orphan_moves_unsettled_task_to_human_review_and_releases_owned_lock(self):
        self.seed()
        task_id='relay-orphan'
        with m.registry_transaction() as r:
            r['agents']['worker'].update(current_task_id=task_id,operation='stale-token',operation_kind='dispatch')
        evidence={'task_id':task_id,'agent':'worker','phase':'RUNNING','final_state':None,
                  'dispatch_mode':'opaque_async','evidence':'保留此证据'}
        m.write_json(m.TASKS/f'{task_id}.json', evidence)
        (m.LOCKS/'worker.lock').write_text(task_id)
        missing={'error':{'code':'agent_not_found'}}
        with patch.object(m,'herdr_json',return_value={'result':{'agents':[]}}), \
             patch.object(m,'agent_status_obj',return_value=missing):
            result=m.sync()
        rec=result['agents']['worker']
        task=m.read_json(m.TASKS/f'{task_id}.json',{})
        self.assertEqual(rec['lifecycle'],'REMOVED')
        self.assertIsNone(rec.get('current_task_id'))
        self.assertNotIn('operation',rec)
        self.assertNotIn('operation_kind',rec)
        self.assertEqual(task['final_state'],'HUMAN_REVIEW')
        self.assertEqual(task['phase'],'HUMAN_REVIEW')
        self.assertEqual(task['evidence'],'保留此证据')
        self.assertFalse((m.LOCKS/'worker.lock').exists())

    def test_confirmed_orphan_does_not_touch_mismatched_task_or_lock(self):
        self.seed()
        task_id='relay-orphan'
        with m.registry_transaction() as r:
            r['agents']['worker'].update(current_task_id=task_id)
        m.write_json(m.TASKS/f'{task_id}.json', {'task_id':'relay-other','agent':'worker','phase':'RUNNING'})
        (m.LOCKS/'worker.lock').write_text('relay-other')
        with patch.object(m,'herdr_json',return_value={'result':{'agents':[]}}), \
             patch.object(m,'agent_status_obj',return_value={'error':{'code':'agent_not_found'}}):
            result=m.sync()
        self.assertEqual(result['agents']['worker']['lifecycle'],'REMOVED')
        self.assertIsNone(result['agents']['worker'].get('current_task_id'))
        self.assertEqual((m.LOCKS/'worker.lock').read_text(),'relay-other')
        self.assertEqual(m.read_json(m.TASKS/f'{task_id}.json',{})['phase'],'RUNNING')

    def test_confirmed_orphan_keeps_unowned_lock_even_when_task_matches(self):
        self.seed()
        task_id='relay-orphan'
        with m.registry_transaction() as r:
            r['agents']['worker'].update(current_task_id=task_id)
        m.write_json(m.TASKS/f'{task_id}.json', {'task_id':task_id,'agent':'worker','phase':'RUNNING'})
        (m.LOCKS/'worker.lock').write_text('relay-someone-elses-task')
        with patch.object(m,'herdr_json',return_value={'result':{'agents':[]}}), \
             patch.object(m,'agent_status_obj',return_value={'error':{'code':'agent_not_found'}}):
            m.sync()
        self.assertEqual((m.LOCKS/'worker.lock').read_text(),'relay-someone-elses-task')
        self.assertEqual(m.read_json(m.TASKS/f'{task_id}.json',{})['phase'],'RUNNING')

    def test_stderr_transport_errors_do_not_confirm_absence(self):
        for stderr in ('权限不足', '{"error":{"code":"transport_error"}}'):
            with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', stderr)) as command:
                with self.assertRaises(RuntimeError): m.herdr_json(['agent', 'get', 'worker'])
                self.assertTrue(command.call_args.kwargs['capture_output'])

    def test_cache_is_bound_to_task_instance_and_runtime(self):
        self.seed()
        with patch.object(m, 'agent_status_obj', return_value={'result': {'agent_status': 'done'}}):
            m.sensor('worker')
        for changes in ({'current_task_id': 'next'}, {'pane_id': 'replacement'}, {}):
            with m.registry_transaction() as r: r['agents']['worker'].update(changes)
            with patch.object(m, 'agent_status_obj', return_value={'result': {'agent_status': 'working'}}), patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), patch.object(m, 'jev_call', return_value={'semantic_status': 'PROGRESSING'}):
                self.assertEqual(m.sensor('worker')['semantic_status'], 'PROGRESSING')

    def test_daemon_reuses_snapshot_and_continues_after_one_failure(self):
        self.seed()
        with m.registry_transaction() as r:
            r['agents']['other'] = {'lifecycle': 'BUSY'}
        snapshot = {'result': {'agents': [{'name': 'worker', 'pane_id': 'p', 'agent_status': 'working'}, {'name': 'other', 'agent_status': 'done'}]}}
        with patch.object(m, 'herdr_json', return_value=snapshot) as herdr, patch.object(m, 'sensor', side_effect=[RuntimeError('单个故障'), {}]) as sensor, patch.object(m.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): m.daemon()
        self.assertEqual(herdr.call_count, 1)
        self.assertEqual(sensor.call_count, 2)
        self.assertIn('observed', sensor.call_args.kwargs)
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'BUSY')

    def test_sync_does_not_settle_old_task_after_same_name_instance_replacement(self):
        self.seed()
        with m.registry_transaction() as r:
            r['agents']['worker'].update({'tab_id': 'tab-old', 'pane_id': 'pane-old'})
        task_id = 'new'
        m.write_json(m.TASKS / f'{task_id}.json', {
            'task_id': task_id,
            'dispatch_mode': 'opaque_async',
            'dispatch_confirmed': True,
            'phase': 'RUNNING',
            'observed_active': True,
        })
        (m.LOCKS / 'worker.lock').parent.mkdir(parents=True, exist_ok=True)
        (m.LOCKS / 'worker.lock').write_text(task_id)
        snapshot = {'result': {'agents': [{
            'name': 'worker', 'tab_id': 'tab-new', 'pane_id': 'pane-new',
            'agent_status': 'done',
        }]}}
        result = m.sync(snapshot=snapshot)
        self.assertEqual(result['agents']['worker']['lifecycle'], 'BUSY')
        self.assertEqual(result['agents']['worker']['current_task_id'], task_id)
        self.assertTrue((m.LOCKS / 'worker.lock').exists())
        self.assertEqual(m.read_json(m.TASKS / f'{task_id}.json', {})['phase'], 'RUNNING')

    def test_daemon_accepts_list_result_and_only_sensors_matching_instance(self):
        self.seed()
        with m.registry_transaction() as r:
            r['agents']['worker'].update({'tab_id': 'tab-old', 'pane_id': 'pane-old'})
        snapshot = {'result': [{
            'name': 'worker', 'tab_id': 'tab-old', 'pane_id': 'pane-old',
            'agent_status': 'working',
        }]}
        with patch.object(m, 'herdr_json', return_value=snapshot) as herdr, \
                patch.object(m, 'sensor', side_effect=KeyboardInterrupt) as sensor, \
                patch.object(m.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                m.daemon()
        herdr.assert_called_once_with(['agent', 'list'], timeout=15)
        sensor.assert_called_once()
        self.assertEqual(sensor.call_args.kwargs['observed']['tab_id'], 'tab-old')

    def test_daemon_does_not_sensor_same_name_replacement(self):
        self.seed()
        with m.registry_transaction() as r:
            r['agents']['worker'].update({'tab_id': 'tab-old', 'pane_id': 'pane-old'})
        snapshot = {'result': [{'name': 'worker', 'tab_id': 'tab-new', 'pane_id': 'pane-new',
                                'agent_status': 'working'}]}
        with patch.object(m, 'herdr_json', return_value=snapshot), \
                patch.object(m, 'sensor') as sensor, \
                patch.object(m.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                m.daemon()
        sensor.assert_not_called()
