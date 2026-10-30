"""Additions-only local-to-115 watches, with persistent baselines and upload sessions."""
import copy
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import threading
import time
import uuid

from .cloud import parse_uri, safe_name
from .cloud_cookie import Cookie115
from .cloud_providers import CloudError
from .core import inside, walk
from .upload115 import digest, UploadCancelled

TEMP_SUFFIXES = ('.part', '.partial', '.tmp', '.temp', '.crdownload', '.aria2', '.!qb', '.download', '.td', '.xltd')


def file_signature(path):
    st = Path(path).stat()
    return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, st.st_ctime_ns]


class UploadWatcher:
    def __init__(self, state):
        self.state = state
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.active_id = self.active_job = None
        self.cancelled = set()
        self.staging_root = state.data / 'manual-upload-cache'
        self.staging_locks = {}
        with state.lock:
            state.db.executescript('CREATE TABLE IF NOT EXISTS upload_watches(id TEXT PRIMARY KEY,value TEXT);'
                'CREATE TABLE IF NOT EXISTS upload_files(watch TEXT,path TEXT,value TEXT,PRIMARY KEY(watch,path));')
            state.db.commit()
        for watch in self.list():
            if watch['last_status'] == 'running':
                watch.update(last_status='interrupted', next_run=time.time(), stage='等待续传')
                self.put(watch)

    def roots(self):
        c = self.state.config
        return list(dict.fromkeys(c.get('upload_roots') or (c['source_roots'] + c['target_roots'])))

    def list(self, mode=None, limit=None):
        query = 'SELECT value FROM upload_watches'
        if mode == 'manual':
            query += " WHERE json_extract(value,'$.mode')='manual'"
        elif mode == 'watch':
            query += " WHERE COALESCE(json_extract(value,'$.mode'),'watch')!='manual'"
        query += ' ORDER BY rowid DESC'
        args = ()
        if limit is not None:
            query += ' LIMIT ?'; args = (int(limit),)
        with self.state.lock:
            rows = self.state.db.execute(query, args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def put(self, watch):
        with self.state.lock:
            self.state.db.execute('INSERT OR REPLACE INTO upload_watches VALUES(?,?)',
                                  (watch['id'], json.dumps(watch, ensure_ascii=False)))
            self.state.db.commit()

    def get(self, identifier):
        with self.state.lock:
            row = self.state.db.execute('SELECT value FROM upload_watches WHERE id=?', (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def files(self, identifier):
        with self.state.lock:
            rows = self.state.db.execute('SELECT path,value FROM upload_files WHERE watch=?', (identifier,)).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def put_file(self, identifier, relative, record):
        with self.state.lock:
            if not self.state.db.execute('SELECT 1 FROM upload_watches WHERE id=?', (identifier,)).fetchone():
                return
            self.state.db.execute('INSERT OR REPLACE INTO upload_files VALUES(?,?,?)',
                                  (identifier, relative, json.dumps(record, ensure_ascii=False)))
            self.state.db.commit()

    def snapshot(self, source):
        root = inside(source, self.roots())
        if not root.is_dir():
            raise ValueError('本地目录不可用，请检查容器挂载')
        result = {}
        for p in walk(root):
            if p.name.lower().endswith(TEMP_SUFFIXES):
                continue
            p = inside(p, [root])
            relative = p.relative_to(root).as_posix()
            for name in Path(relative).parts:
                safe_name(name)
            result[relative] = file_signature(p)
        return result

    def manual(self, data):
        sources = data.get('sources')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 1000 or any(not isinstance(p, str) for p in sources):
            raise ValueError('请选择 1 至 1000 个 NAS 文件或文件夹')
        selected = list(dict.fromkeys(inside(p, self.roots()) for p in sources if isinstance(p, str)))
        if len(selected) != len(set(sources)):
            raise ValueError('选择的本地路径无效或重复')
        if any(not p.is_file() and not p.is_dir() for p in selected):
            raise ValueError('所选文件或文件夹不存在，请检查容器挂载')
        if any(p.is_file() and p.name.lower().endswith(TEMP_SUFFIXES) for p in selected):
            raise ValueError('临时下载文件尚未完成，请完成下载后再上传')
        try:
            source = selected[0] if len(selected) == 1 and selected[0].is_dir() else Path(os.path.commonpath([str(p.parent) for p in selected]))
            source = inside(source, self.roots())
        except ValueError:
            raise ValueError('请分别上传不同挂载根目录中的文件') from None
        account, parent = parse_uri(data['target'])
        a = self.state.clouds.account(account)
        if a['provider'] != '115' or a['credentials'].get('auth_mode') != 'cookie':
            raise ValueError('请选择使用 Cookie / 扫码登录的 115 账号')
        target = self.state.clouds.node(account, parent)
        if not target['folder']:
            raise ValueError('请选择 115 目标文件夹')
        paths = sorted(p.relative_to(source).as_posix() for p in selected)
        # The scan root remains inside the allowed mount; retain a selected
        # directory's own name separately instead of discarding its outer level.
        prefix = safe_name(selected[0].name) if len(selected) == 1 and selected[0].is_dir() else ''
        target_label = a['name'] + target['path']
        if prefix:
            target_label = target_label.rstrip('/') + '/' + prefix
        with self.state.lock:
            if any(w.get('mode') == 'manual' and w['enabled'] and w['source'] == str(source)
                   and w.get('selection') == paths and w['target'] == data['target'] for w in self.list()):
                raise ValueError('这些文件已有手动上传任务')
            identifier, job_id = uuid.uuid4().hex, uuid.uuid4().hex
            now = time.time()
            task = dict(id=identifier, mode='manual', job_id=job_id,
                name=str(data.get('name') or (selected[0].name if len(selected) == 1 else '手动上传 %s 项' % len(selected)))[:80],
                source=str(source), selection=paths, manifest_ready=False, target=data['target'],
                target_prefix=prefix, target_label=target_label, account=account, parent=parent,
                interval=300, stable_seconds=0, include_existing=True,
                delete_local=data.get('delete_local') is True, enabled=True, created=now,
                next_run=now, last_status='queued', last_error='', uploaded_total=0)
            self.put(task)
            self.state.put_job(dict(id=job_id, type='upload', upload_mode='manual', status='queued',
                created=now, options={'source': str(source), 'target': task['target_label']}, done=0, total=0))
        self.wake.set()
        return task

    def manual_control(self, identifier, action):
        if action == 'delete':
            task = self.get(identifier)
            if not task or task.get('mode') != 'manual':
                raise ValueError('手动上传任务不存在')
            with self.staging_lock(identifier):
                if self.active_id == identifier:
                    raise ValueError('文件尚未停止，请稍后再移除')
                result = self.control(identifier, delete=True)
                if task.get('origin') == 'browser':
                    self.clear_browser_cache(task)
                return result
        with self.state.lock:
            task = self.get(identifier)
            if not task or task.get('mode') != 'manual':
                raise ValueError('手动上传任务不存在')
            if task.get('last_status') == 'receiving':
                raise ValueError('请先从电脑完成文件传输')
            if action == 'pause':
                self.save({'id': identifier, 'enabled': False})
                if self.active_id != identifier:
                    task.update(enabled=False, last_status='paused')
                    self.put(task)
                    job = self.state.job(task['job_id'], include_plan=False)
                    job['status'] = 'cancelled'
                    self.state.put_job(job)
                return {'ok': True}
            if action not in ('resume', 'retry'):
                raise ValueError('上传操作无效')
            if self.active_id == identifier:
                raise ValueError('请等待当前文件处理完成')
            if action == 'retry':
                for path, row in self.files(identifier).items():
                    if row['status'] in ('failed', 'conflict'):
                        row.update(status='pending', next_retry=0)
                        row.pop('error', None)
                        self.put_file(identifier, path, row)
            task.update(enabled=True, next_run=0, last_status='queued', last_error='')
            self.put(task)
        self.wake.set()
        return {'ok': True}

    def staging_lock(self, identifier):
        with self.state.lock:
            return self.staging_locks.setdefault(identifier, threading.Lock())

    def browser_task(self, identifier):
        task = self.get(identifier)
        if not task or task.get('mode') != 'manual' or task.get('origin') != 'browser':
            raise ValueError('电脑上传任务不存在')
        return task

    def task_source(self, task):
        if task.get('origin') == 'browser':
            expected = self.staging_root / task['id']
            if Path(task['source']) != expected or len(task['id']) != 32 or any(c not in '0123456789abcdef' for c in task['id']):
                raise ValueError('上传临时目录无效')
            return inside(expected, [self.staging_root])
        return inside(task['source'], self.roots())

    def browser_begin(self, data):
        files = data.get('files')
        if not isinstance(files, list) or not 1 <= len(files) <= 1000:
            raise ValueError('每批请选择 1 至 1000 个电脑文件')
        manifest, names = [], set()
        for f in files:
            if not isinstance(f, dict):
                raise ValueError('电脑文件信息无效')
            name, size = f.get('name'), f.get('size')
            if not isinstance(name, str) or len(name) > 4096 or PurePosixPath(name).is_absolute():
                raise ValueError('电脑文件路径无效')
            for part in name.split('/'):
                safe_name(part)
                if len(part.encode('utf-8')) > 255:
                    raise ValueError('文件名过长')
            if name in names or not isinstance(size, int) or isinstance(size, bool) or not 0 <= size < 2**63:
                raise ValueError('文件名重复或文件大小无效')
            if name.lower().endswith(TEMP_SUFFIXES):
                raise ValueError('请完成临时文件的下载后再上传')
            names.add(name); manifest.append(dict(name=name, size=size, received=0))
        if any('/'.join(name.split('/')[:i]) in names for name in names for i in range(1, len(name.split('/')))):
            raise ValueError('文件和文件夹路径存在同名冲突')
        account, parent = parse_uri(data['target'])
        a = self.state.clouds.account(account)
        if a['provider'] != '115' or a['credentials'].get('auth_mode') != 'cookie':
            raise ValueError('请选择使用 Cookie / 扫码登录的 115 账号')
        target = self.state.clouds.node(account, parent)
        if not target['folder']:
            raise ValueError('请选择 115 目标文件夹')
        self.staging_root.mkdir(exist_ok=True)
        total = sum(f['size'] for f in manifest)
        identifier, job_id = uuid.uuid4().hex, uuid.uuid4().hex
        now = time.time()
        task = dict(id=identifier, mode='manual', origin='browser', job_id=job_id,
            name=manifest[0]['name'] if len(files) == 1 else '电脑上传 %s 个文件' % len(files),
            source=str(self.staging_root / identifier), selection=['.'], manifest_ready=False,
            browser_files=manifest, total_bytes=total, received_bytes=0,
            target=data['target'], target_label=a['name'] + target['path'], account=account, parent=parent,
            interval=300, stable_seconds=0, include_existing=True, delete_local=True,
            enabled=False, created=now, next_run=now, last_status='receiving', last_error='', uploaded_total=0)
        with self.state.lock:
            reserved = sum(w.get('total_bytes', 0) - w.get('received_bytes', 0) for w in self.list()
                           if w.get('origin') == 'browser' and w['last_status'] == 'receiving')
            if shutil.disk_usage(self.staging_root).free < total + reserved + 32 * 1024 * 1024:
                raise ValueError('NAS 临时目录空间不足，请减少本批文件或使用 NAS 本地文件上传')
            Path(task['source']).mkdir()
            self.put(task)
            self.state.put_job(dict(id=job_id, type='upload', upload_mode='manual', status='receiving', created=now,
                options={'source': '电脑文件', 'target': task['target_label']}, done=0, total=len(manifest)))
        return task

    def browser_chunk(self, identifier, index, offset, length, stream):
        if not 0 < length <= 8 * 1024 * 1024:
            raise ValueError('每次传输数据需要在 8MB 以内')
        with self.staging_lock(identifier):
            task = self.browser_task(identifier)
            if task['last_status'] != 'receiving' or not 0 <= index < len(task['browser_files']):
                raise ValueError('电脑上传任务状态无效')
            record = task['browser_files'][index]
            if offset != record['received'] or offset + length > record['size']:
                raise ValueError('文件传输进度不一致，请继续传输或重新选择文件')
            root = self.task_source(task)
            target = inside(root / record['name'], [root]); target.parent.mkdir(parents=True, exist_ok=True)
            if offset and (not target.exists() or target.stat().st_size < offset):
                raise ValueError('NAS 临时文件已变更，请重新选择文件开始传输')
            if shutil.disk_usage(root).free < length + 16 * 1024 * 1024:
                raise ValueError('NAS 临时目录空间不足')
            with target.open('r+b' if target.exists() else 'w+b') as f:
                f.truncate(offset); f.seek(offset)
                remaining = length
                while remaining:
                    block = stream.read(min(128 * 1024, remaining))
                    if not block:
                        raise ValueError('电脑文件传输中断，可继续传输')
                    f.write(block); remaining -= len(block)
            record['received'] += length
            task['received_bytes'] += length
            task.update(current=record['name'], stage='接收电脑文件', file_done=record['received'], file_size=record['size'])
            self.put(task)
            return {'received': record['received'], 'received_bytes': task['received_bytes']}

    def browser_finish(self, identifier):
        with self.staging_lock(identifier):
            task = self.browser_task(identifier)
            if task['last_status'] != 'receiving':
                raise ValueError('电脑文件已经提交上传')
            root = self.task_source(task)
            for record in task['browser_files']:
                target = inside(root / record['name'], [root])
                if record['size'] == 0:
                    target.parent.mkdir(parents=True, exist_ok=True); target.touch(exist_ok=True)
                if record['received'] != record['size'] or not target.is_file() or target.stat().st_size != record['size']:
                    raise ValueError('电脑文件尚未传输完整')
            task.update(enabled=True, last_status='queued', next_run=0, stage='等待上传到 115')
            self.put(task)
            job = self.state.job(task['job_id'], include_plan=False); job['status'] = 'queued'; self.state.put_job(job)
        self.wake.set()
        return {'ok': True, 'id': task['id']}

    def clear_browser_cache(self, task):
        root = self.task_source(task)
        # Only this server-created task directory may be recursively removed.
        if root.parent != self.staging_root.resolve() or root.name != task['id']:
            raise ValueError('上传临时目录超出允许范围')
        if root.exists():
            try:
                shutil.rmtree(root)
            except FileNotFoundError:
                pass

    def manual_snapshot(self, task, cancel):
        root = self.task_source(task)
        result = {}
        directories = set()
        def visit(folder):
            relative = Path(folder).relative_to(root)
            for part in relative.parts:
                safe_name(part)
            directories.add(relative.as_posix())
        if task.get('manifest_ready'):
            paths = [root / name for name, row in self.files(task['id']).items() if row['status'] == 'pending']
        elif task.get('origin') == 'browser':
            paths = [root / row['name'] for row in task['browser_files']]
        else:
            paths = [root / name for name in task['selection']]
        for selected in paths:
            if cancel():
                raise UploadCancelled('上传已暂停')
            selected = inside(selected, [root])
            if not selected.exists():
                raise CloudError('手动上传的源文件已不存在：' + str(selected.relative_to(root)))
            for p in walk(selected, cancel, visit) if selected.is_dir() else (selected,):
                if cancel():
                    raise UploadCancelled('上传已暂停')
                if p.name.lower().endswith(TEMP_SUFFIXES):
                    continue
                p = inside(p, [root])
                relative = p.relative_to(root).as_posix()
                for name in Path(relative).parts:
                    safe_name(name)
                result[relative] = file_signature(p)
        if not task.get('manifest_ready'):
            task['directory_manifest'] = sorted(directories)
        return result

    def manual_totals(self, identifier):
        records = self.files(identifier)
        return dict(uploaded=sum(r['status'] == 'uploaded' for r in records.values()),
            skipped=sum(r['status'] == 'skipped' for r in records.values()),
            conflicts=sum(r['status'] == 'conflict' for r in records.values()),
            waiting=sum(r['status'] == 'pending' for r in records.values()),
            deleted=sum(bool(r.get('local_deleted')) for r in records.values()),
            errors=[{'path': p, 'message': r.get('error', '上传失败')} for p, r in records.items() if r['status'] == 'failed'])

    def save(self, data):
        if data.get('id'):
            with self.state.lock:
                watch = self.get(data['id'])
                if not watch:
                    raise ValueError('上传监控不存在')
                watch['enabled'] = data.get('enabled', watch['enabled']) is True
                watch['next_run'] = time.time()
                if not watch['enabled'] and self.active_id == watch['id']:
                    self.cancelled.add(self.active_job)
                self.put(watch)
            self.wake.set()
            return watch
        source = str(inside(data['source'], self.roots()))
        account, target = parse_uri(data['target'])
        a = self.state.clouds.account(account)
        if a['provider'] != '115' or a['credentials'].get('auth_mode') != 'cookie':
            raise ValueError('上传监控请选择使用 Cookie / 扫码登录的 115 账号')
        if not self.state.clouds.node(account, target)['folder']:
            raise ValueError('请选择 115 目标文件夹')
        interval, stable = int(data.get('interval', 300)), int(data.get('stable_seconds', 60))
        if not 60 <= interval <= 86400 or not 10 <= stable <= 86400:
            raise ValueError('检查间隔需为 1 分钟至 24 小时，稳定时间需为 10 秒至 24 小时')
        if any(w.get('mode') != 'manual' and w['source'] == source and w['target'] == data['target'] for w in self.list()):
            raise ValueError('此目录组合已有上传监控')
        started = time.time()
        snapshot = self.snapshot(source)
        watch = dict(id=uuid.uuid4().hex, name=str(data.get('name') or Path(source).name)[:80],
            source=source, target=data['target'], target_label=a['name'] + self.state.clouds.node(account, target)['path'],
            account=account, parent=target, interval=interval, stable_seconds=stable,
            include_existing=data.get('include_existing') is True,
            delete_local=data.get('delete_local') is True, enabled=True, created=started,
            next_run=time.time(), last_status='waiting', last_error='', uploaded_total=0)
        with self.state.lock:
            for relative, sig in snapshot.items():
                state = 'pending' if watch['include_existing'] or sig[4] / 1e9 > started else 'baseline'
                self.state.db.execute('INSERT INTO upload_files VALUES(?,?,?)',
                    (watch['id'], relative, json.dumps(dict(signature=sig, status=state, observed=time.time()))))
            self.put(watch)
        self.wake.set()
        return watch

    def control(self, identifier, retry=False, delete=False):
        with self.state.lock:
            watch = self.get(identifier)
            if not watch:
                raise ValueError('上传监控不存在')
            if delete:
                if self.active_id == identifier:
                    self.cancelled.add(self.active_job)
                self.state.db.execute('DELETE FROM upload_watches WHERE id=?', (identifier,))
                self.state.db.execute('DELETE FROM upload_files WHERE watch=?', (identifier,))
                self.state.db.commit()
            else:
                if not watch['enabled']:
                    raise ValueError('请先启用此上传监控')
                if retry:
                    if self.active_id == identifier:
                        raise ValueError('请等待当前检查完成后重试')
                    for path, row in self.files(identifier).items():
                        if row['status'] in ('failed', 'conflict'):
                            row.update(status='pending', next_retry=0)
                            self.put_file(identifier, path, row)
                watch['next_run'] = 0
                self.put(watch)
        self.wake.set()
        return {'ok': True}

    def account_removed(self, account):
        for w in self.list():
            if w['account'] == account:
                self.save({'id': w['id'], 'enabled': False})
                w.update(enabled=False, last_status='failed', last_error='115 账号已移除')
                self.put(w)

    def tick(self):
        with self.state.lock:
            if self.active_id or self.stop.is_set():
                return False
            row = self.state.db.execute("SELECT value FROM upload_watches WHERE json_extract(value,'$.enabled')=1 "
                "AND json_extract(value,'$.next_run')<=? ORDER BY CASE WHEN json_extract(value,'$.mode')='manual' THEN 0 ELSE 1 END, "
                "json_extract(value,'$.next_run') LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return False
            watch = json.loads(row[0])
            self.active_id, self.active_job = watch['id'], watch.get('job_id') or uuid.uuid4().hex
        self.run(watch, self.active_job)
        return True

    def run(self, watch, job_id):
        state = self.state
        manual = watch.get('mode') == 'manual'
        now = time.time()
        result = self.manual_totals(watch['id']) if manual else dict(uploaded=0, skipped=0, conflicts=0, waiting=0, deleted=0, errors=[])
        job = state.job(job_id, include_plan=False) if manual else dict(id=job_id, type='upload', created=now, options={
            'source': watch['source'], 'target': watch['target_label']}, done=0, total=0)
        job['status'] = 'running'
        job.pop('error', None)
        recorded, last_progress = manual, [0.0]
        watch.update(last_status='running', last_run=now, last_error='', current='', stage='检查文件')
        self.put(watch)
        if manual:
            state.put_job(job)
        revision = None

        def cancel():
            return self.stop.is_set() or job_id in self.cancelled

        def progress(stage, done, total):
            if cancel():
                raise UploadCancelled('上传已暂停')
            if time.monotonic() - last_progress[0] < 1 and done != total:
                return
            last_progress[0] = time.monotonic()
            if state.clouds.account(watch['account'])['revision'] != revision:
                raise UploadCancelled('网盘设置已更新，等待下一次检查')
            if file_signature(current_path) != current_record['signature']:
                raise CloudError('源文件仍在变化，下一次检查后重试')
            watch.update(stage={'hashing': '校验文件', 'uploading': '上传中', 'verifying': '核对网盘'}[stage],
                         file_done=done, file_size=total)
            self.put(watch)

        try:
            snapshot = self.manual_snapshot(watch, cancel) if manual else self.snapshot(watch['source'])
            records = self.files(watch['id'])
            if manual and not watch.get('manifest_ready'):
                with state.lock:
                    state.db.executemany('INSERT OR REPLACE INTO upload_files VALUES(?,?,?)',
                        [(watch['id'], name, json.dumps(dict(signature=sig, status='pending', observed=0))) for name, sig in snapshot.items()])
                    watch['manifest_ready'] = True
                    self.put(watch)
                records = self.files(watch['id'])
            pending = []
            for relative, sig in snapshot.items():
                old = records.get(relative)
                if old is None or old['signature'] != sig:
                    old = dict(signature=sig, status='pending', observed=time.time())
                    self.put_file(watch['id'], relative, old)
                if old['status'] not in (('pending',) if manual else ('pending', 'failed')):
                    continue
                if time.time() - old['observed'] < watch['stable_seconds'] or old.get('next_retry', 0) > time.time():
                    result['waiting'] += 1
                    continue
                pending.append((relative, old))
            if manual:
                job.update(total=len(records), done=sum(r['status'] != 'pending' for r in records.values()))
                watch.update(file_count=job['total'], completed_count=job['done'])
                self.put(watch)
                result = self.manual_totals(watch['id'])
                result['waiting'] = 0
            else:
                job['total'] = len(pending)
            directory_manifest = watch.get('directory_manifest', []) if manual else []
            create_directories = directory_manifest and not watch.get('directories_ready')
            if pending or create_directories:
                recorded = True
                state.put_job(job)
                a = copy.deepcopy(state.clouds.account(watch['account']))
                revision = a['revision']
                client = Cookie115(a['credentials'])
                directories, listings = {(): watch['parent']}, {}
                prefix = PurePosixPath(watch.get('target_prefix') or '.').parts
                for part in prefix:
                    safe_name(part)

                def children(parent, fresh=False):
                    if fresh or parent not in listings:
                        listings[parent] = list(client.list(parent))
                    return listings[parent]

                def directory(parts):
                    key = tuple(parts)
                    if key in directories:
                        return directories[key]
                    parent = directory(parts[:-1])
                    rows = [r for r in children(parent) if r['name'] == parts[-1]]
                    if len(rows) > 1 or (rows and not rows[0]['folder']):
                        raise CloudError('115 中存在同名项，无法确定上传目录')
                    identifier = rows[0]['id'] if rows else client.mkdir(parent, parts[-1])
                    if not rows:
                        rows = [r for r in children(parent, True) if r['name'] == parts[-1]]
                        if len(rows) != 1 or not rows[0]['folder'] or rows[0]['id'] != identifier:
                            raise CloudError('115 新建目录尚未核对通过，稍后重试')
                    directories[key] = identifier
                    return identifier

                if create_directories:
                    for relative in directory_manifest:
                        if cancel():
                            raise UploadCancelled('上传已暂停')
                        directory(prefix + PurePosixPath(relative).parts)
                    watch['directories_ready'] = True
                    self.put(watch)

                for relative, current_record in pending[:200]:
                    if cancel():
                        raise UploadCancelled('上传已暂停')
                    watch.update(current=relative, file_done=0, file_size=current_record['signature'][0], stage='校验文件')
                    self.put(watch)
                    current_path = inside(Path(watch['source']) / relative, [watch['source']])
                    try:
                        sha1 = digest(current_path, cancel, progress)
                        if file_signature(current_path) != current_record['signature']:
                            raise CloudError('源文件仍在变化，稍后重试')
                        parent = directory(prefix + Path(relative).parts[:-1])
                        existing = [r for r in children(parent) if r['name'] == current_path.name]
                        if existing:
                            if (len(existing) == 1 and not existing[0]['folder'] and existing[0]['size'] == current_path.stat().st_size
                                    and existing[0].get('sha1', '').upper() == sha1):
                                current_record.update(status='skipped', session=None, sha1=sha1)
                                result['skipped'] += 1
                            else:
                                current_record.update(status='conflict', error='网盘存在同名文件，未覆盖')
                                result['conflicts'] += 1
                        else:
                            def checkpoint(session):
                                current_record['session'] = session
                                self.put_file(watch['id'], relative, current_record)
                            client.upload(current_path, parent, sha1, session=current_record.get('session'),
                                          checkpoint=checkpoint, progress=progress, cancel=cancel)
                            if file_signature(current_path) != current_record['signature']:
                                raise CloudError('上传期间源文件发生变化，下一次检查后重试')
                            verified = False
                            for delay in (0, 1, 2, 4):
                                if self.stop.wait(delay) or cancel():
                                    raise UploadCancelled('上传已暂停')
                                rows = [r for r in children(parent, True) if r['name'] == current_path.name and not r['folder']]
                                if (len(rows) == 1 and rows[0]['size'] == current_path.stat().st_size
                                        and rows[0].get('sha1', '').upper() == sha1):
                                    verified = True
                                    break
                            if not verified:
                                raise CloudError('上传结果尚未在目标目录校验通过，稍后会重新核对')
                            current_record.update(status='uploaded', sha1=sha1, session=None, uploaded=time.time())
                            result['uploaded'] += 1
                            watch['uploaded_total'] += 1
                        if watch.get('delete_local') and current_record['status'] in ('uploaded', 'skipped'):
                            if cancel():
                                raise UploadCancelled('上传已暂停')
                            rows = [r for r in children(parent, True) if r['name'] == current_path.name]
                            if (len(rows) != 1 or rows[0]['folder'] or rows[0]['size'] != current_record['signature'][0]
                                    or rows[0].get('sha1', '').upper() != sha1):
                                raise CloudError('网盘文件发生变化，保留本地文件并等待重新核对')
                            if file_signature(current_path) != current_record['signature']:
                                raise CloudError('本地文件已变化，保留文件并等待重新检查')
                            current_path.unlink()
                            current_record['local_deleted'] = time.time()
                            result['deleted'] += 1
                        current_record.pop('error', None) if current_record['status'] != 'conflict' else None
                    except UploadCancelled:
                        raise
                    except Exception as exc:
                        message = str(exc) if isinstance(exc, (ValueError, OSError)) else '上传失败，请检查连接后重试'
                        result['errors'].append({'path': relative, 'message': message})
                        attempts = current_record.get('attempts', 0) + 1
                        current_record.update(status='failed', attempts=attempts, error=message,
                            next_retry=time.time() + min(3600, watch['interval'] * 2 ** min(attempts - 1, 4)))
                    self.put_file(watch['id'], relative, current_record)
                    job.update(done=job['done'] + 1, current=relative, result=result)
                    if manual:
                        watch['completed_count'] = job['done']
                        self.put(watch)
                    state.put_job(job)
            status = 'partial' if result['errors'] or result['conflicts'] else ('completed' if recorded else 'idle')
            watch.update(last_status=status, last_result=result,
                         last_error='；'.join(e['message'] for e in result['errors'][:3]))
            job.update(status=status, result=result)
        except UploadCancelled as exc:
            job.update(status='cancelled', result=result)
            watch.update(last_status='paused', last_error=str(exc), last_result=result)
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, OSError)) else '目录检查失败，请检查目录和网盘连接'
            watch.update(last_status='failed', last_error=message, last_result=result)
            job.update(status='failed', error=message, result=result)
            recorded = True
        finally:
            with state.lock:
                if recorded:
                    state.put_job(job)
                    watch['last_job'] = job_id
                current = self.get(watch['id'])
                if current:
                    watch.update(enabled=current['enabled'], next_run=time.time() + watch['interval'])
                    if cancel() and current['enabled'] and not self.stop.is_set():
                        watch['next_run'] = time.time()
                    if manual:
                        if watch.get('manifest_ready'):
                            totals = self.manual_totals(watch['id'])
                            records = self.files(watch['id'])
                            job.update(result=totals, done=sum(r['status'] != 'pending' for r in records.values()), total=len(records))
                            watch.update(last_result=totals, completed_count=job['done'], file_count=job['total'])
                            if not cancel() and job['status'] != 'failed':
                                watch['enabled'] = current['enabled'] and totals['waiting'] > 0
                                watch['next_run'] = time.time()
                                job['status'] = 'queued' if watch['enabled'] else ('partial' if totals['errors'] or totals['conflicts'] else 'completed')
                                watch.update(last_status=job['status'], last_result=totals)
                            elif not current['enabled']:
                                job['status'] = 'cancelled'
                                watch['last_status'] = 'paused'
                        else:
                            watch['enabled'] = False
                        if self.stop.is_set() and current['enabled'] and watch.get('manifest_ready'):
                            watch.update(enabled=True, next_run=time.time(), last_status='interrupted')
                            job['status'] = 'interrupted'
                        if job['status'] == 'failed':
                            watch['enabled'] = False
                        state.put_job(job)
                    self.put(watch)
                self.active_id = self.active_job = None
                self.cancelled.discard(job_id)
            if manual and watch.get('origin') == 'browser' and watch.get('last_status') == 'completed':
                self.clear_browser_cache(watch)

    def start(self):
        def loop():
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception:
                    pass
                self.wake.wait(2)
                self.wake.clear()
        self.thread = threading.Thread(target=loop, name='upload-watch', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=3)
