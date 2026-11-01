"""Local-only bridge between the desktop UI and the existing SSH collector."""
import copy
import csv
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from collector import COLUMNS, PROFILES, collect, device_from_row, read_inventory, write_summary
from inventory import Inventory


class DesktopAPI:
    def __init__(self, state_dir, output_dir):
        self._state_dir = Path(state_dir)
        self._library = Inventory(self._state_dir / 'inventory.json')
        self._lock = threading.RLock()
        self._window = None
        self._credentials = {}
        self._revision = 0
        self._running = False
        self._cancel = threading.Event()
        self._records = {}
        self._trust = {}
        self._folder = None
        self._last = ''
        self._message = '项目已就绪，设置登录凭据后即可采集。'
        self._settings = dict(output=str(output_dir), workers=4, timeout=180, encoding='utf-8', auto_trust=True)
        self._completed = 0
        self._total = 0
        self._thread = None

    def call(self, action, payload=None):
        """The sole public bridge entry point; credentials never appear in snapshots."""
        payload = payload or {}
        try:
            with self._lock:
                if action == 'state':
                    return dict(ok=True, state=self._snapshot())
                handlers = {
                    'select': self._select, 'project': self._project, 'device': self._device,
                    'delete_devices': self._delete_devices, 'credentials': self._set_credentials,
                    'settings': self._set_settings, 'import_csv': self._import_csv,
                    'replace_devices': self._replace_devices, 'export_csv': self._export_csv,
                    'browse': self._browse, 'start': self._start, 'stop': self._stop,
                    'trust': self._answer_trust, 'open_results': self._open_results,
                }
                if action not in handlers:
                    raise ValueError('不支持的操作')
                if action not in ('stop', 'trust', 'open_results'):
                    if self._running:
                        raise ValueError('采集期间不能修改或切换项目，请先停止采集')
                    if payload.get('revision') != self._revision:
                        raise ValueError('界面数据已更新，请重新操作')
                value = handlers[action](payload)
                return dict(ok=True, state=self._snapshot(), value=value)
        except (ValueError, OSError, UnicodeError, csv.Error) as exc:
            return dict(ok=False, error=str(exc))
        except Exception:
            return dict(ok=False, error='操作未完成，请检查文件权限或重新打开程序。')

    def _snapshot(self):
        project = self._library.selected
        rows = []
        for i, device in enumerate(self._library.devices()):
            row = dict(asdict(device), id=i, status='pending', stage='待采集', logs=[], message='', file='', sha256='')
            row.update(copy.deepcopy(self._records.get(i, {})))
            rows.append(row)
        return dict(project=project, projects=[dict(name=k, count=len(v)) for k, v in self._library.data['projects'].items()],
                    devices=rows, platforms=list(PROFILES), revision=self._revision,
                    credentials=sorted(self._credentials.get(project, {})), settings=dict(self._settings),
                    running=self._running, stopping=self._cancel.is_set() and self._running,
                    total=self._total, completed=self._completed, message=self._message,
                    last=self._last, folder=str(self._folder or ''),
                    trust=[dict(id=k, host=v['host'], algorithm=v['algorithm'], fingerprint=v['fingerprint'])
                           for k, v in self._trust.items() if not v['event'].is_set()])

    def _reset(self):
        self._revision += 1
        self._records.clear()
        self._folder = None
        self._last = ''
        self._completed = self._total = 0
        self._message = '项目设备清单已自动保存。'

    def _select(self, p):
        self._library.select(p['name'])
        self._reset()

    def _project(self, p):
        old = self._library.selected
        action = p.get('action')
        if action == 'create':
            self._library.create(p['name'])
        elif action == 'rename':
            self._library.rename(p['name'])
            self._credentials[self._library.selected] = self._credentials.pop(old, {})
        elif action == 'delete':
            self._library.delete()
            self._credentials.pop(old, None)
        else:
            raise ValueError('项目操作无效')
        self._reset()

    def _device(self, p):
        devices = self._library.devices()
        device = device_from_row(p['device'])
        index = p.get('id')
        if index is None:
            devices.append(device)
        elif isinstance(index, int) and 0 <= index < len(devices):
            devices[index] = device
        else:
            raise ValueError('设备已不存在')
        self._library.save_devices(devices)
        if index is not None:
            self._records.pop(index, None)
        self._revision += 1
        self._message = '设备信息已保存。'

    def _delete_devices(self, p):
        devices = self._library.devices()
        ids = set(p['ids'])
        if not ids or any(not isinstance(i, int) or i < 0 or i >= len(devices) for i in ids):
            raise ValueError('请选择有效设备')
        kept = [(i, d) for i, d in enumerate(devices) if i not in ids]
        self._library.save_devices([d for _, d in kept])
        self._records = {new: self._records[old] for new, (old, _) in enumerate(kept) if old in self._records}
        self._revision += 1
        self._message = '所选设备已删除，已有采集文件保留。'

    def _set_credentials(self, p):
        group, username, password = p.get('group', '').strip(), p.get('username', '').strip(), p.get('password', '')
        if not group or not username or not password:
            raise ValueError('凭据组、用户名和密码均须填写')
        self._credentials.setdefault(self._library.selected, {})[group] = (username, password)
        self._message = f'凭据组 {group} 已设置，仅本次会话有效。'

    def _set_settings(self, p):
        values = p['settings']
        workers, timeout = int(values['workers']), int(values['timeout'])
        if workers not in (1, 2, 4, 8) or not 15 <= timeout <= 900:
            raise ValueError('并发数须为 1/2/4/8，超时须为 15–900 秒')
        if values['encoding'] not in ('utf-8', 'gb18030') or not str(values['output']).strip():
            raise ValueError('请选择有效编码和保存目录')
        if not isinstance(values['auto_trust'], bool):
            raise ValueError('指纹信任选项无效')
        self._settings = dict(output=str(values['output']).strip(), workers=workers, timeout=timeout,
                              encoding=values['encoding'], auto_trust=values['auto_trust'])
        self._message = '采集设置已应用。'

    def _import_csv(self, p):
        import webview
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=('CSV (*.csv)',))
        if not paths:
            return None
        return [asdict(d) for d in read_inventory(paths[0])]

    def _replace_devices(self, p):
        self._library.save_devices([device_from_row(row) for row in p['devices']])
        self._reset()

    def _export_csv(self, p):
        import webview
        paths = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename='devices.csv',
                                                file_types=('CSV (*.csv)',))
        if not paths:
            return None
        filename = paths if isinstance(paths, str) else paths[0]
        if p.get('template'):
            rows = [dict(name=f'DEVICE-{i}', host=f'192.0.2.{i}', platform=platform, port=22, credential='default')
                    for i, platform in enumerate(PROFILES, 1)]
        else:
            rows = [asdict(d) for d in self._library.devices()]
        with open(filename, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v
                                 for k, v in row.items()})
        return filename

    def _browse(self, p):
        import webview
        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return paths[0] if paths else None

    def _open_results(self, p):
        folder = self._folder or Path(self._settings['output'])
        if not folder.is_dir():
            raise ValueError('结果目录尚不存在，请先完成一次采集')
        os.startfile(str(folder.resolve()))

    def _start(self, p):
        jobs = [(i, d) for i, d in enumerate(self._library.devices())
                if not p.get('retry') or self._records.get(i, {}).get('status') in ('failed', 'cancelled')]
        if not jobs:
            raise ValueError('没有可采集的设备')
        credentials = self._credentials.get(self._library.selected, {})
        missing = sorted({d.credential for _, d in jobs} - set(credentials))
        if missing:
            raise ValueError('请先设置登录凭据组：' + '、'.join(missing))
        # Recheck the on-disk inventory before using a potentially stale window.
        self._library.save_devices(self._library.devices())
        folder = Path(self._settings['output']) / self._library.selected / (
            ('retry_' if p.get('retry') else '') + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        folder.mkdir(parents=True, exist_ok=False)
        if not p.get('retry'):
            self._records.clear()
        for i, _ in jobs:
            self._records[i] = dict(status='pending', stage='排队中', logs=[], message='', file='', sha256='')
        self._folder = folder
        self._cancel.clear()
        self._running = True
        self._total, self._completed = len(jobs), 0
        self._revision += 1
        self._message = '采集已开始。'
        self._thread = threading.Thread(target=self._batch, args=(jobs, dict(credentials), folder, dict(self._settings)), daemon=True)
        self._thread.start()

    def _stop(self, p):
        self._cancel.set()
        self._message = '正在停止；连接与认证阶段需等待网络超时。'
        for request in self._trust.values():
            request['accepted'] = False
            request['event'].set()

    def _confirm_key(self, host, algorithm, fingerprint):
        key = uuid.uuid4().hex
        request = dict(host=host, algorithm=algorithm, fingerprint=fingerprint, accepted=False, event=threading.Event())
        with self._lock:
            self._trust[key] = request
        try:
            while not request['event'].wait(.1):
                if self._cancel.is_set():
                    return False
            return request['accepted'] and not self._cancel.is_set()
        finally:
            with self._lock:
                self._trust.pop(key, None)

    def _answer_trust(self, p):
        request = self._trust.get(p['id'])
        if request:
            request['accepted'] = p.get('accepted') is True
            request['event'].set()

    def _stage(self, i, value):
        with self._lock:
            record = self._records[i]
            record.update(status='working', stage=value)
            record['logs'].append(datetime.now().strftime('%H:%M:%S') + '  ' + value)
            record['logs'] = record['logs'][-100:]

    def _batch(self, jobs, credentials, folder, settings):
        results = {}
        errors = []
        confirm = (lambda *args: True) if settings['auto_trust'] else self._confirm_key
        def task(i, device):
            if self._cancel.is_set():
                return dict(name=device.name, host=device.host, platform=device.platform, status='失败',
                            file='', message='已取消，尚未建立连接', sha256='')
            return collect(device, credentials[device.credential], folder, i + 1,
                           self._state_dir / 'known_hosts', confirm, self._cancel,
                           settings['timeout'], settings['encoding'], on_status=lambda value: self._stage(i, value))
        try:
            with ThreadPoolExecutor(max_workers=settings['workers']) as pool:
                futures = {pool.submit(task, i, device): (i, device) for i, device in jobs}
                for future in as_completed(futures):
                    i, device = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = dict(name=device.name, host=device.host, platform=device.platform, status='失败',
                                      file='', message=f'采集任务异常：{type(exc).__name__}', sha256='')
                    results[i] = result
                    with self._lock:
                        record = self._records[i]
                        status = 'success' if result['status'] == '成功' else ('cancelled' if self._cancel.is_set() else 'failed')
                        record.update(status=status, stage=result['status'], message=result['message'],
                                      file=str(folder / result['file']) if result['file'] else '', sha256=result['sha256'])
                        record['logs'].append(datetime.now().strftime('%H:%M:%S') + '  ' + result['message'])
                        self._completed += 1
                    try:
                        write_summary(folder, [results[k] for k in sorted(results)])
                    except OSError:
                        if not errors:
                            errors.append('汇总文件写入失败，请检查目录权限；单台结果已保留。')
        finally:
            credentials.clear()
            with self._lock:
                for i, _ in jobs:
                    if self._records[i]['status'] in ('pending', 'working'):
                        self._records[i].update(status='failed', message='任务中断，请重试', stage='任务中断')
                successes = sum(r['status'] == '成功' for r in results.values())
                self._last = datetime.now().strftime('%H:%M')
                self._message = '；'.join(errors) or f'本次完成 {len(results)} / {len(jobs)} 台，成功 {successes} 台。'
                self._running = False

    def _close(self):
        with self._lock:
            self._stop({})
            self._credentials.clear()
