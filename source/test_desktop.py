import csv
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from desktop_api import DesktopAPI
from test_collector import FakeDevice


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.api = DesktopAPI(self.root / 'state', self.root / 'backups')

    def tearDown(self):
        self.api._close()
        if self.api._thread:
            self.api._thread.join(10)
        self.temp.cleanup()

    def call(self, operation, **payload):
        result = self.api.call(operation, dict(revision=self.api._revision, **payload))
        self.assertTrue(result['ok'], result)
        return result

    def add(self, name='CE-01', host='192.0.2.1', **extra):
        return self.call('device', device=dict(name=name, host=host, platform='Huawei CE', **extra))

    def credential(self):
        self.call('credentials', group='default', username='test', password='test-only')

    def wait(self):
        self.api._thread.join(10)
        self.assertFalse(self.api._running)
        return self.call('state')['state']

    @staticmethod
    def result(device, success=True):
        return dict(name=device.name, host=device.host, platform=device.platform,
                    status='成功' if success else '失败', file='', message='local test', sha256='')

    def test_crud_and_reload(self):
        self.add()
        self.call('device', id=0, device=dict(name='USG', host='192.0.2.1', platform='Huawei USG'))
        restored = DesktopAPI(self.root / 'state', self.root / 'backups')
        self.assertEqual(restored._snapshot()['devices'][0]['platform'], 'Huawei USG')
        self.call('delete_devices', ids=[0])
        self.assertEqual(self.call('state')['state']['devices'], [])

    def test_invalid_duplicate_and_stale_edit_rejected(self):
        revision = self.api._revision
        self.add()
        result = self.api.call('device', dict(revision=revision, device=dict(name='X', host='192.0.2.2', platform='Huawei CE')))
        self.assertFalse(result['ok'])
        result = self.api.call('device', dict(revision=self.api._revision, device=dict(name='X', host='192.0.2.1', platform='Huawei CE')))
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.api._snapshot()['devices']), 1)

    def test_credentials_isolated_hidden_and_rename_preserves(self):
        self.credential()
        self.call('project', action='rename', name='Project A')
        self.assertEqual(self.api._snapshot()['credentials'], ['default'])
        self.call('project', action='create', name='Project B')
        self.assertEqual(self.api._snapshot()['credentials'], [])
        self.call('select', name='Project A')
        self.assertEqual(self.api._snapshot()['credentials'], ['default'])
        self.assertNotIn('test-only', json.dumps(self.api._snapshot()))
        self.assertNotIn('test-only', (self.root / 'state/inventory.json').read_text(encoding='utf-8'))

    def test_missing_credentials_blocks_before_creating_output(self):
        self.add()
        result = self.api.call('start', dict(revision=self.api._revision))
        self.assertFalse(result['ok'])
        self.assertFalse((self.root / 'backups').exists())

    def test_real_local_ssh_chain_saves_config_summary_and_diagnostics(self):
        server = FakeDevice('Huawei CE')
        try:
            self.add(host='127.0.0.1', port=server.port)
            self.credential()
            self.call('start')
            state = self.wait()
            self.assertEqual(state['devices'][0]['status'], 'success')
            config = Path(state['devices'][0]['file'])
            self.assertIn('sysname', config.read_text(encoding='utf-8'))
            self.assertTrue((config.parent / 'results.csv').exists())
            self.assertEqual(len(list(config.parent.glob('*.ssh.jsonl'))), 1)
            self.assertEqual(config.parent.parent.name, '默认项目')
        finally:
            server.close()

    def test_retry_only_failed_keeps_success_and_original_folder(self):
        self.add('ONE', '192.0.2.1'); self.add('TWO', '192.0.2.2'); self.credential()
        with patch('desktop_api.collect', side_effect=lambda d, *a, **k: self.result(d, d.name == 'ONE')):
            self.call('start'); first = self.wait()
        old_folder = first['folder']
        record = dict(first['devices'][0])
        with patch('desktop_api.collect', side_effect=lambda d, *a, **k: self.result(d)) as collector:
            self.call('start', retry=True); last = self.wait()
            self.assertEqual(collector.call_count, 1)
            self.assertEqual(collector.call_args.args[0].name, 'TWO')
        self.assertEqual(record, last['devices'][0])
        self.assertNotEqual(old_folder, last['folder'])
        self.assertTrue(Path(last['folder']).name.startswith('retry_'))
        self.assertTrue((Path(old_folder) / 'results.csv').exists())

    def test_running_blocks_edits_and_stop_cancels_pending_jobs(self):
        self.add('ONE'); self.add('TWO', '192.0.2.2'); self.credential()
        self.api._settings['workers'] = 1
        entered = threading.Event()
        def task(d, *args, **kwargs):
            entered.set(); self.api._cancel.wait(3)
            return self.result(d, False)
        with patch('desktop_api.collect', side_effect=task):
            self.call('start'); self.assertTrue(entered.wait(2))
            for action, values in [('select', {'name': '默认项目'}), ('device', {'device': {}}),
                                   ('credentials', {'group': 'new', 'username': 'x', 'password': 'y'})]:
                self.assertFalse(self.api.call(action, dict(revision=self.api._revision, **values))['ok'])
            self.call('stop'); state = self.wait()
        self.assertTrue(all(d['status'] == 'cancelled' for d in state['devices']))

    def test_manual_host_key_confirmation(self):
        self.add(); self.credential(); self.api._settings['auto_trust'] = False
        def task(d, credentials, folder, index, known_hosts, confirm, *args, **kwargs):
            return self.result(d, confirm('local', 'ssh-rsa', 'SHA256:example'))
        with patch('desktop_api.collect', side_effect=task):
            self.call('start')
            deadline = time.monotonic() + 3
            while not self.api._snapshot()['trust'] and time.monotonic() < deadline:
                time.sleep(.01)
            request = self.call('state')['state']['trust'][0]
            self.call('trust', id=request['id'], accepted=True)
            self.assertEqual(self.wait()['devices'][0]['status'], 'success')

    def test_stop_releases_host_key_wait(self):
        self.add(); self.credential(); self.api._settings['auto_trust'] = False
        def task(d, credentials, folder, index, known_hosts, confirm, *args, **kwargs):
            return self.result(d, confirm('local', 'ssh-rsa', 'SHA256:example'))
        with patch('desktop_api.collect', side_effect=task):
            self.call('start')
            time.sleep(.1)
            self.call('stop')
            self.assertEqual(self.wait()['devices'][0]['status'], 'cancelled')

    def test_task_exception_does_not_lose_other_results(self):
        self.add('ONE'); self.add('TWO', '192.0.2.2'); self.credential()
        def task(d, *args, **kwargs):
            if d.name == 'ONE':
                raise RuntimeError('do not expose credential text')
            return self.result(d)
        with patch('desktop_api.collect', side_effect=task):
            self.call('start'); state = self.wait()
        self.assertEqual([d['status'] for d in state['devices']], ['failed', 'success'])
        self.assertNotIn('credential text', json.dumps(state))
        self.assertEqual(state['completed'], 2)

    def test_summary_failure_does_not_discard_device_results(self):
        self.add(); self.credential()
        with patch('desktop_api.collect', side_effect=lambda d, *a, **k: self.result(d)), \
             patch('desktop_api.write_summary', side_effect=OSError('disk')):
            self.call('start'); state = self.wait()
        self.assertEqual(state['devices'][0]['status'], 'success')
        self.assertIn('汇总', state['message'])

    def test_failed_write_preserves_state(self):
        self.add(); before = self.api._snapshot()
        with patch.object(self.api._library, 'commit', side_effect=OSError('disk')):
            result = self.api.call('delete_devices', dict(revision=self.api._revision, ids=[0]))
        self.assertFalse(result['ok'])
        self.assertEqual(self.api._snapshot(), before)

    def test_invalid_settings_rejected(self):
        for key, value in [('workers', 0), ('timeout', 1), ('encoding', 'bad'), ('output', ''), ('auto_trust', 'yes')]:
            settings = dict(self.api._settings); settings[key] = value
            self.assertFalse(self.api.call('settings', dict(revision=self.api._revision, settings=settings))['ok'])

    def test_csv_import_preview_cancel_and_apply(self):
        filename = self.root / 'devices.csv'
        filename.write_text('name,host,platform,port,credential\nNEW,192.0.2.8,Huawei CE,22,default\n', encoding='utf-8')
        self.api._window = Mock()
        self.api._window.create_file_dialog.return_value = [str(filename)]
        self.add()
        rows = self.call('import_csv')['value']
        self.assertEqual(self.api._snapshot()['devices'][0]['name'], 'CE-01')
        self.call('replace_devices', devices=rows)
        self.assertEqual(self.api._snapshot()['devices'][0]['name'], 'NEW')

    def test_export_formula_safety_and_template(self):
        self.add('=test')
        filename = self.root / 'export.csv'
        self.api._window = Mock()
        self.api._window.create_file_dialog.return_value = [str(filename)]
        self.call('export_csv')
        with filename.open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(next(csv.DictReader(stream))['name'], "'=test")
        self.call('export_csv', template=True)
        self.assertIn('Huawei USG', filename.read_text(encoding='utf-8-sig'))

    def test_stale_library_blocks_collection(self):
        self.add(); self.credential()
        other = DesktopAPI(self.root / 'state', self.root / 'out')
        other.call('project', dict(revision=0, action='rename', name='Other'))
        self.assertFalse(self.api.call('start', dict(revision=self.api._revision))['ok'])


if __name__ == '__main__':
    unittest.main()
