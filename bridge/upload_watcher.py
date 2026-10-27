"""Additions-only local-to-115 watches, with persistent baselines and upload sessions."""
import copy
import json
from pathlib import Path
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

    def list(self):
        with self.state.lock:
            rows = self.state.db.execute('SELECT value FROM upload_watches ORDER BY rowid DESC').fetchall()
        return [json.loads(r[0]) for r in rows]

    def put(self, watch):
        with self.state.lock:
            self.state.db.execute('INSERT OR REPLACE INTO upload_watches VALUES(?,?)',
                                  (watch['id'], json.dumps(watch, ensure_ascii=False)))
            self.state.db.commit()

    def get(self, identifier):
        return next((w for w in self.list() if w['id'] == identifier), None)

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
        if any(w['source'] == source and w['target'] == data['target'] for w in self.list()):
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
            due = sorted((w for w in self.list() if w['enabled'] and w['next_run'] <= time.time()),
                         key=lambda w: w['next_run'])
            if not due:
                return False
            watch = due[0]
            self.active_id, self.active_job = watch['id'], uuid.uuid4().hex
        self.run(watch, self.active_job)
        return True

    def run(self, watch, job_id):
        state = self.state
        now = time.time()
        result = dict(uploaded=0, skipped=0, conflicts=0, waiting=0, deleted=0, errors=[])
        job = dict(id=job_id, type='upload', status='running', created=now, options={
            'source': watch['source'], 'target': watch['target_label']}, done=0, total=0)
        recorded, last_progress = False, [0.0]
        watch.update(last_status='running', last_run=now, last_error='', current='', stage='检查文件')
        self.put(watch)
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
            snapshot = self.snapshot(watch['source'])
            records = self.files(watch['id'])
            pending = []
            for relative, sig in snapshot.items():
                old = records.get(relative)
                if old is None or old['signature'] != sig:
                    old = dict(signature=sig, status='pending', observed=time.time())
                    self.put_file(watch['id'], relative, old)
                if old['status'] not in ('pending', 'failed'):
                    continue
                if time.time() - old['observed'] < watch['stable_seconds'] or old.get('next_retry', 0) > time.time():
                    result['waiting'] += 1
                    continue
                pending.append((relative, old))
            job['total'] = len(pending)
            if pending:
                recorded = True
                state.put_job(job)
                a = copy.deepcopy(state.clouds.account(watch['account']))
                revision = a['revision']
                client = Cookie115(a['credentials'])
                directories, listings = {(): watch['parent']}, {}

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
                        parent = directory(Path(relative).parts[:-1])
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
                    self.put(watch)
                self.active_id = self.active_job = None
                self.cancelled.discard(job_id)

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
