import multiprocessing
import subprocess
import unittest
from unittest.mock import patch

import test_event_regression
from test_relay import m


class ConcurrencyRegressionTests(unittest.TestCase):
    setUp = test_event_regression.EventRegressionTests.setUp

    def seed(self):
        m.save_registry({'agents': {'worker': {'lifecycle': 'IDLE'}}})

    def dispatch(self):
        with patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            return m.dispatch_opaque('worker', '任务')

    def snapshot(self, status):
        return {'result': {'agents': [{'name': 'worker', 'status': status}]}}

    def paused_sync(self, snapshot):
        ctx = multiprocessing.get_context('fork')
        entered, resume = ctx.Event(), ctx.Event()
        def child():
            def external(*args, **kwargs):
                entered.set()
                if not resume.wait(8):
                    raise RuntimeError('等待超时')
                return snapshot
            with patch.object(m, 'herdr_json', side_effect=external):
                m.sync()
        process = ctx.Process(target=child)
        process.start()
        self.addCleanup(lambda: process.kill() if process.is_alive() else None)
        self.assertTrue(entered.wait(3))
        return process, resume

    def finish(self, process, resume):
        resume.set()
        process.join(4)
        self.assertFalse(process.is_alive(), '外部调用不得持全局锁或死锁')
        self.assertEqual(process.exitcode, 0)

    def test_sync_preserves_dispatch_from_another_process(self):
        self.seed()
        process, resume = self.paused_sync(self.snapshot('done'))
        task = self.dispatch()['task_id']
        self.finish(process, resume)
        self.assertEqual(m.registry()['agents']['worker'].get('current_task_id'), task)

    def test_two_syncs_cannot_delete_new_task_lock(self):
        self.seed()
        self.dispatch()
        with patch.object(m, 'herdr_json', return_value=self.snapshot('working')):
            m.sync()
        process, resume = self.paused_sync(self.snapshot('done'))
        with patch.object(m, 'herdr_json', return_value=self.snapshot('done')):
            m.sync()
        task = self.dispatch()['task_id']
        self.finish(process, resume)
        self.assertTrue((m.LOCKS / 'worker.lock').exists(), '新任务锁被旧同步删除')
        self.assertEqual((m.LOCKS / 'worker.lock').read_text(), task)
        self.assertEqual(m.registry()['agents']['worker']['current_task_id'], task)

    def test_spawn_refuses_active_name_before_external_call(self):
        self.seed()
        with patch.object(m, 'herdr_json', return_value={'result': {'tab': {'tab_id': 't'}, 'root_pane': {'pane_id': 'p'}}}) as external:
            with self.assertRaises(SystemExit):
                m.spawn('worker', '/tmp', name='worker')
            external.assert_not_called()

    def test_delegate_cli_exit_status(self):
        self.seed()
        for phase in ('DONE', 'HUMAN_REVIEW', 'BLOCKED', 'DEEP_REVIEW'):
            with self.subTest(phase=phase), patch.object(m.sys, 'argv', ['relay', 'delegate', 'worker', '任务']), patch.object(m, 'delegate', return_value={'final_state': phase}), patch('builtins.print'):
                try:
                    m.main()
                    code = 0
                except SystemExit as error:
                    code = error.code
                self.assertEqual(code == 0, phase == 'DONE')

    def test_spawn_reservation_and_other_agent_progress_across_processes(self):
        ctx = multiprocessing.get_context('fork')
        entered, resume = ctx.Event(), ctx.Event()
        def child():
            def external(*args, **kwargs):
                entered.set()
                if not resume.wait(8): raise RuntimeError('等待超时')
                return {'result': {'tab': {'tab_id': 't'}, 'root_pane': {'pane_id': 'p'}}}
            with patch.object(m, 'herdr_json', side_effect=external):
                m.spawn('worker', '/tmp', name='worker')
        process = ctx.Process(target=child)
        process.start()
        self.addCleanup(lambda: process.kill() if process.is_alive() else None)
        self.assertTrue(entered.wait(3))
        with patch.object(m, 'herdr_json', return_value={'result': {'tab': {'tab_id': 't2'}, 'root_pane': {'pane_id': 'p2'}}}) as external:
            with self.assertRaises(SystemExit): m.spawn('worker', '/tmp', name='worker')
            external.assert_not_called()
            m.spawn('worker', '/tmp', name='other')
        self.finish(process, resume)
        self.assertEqual(set(m.registry()['agents']), {'worker', 'other'})
        self.assertTrue(all(rec['lifecycle'] == 'IDLE' for rec in m.registry()['agents'].values()))

    def test_delegate_process_exit_with_real_flow(self):
        ctx = multiprocessing.get_context('fork')
        for success in (True, False):
            self.seed()
            def child():
                marker = []
                def external(cmd, **kwargs):
                    if cmd[2] == 'prompt': marker.append(cmd[4].splitlines()[0])
                    return subprocess.CompletedProcess(cmd, 0 if success else 1, '\n'.join(marker), '发送失败')
                with patch.object(m.sys, 'argv', ['relay', 'delegate', 'worker', '任务']), patch.object(m, 'run', side_effect=external), patch.object(m, 'agent_status_obj', return_value={'result': {'status': 'done'}}), patch.object(m, 'review', return_value={'route': 'PASS'}), patch('builtins.print'):
                    m.main()
            process = ctx.Process(target=child)
            process.start()
            process.join(4)
            if process.is_alive():
                process.kill()
                process.join()
                self.fail('CLI 死锁')
            self.assertEqual(process.exitcode, 0 if success else 1)

    def test_release_can_retry_failed_close(self):
        m.save_registry({'agents': {'worker': {'lifecycle': 'IDLE', 'tab_id': 't'}}})
        with patch.object(m, 'herdr_json', side_effect=RuntimeError('关闭失败')):
            with self.assertRaises(RuntimeError): m.release('worker')
        with patch.object(m, 'herdr_json', return_value={}):
            self.assertEqual(m.release('worker')['lifecycle'], 'STOPPED')

    def test_release_cleans_failed_record_without_tab(self):
        m.save_registry({'agents': {'worker': {'lifecycle': 'FAILED', 'start_error': '创建失败'}}})
        with patch.object(m, 'herdr_json') as herdr:
            result = m.release('worker')
        self.assertEqual(result['lifecycle'], 'STOPPED')
        herdr.assert_not_called()
        self.assertNotEqual(m.registry()['agents']['worker']['lifecycle'], 'STOPPING')

    def test_cross_process_release_refuses_inflight_send_then_finishes_cleanly(self):
        m.save_registry({'agents': {'worker': {'lifecycle': 'IDLE', 'tab_id': 't'}}})
        ctx = multiprocessing.get_context('fork')
        entered, resume = ctx.Event(), ctx.Event()

        def child():
            def external(*args, **kwargs):
                entered.set()
                if not resume.wait(8):
                    raise RuntimeError('等待发送恢复超时')
                return subprocess.CompletedProcess([], 0, '', '')
            with patch.object(m, 'run', side_effect=external):
                m.send_opaque('worker', '消息')

        process = ctx.Process(target=child)
        process.start()
        self.addCleanup(lambda: process.kill() if process.is_alive() else None)
        self.assertTrue(entered.wait(3))
        with self.assertRaises(RuntimeError):
            m.release('worker')
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'IDLE')
        resume.set()
        process.join(4)
        self.assertFalse(process.is_alive(), '发送子进程未退出')
        self.assertEqual(process.exitcode, 0)
        self.assertNotIn('operation', m.registry()['agents']['worker'])
        with patch.object(m, 'herdr_json', return_value={}):
            self.assertEqual(m.release('worker')['lifecycle'], 'STOPPED')

    def test_cross_process_release_refuses_inflight_dispatch(self):
        m.save_registry({'agents': {'worker': {'lifecycle': 'IDLE', 'tab_id': 't'}}})
        ctx = multiprocessing.get_context('fork')
        entered, resume = ctx.Event(), ctx.Event()

        def child():
            def external(*args, **kwargs):
                entered.set()
                if not resume.wait(8):
                    raise RuntimeError('等待派发恢复超时')
                return subprocess.CompletedProcess([], 0, '', '')
            with patch.object(m, 'run', side_effect=external):
                m.dispatch_opaque('worker', '任务')

        process = ctx.Process(target=child)
        process.start()
        self.addCleanup(lambda: process.kill() if process.is_alive() else None)
        self.assertTrue(entered.wait(3))
        with self.assertRaises(RuntimeError):
            m.release('worker')
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'BUSY')
        resume.set()
        process.join(4)
        self.assertFalse(process.is_alive(), '派发子进程未退出')
        self.assertEqual(process.exitcode, 0)
        self.assertNotIn('operation', m.registry()['agents']['worker'])
        record = m.registry()['agents']['worker']
        self.assertEqual(record['lifecycle'], 'BUSY')
        task_id = record['current_task_id']
        task = m.read_json(m.TASKS / (task_id + '.json'), {})
        self.assertEqual(task['phase'], 'RUNNING')
        lock = m.LOCKS / 'worker.lock'
        self.assertTrue(lock.exists())
        self.assertEqual(lock.read_text(), task_id)
        with patch.object(m, 'herdr_json') as herdr:
            with self.assertRaises(RuntimeError):
                m.release('worker')
        herdr.assert_not_called()
        self.assertEqual(m.registry()['agents']['worker']['lifecycle'], 'BUSY')
        self.assertEqual(m.registry()['agents']['worker']['current_task_id'], task_id)
        self.assertEqual(m.read_json(m.TASKS / (task_id + '.json'), {})['phase'], 'RUNNING')
        self.assertTrue(lock.exists())
