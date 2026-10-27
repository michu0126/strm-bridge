"""Persistent polling of mounted folders, including FUSE mounts without events."""
import json
from pathlib import Path
import threading
import time
import uuid

from .core import inside, make_plan, execute, media_extensions
from .cloud import is_cloud, parse_uri
from .progress import ExecutionProgress


class Watcher:
    def __init__(self, state):
        self.state = state
        self.stop = threading.Event()
        self.thread = None
        self.active_id = None
        with state.lock:
            state.db.execute('CREATE TABLE IF NOT EXISTS watches(id TEXT PRIMARY KEY, value TEXT)')
            state.db.commit()

    def list(self):
        with self.state.lock:
            rows = self.state.db.execute('SELECT value FROM watches ORDER BY rowid DESC').fetchall()
        return [json.loads(r[0]) for r in rows]

    def put(self, watch):
        with self.state.lock:
            self.state.db.execute('INSERT OR REPLACE INTO watches VALUES(?,?)', (watch['id'], json.dumps(watch, ensure_ascii=False)))
            self.state.db.commit()

    def save(self, data):
        state = self.state
        with state.lock:
            if data.get('id'):
                watch = next((w for w in self.list() if w['id'] == data['id']), None)
                if not watch:
                    raise ValueError('监控不存在')
                watch['enabled'] = data.get('enabled', watch['enabled']) is True
                watch['next_run'] = time.time()
                if not watch['enabled'] and self.active_id == watch['id'] and state.busy:
                    state.cancelled.add(state.busy)
            else:
                options = dict(data['options'])
                remote = is_cloud(options['source'])
                source = options['source'] if remote else inside(options['source'], state.config['source_roots'])
                target = inside(options['target'], state.config['target_roots'])
                if remote:
                    account, identifier = parse_uri(source)
                    if not state.clouds.node(account, identifier)['folder']:
                        raise ValueError('请选择网盘文件夹')
                    options['mode'] = 'redirect'
                if (not remote and not source.is_dir()) or not target.is_dir():
                    raise ValueError('请选择已挂载的源目录和已存在的媒体库目录')
                if not remote and (source.is_relative_to(target) or target.is_relative_to(source)):
                    raise ValueError('源目录和目标目录不能互相包含')
                if options.get('kind') not in ('movie', 'tv', 'jav') or options.get('mode') not in ('path', 'redirect'):
                    raise ValueError('监控的媒体类型或播放入口无效')
                media_extensions(options.get('extra_extensions', ''))
                interval = int(data.get('interval', 300))
                if not 60 <= interval <= 86400:
                    raise ValueError('检查间隔需为 1 分钟至 24 小时')
                options.update(source=str(source), target=str(target), cleanup=False, subtitle_mode='copy')
                if any(w['options']['source'] == str(source) and w['options']['target'] == str(target) for w in self.list()):
                    raise ValueError('此目录组合已有监控，请在监控列表中启用')
                watch = {'id': uuid.uuid4().hex, 'options': options, 'interval': interval, 'enabled': True,
                         'created': time.time(), 'next_run': time.time(), 'last_status': 'waiting', 'last_error': ''}
            self.put(watch)
            return watch

    def delete(self, watch_id):
        with self.state.lock:
            if self.active_id == watch_id and self.state.busy:
                self.state.cancelled.add(self.state.busy)
            self.state.db.execute('DELETE FROM watches WHERE id=?', (watch_id,))
            self.state.db.commit()
        return {'ok': True}

    def tick(self):
        state = self.state
        with state.lock:
            if state.busy:
                return False
            due = sorted((w for w in self.list() if w['enabled'] and w.get('next_run', 0) <= time.time()), key=lambda w: w.get('next_run', 0))
            if not due:
                return False
            watch = due[0]
            job_id = uuid.uuid4().hex
            state.busy = job_id
            self.active_id = watch['id']
            config = dict(state.config)
            watch.update(next_run=time.time() + watch['interval'], last_run=time.time(), last_status='running', last_error='')
            self.put(watch)
        state.pool.submit(self.run, watch, job_id, config)
        return True

    def run(self, watch, job_id, config):
        state = self.state
        job = {'id': job_id, 'watch_id': watch['id'], 'status': 'scanning', 'created': time.time(),
               'options': watch['options'], 'done': 0, 'total': 0}
        recorded = True
        state.put_job(job)
        cancel = lambda: self.stop.is_set() or job_id in state.cancelled
        try:
            plan = state.make_plan(watch['options'], config, cancel, state.scan_progress(job))
            # Additions only: never move, clean up, rewrite or delete an existing file.
            # A recently modified source is retried on the next pass.
            plan['actions'] = [a for a in plan['actions'] if a['type'] in ('strm', 'copy')
                               and not Path(a['target']).exists()
                               and (a['remote']['modified'] if 'remote' in a else Path(a['source']).stat().st_mtime) <= time.time() - 60]
            plan['counts'] = {k: sum(a['type'] == k for a in plan['actions']) for k in ('strm', 'copy', 'move', 'trash')}
            if plan['actions']:
                job.update(status='running', plan=plan, counts=plan['counts'], warnings=plan['warnings'])
                state.put_job(job)
                recorded = True
                progress = ExecutionProgress(state, job)
                result = state.execute(plan, config, state.playback, progress, cancel)
                job.update(status='cancelled' if result['cancelled'] else ('partial' if result['errors'] else 'completed'), result=result)
                watch.update(last_status=job['status'], last_job=job_id, last_result=result,
                             last_error='；'.join(e['message'] for e in result['errors'][:3]))
            else:
                watch.update(last_status='idle', last_error='', last_result={'created': 0, 'subtitles': 0})
                job.update(status='completed', counts=plan['counts'], result={
                    'created': 0, 'skipped': 0, 'subtitles': 0, 'trashed': 0, 'errors': [], 'cancelled': False})
        except Exception as e:
            job.update(status='cancelled' if cancel() else 'failed', error=str(e))
            watch.update(last_status=job['status'], last_error=str(e), last_job=job_id)
            recorded = True
        finally:
            with state.lock:
                if recorded:
                    state.put_job(job)
                # Preserve a pause or removal requested while scanning was in progress.
                current = next((w for w in self.list() if w['id'] == watch['id']), None)
                if current:
                    watch['enabled'] = current['enabled']
                    watch['next_run'] = time.time() + watch['interval']
                    self.put(watch)
                state.busy = None
                self.active_id = None

    def start(self):
        def loop():
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception:
                    pass
                self.stop.wait(2)
        self.thread = threading.Thread(target=loop, name='folder-watch', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
