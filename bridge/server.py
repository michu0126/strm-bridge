import base64
from concurrent.futures import ThreadPoolExecutor
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit
import uuid

from . import __version__
from .core import VIDEO, SUB, SKIP_DIRS, inside, make_plan, execute
from .progress import ExecutionProgress
from .resolver import valid_url, resolve, test_connection
from .watcher import Watcher
from .upload_watcher import UploadWatcher
from .appearance import Wallpapers, MAX_IMAGE
from .generated import GeneratedRecords
from .reconcile import Reconciler
from .videos import VideoLibrary
from .cloud import Clouds, is_cloud
from .cloud_providers import CATALOG
from .cloud_login import CloudLogin
from .mounts import apply_mount_roots
from . import emby, webdav

STATIC = Path(__file__).parent.parent / 'static'

class State:
    def __init__(self, data):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.cancelled = set()
        self.busy = None
        self.db = sqlite3.connect(str(self.data / 'bridge.db'), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, value TEXT); CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY, path TEXT); CREATE TABLE IF NOT EXISTS journal(job TEXT, kind TEXT, value TEXT);')
        self.db.execute('CREATE TABLE IF NOT EXISTS job_plans(id TEXT PRIMARY KEY,value TEXT)')
        self.saved_plans = {row[0] for row in self.db.execute('SELECT id FROM job_plans')}
        self.key = self.secret('playback-key.txt')
        self.password = os.environ.get('ADMIN_PASSWORD') or self.secret('admin-password.txt')
        self.config_path = self.data / 'config.json'
        self.config = json.loads(self.config_path.read_text(encoding='utf-8')) if self.config_path.exists() else {
            'source_roots': json.loads(os.environ.get('SOURCE_ROOTS', '["/cloud"]')),
            'target_roots': json.loads(os.environ.get('TARGET_ROOTS', '["/library"]')),
            'public_url': os.environ.get('PUBLIC_URL', 'http://localhost:18642'),
            'provider': 'none', 'default_mode': 'path',
            'openlist_url': '', 'openlist_user': '', 'openlist_password': '', 'openlist_token': '',
            'url_template': '',
            'cd2_url': os.environ.get('CD2_URL', ''),
            'cd2_token': '', 'cd2_user': '', 'cd2_password': '',
            'mappings': []}
        self.config.setdefault('provider', 'cd2' if self.config.get('cd2_url') else 'none')
        self.config.setdefault('default_mode', 'redirect' if self.config['provider'] != 'none' else 'path')
        self.config.setdefault('emby_url', '')
        self.config.setdefault('webdav_enabled', False)
        if 'UPLOAD_ROOTS' in os.environ:
            self.config['upload_roots'] = json.loads(os.environ['UPLOAD_ROOTS'])
        else:
            self.config.setdefault('upload_roots', [])
        if not self.config_path.exists():
            self.save_config()
        apply_mount_roots(self.config, self.data)
        for row in self.db.execute("SELECT id,value FROM jobs WHERE json_extract(value,'$.status') IN ('running','scanning','queued')").fetchall():
            j = json.loads(row[1])
            if j['status'] in ('running', 'scanning', 'queued'):
                message = ('服务重启，上传监控将在启用后自动继续。' if j.get('type') == 'upload' else
                           '服务重启中断了任务，请重新预览。已完成操作保留，可查看回收记录。')
                j.update(status='interrupted', error=message)
                self.put_job(j)
        self.clouds = Clouds(self)
        self.generated = GeneratedRecords(self)
        self.reconciler = Reconciler(self)
        self.dav_cache = webdav.DirectoryCache(self)
        self.cloud_login = CloudLogin()
        self.watcher = Watcher(self)
        self.uploads = UploadWatcher(self)
        self.videos = VideoLibrary(self)
        self.wallpapers = Wallpapers(self.data)

    def secret(self, name):
        p = self.data / name
        if not p.exists():
            p.write_text(secrets.token_urlsafe(32), encoding='utf-8')
            p.chmod(0o600)
        return p.read_text().strip()

    def save_config(self):
        p = self.config_path.with_suffix('.tmp')
        p.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding='utf-8')
        p.chmod(0o600)
        p.replace(self.config_path)

    def put_job(self, job):
        with self.lock:
            plan = job.get('plan')
            if plan is not None and job['id'] not in self.saved_plans:
                self.db.execute('INSERT OR REPLACE INTO job_plans VALUES(?,?)', (job['id'], json.dumps(plan, ensure_ascii=False)))
                self.saved_plans.add(job['id'])
            value = {k: v for k, v in job.items() if k != 'plan'}
            if plan is not None:
                value['action_total'] = len(plan['actions'])
            self.db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?)', (job['id'], json.dumps(value, ensure_ascii=False)))
            self.db.commit()

    def job(self, job_id, include_plan=True):
        with self.lock:
            row = self.db.execute("SELECT json_remove(value,'$.plan') FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError('任务不存在')
        job = json.loads(row[0])
        if include_plan:
            with self.lock:
                stored = self.db.execute("SELECT value FROM job_plans WHERE id=? UNION ALL SELECT json_extract(value,'$.plan') FROM jobs WHERE id=? LIMIT 1", (job_id, job_id)).fetchone()
            if stored and stored[0]:
                job['plan'] = json.loads(stored[0])
        return job

    def job_preview(self, job_id, offset=0):
        job = self.job(job_id, include_plan=False)
        # SQLite streams the requested slice; polling never decodes the entire plan.
        with self.lock:
            source = "WITH p AS (SELECT value FROM job_plans WHERE id=? UNION ALL SELECT json_extract(value,'$.plan') FROM jobs WHERE id=? LIMIT 1) "
            rows = self.db.execute(source + "SELECT a.value FROM p,json_each(p.value,'$.actions') a LIMIT 500 OFFSET ?", (job_id, job_id, max(0, offset))).fetchall()
            count = self.db.execute(source + "SELECT json_array_length(value,'$.actions') FROM p", (job_id, job_id)).fetchone()
        if count and count[0] is not None:
            job.update(actions=[json.loads(row[0]) for row in rows], action_total=count[0])
        return job

    def jobs(self):
        with self.lock:
            rows = self.db.execute("SELECT json_remove(value,'$.plan') FROM jobs ORDER BY rowid DESC LIMIT 50").fetchall()
        return [json.loads(row[0]) for row in rows]

    def journal(self, job, kind, value):
        with self.lock:
            self.db.execute('INSERT INTO journal VALUES(?,?,?)', (job, kind, json.dumps(value)))
            self.db.commit()

    def sign(self, media_id):
        return hmac.new(self.key.encode(), media_id.encode(), hashlib.sha256).hexdigest()

    def playback(self, p, register=True):
        remote = isinstance(p, dict)
        identity = ('cloud:' + p['account'] + ':' + str(p['object'].get('fsid', p['object']['id']))) if remote else str(p)
        stored = 'cloud:' + json.dumps(p, ensure_ascii=False) if remote else str(p)
        media_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        if register:
            with self.lock:
                self.db.execute('INSERT OR REPLACE INTO media VALUES(?,?)', (media_id, stored))
                self.db.commit()
        return self.config['public_url'].rstrip('/') + '/play/' + media_id + '/' + self.sign(media_id) + '/' + quote(p['object']['name'] if remote else p.name, safe='')

    def make_plan(self, options, config, cancel=lambda: False, progress=lambda info: None):
        self.generated.backfill(cancel)
        if options.get('reconcile'):
            self.reconciler.validate(options)
        plan = self.clouds.plan(options, config, cancel, progress) if is_cloud(options['source']) else make_plan(options, config, cancel, progress)
        if options.get('reconcile'):
            self.reconciler.prepare(plan)
        plan = self.generated.filter(plan, config)
        return self.reconciler.finish_plan(plan) if options.get('reconcile') else plan

    def scan_progress(self, job):
        def update(info):
            job['scan'] = info
            self.put_job(job)
        return update

    def execute(self, plan, config, playback, progress=lambda *a: None, cancel=lambda: False, journal=lambda *a: None):
        runner = self.clouds.execute if 'account' in plan else execute
        try:
            normal = dict(plan, actions=[a for a in plan['actions'] if a['type']!='retire'])
            update = lambda done, total, path: progress(done, len(plan['actions']), path)
            result = runner(normal, config, playback, update, cancel, journal, generated=self.generated)
            if plan['options'].get('reconcile'):
                result = self.reconciler.execute(plan, config, result, progress, cancel, journal)
            return result
        finally:
            self.generated.flush()

    def start_scan(self, options, regenerate=None):
        with self.lock:
            if self.busy:
                raise ValueError('已有任务正在运行，请等待或取消')
            job_id = uuid.uuid4().hex
            self.busy = job_id
            config = dict(self.config)
        job = {'id': job_id, 'status': 'scanning', 'created': time.time(), 'options': options, 'done': 0, 'total': 0}
        self.put_job(job)
        def work():
            try:
                plan = (self.generated.single_plan(regenerate, config) if regenerate else
                        self.make_plan(options, config, lambda: job_id in self.cancelled, self.scan_progress(job)))
                job.update(status='preview', plan=plan, counts=plan['counts'], warnings=plan['warnings'])
                if regenerate:
                    job['status'] = 'running'
                    self.put_job(job)
                    result = self.execute(plan, config, self.playback, ExecutionProgress(self, job), lambda: job_id in self.cancelled)
                    job.update(status='cancelled' if result['cancelled'] else ('partial' if result['errors'] else 'completed'), result=result)
            except Exception as e:
                job.update(status='cancelled' if job_id in self.cancelled else 'failed', error=str(e))
            finally:
                self.put_job(job)
                with self.lock:
                    self.busy = None
        self.pool.submit(work)
        return job

    def run(self, job_id):
        with self.lock:
            if self.busy:
                raise ValueError('已有任务正在运行')
            job = self.job(job_id)
            if job['status'] != 'preview':
                raise ValueError('此预览已执行或已失效，请重新扫描')
            self.busy = job_id
            job['status'] = 'running'
            self.put_job(job)
            config = dict(self.config)
        progress = ExecutionProgress(self, job)
        def work():
            try:
                result = self.execute(job['plan'], config, self.playback, progress,
                                 lambda: job_id in self.cancelled,
                                 lambda kind, value: self.journal(job_id, kind, value))
                job.update(status='cancelled' if result['cancelled'] else ('partial' if result['errors'] else 'completed'), result=result)
            except Exception as e:
                job.update(status='failed', error=str(e))
            finally:
                self.put_job(job)
                with self.lock:
                    self.busy = None
        self.pool.submit(work)
        return {'id': job_id}

    def restore(self, job_id):
        with self.lock:
            if self.busy:
                raise ValueError('请等待当前任务完成')
            job = self.job(job_id)
            if job.get('options', {}).get('reconcile'):
                self.busy = 'restore:' + job_id
            if 'account' in job['plan']:
                # Network requests must not hold the SQLite/state lock.
                self.busy = 'restore:' + job_id
        if job.get('options', {}).get('reconcile'):
            try:
                return self.reconciler.restore(job)
            finally:
                with self.lock:self.busy=None
        if 'account' in job['plan']:
            try:
                return self.clouds.restore(job_id)
            finally:
                with self.lock:
                    self.busy = None
        with self.lock:
            root = inside(job['plan']['options']['source'], self.config['source_roots'])
            trash_root = root / '.strm-trash' / job['plan']['id']
            rows = self.db.execute("SELECT value FROM journal WHERE job=? AND kind IN ('trash','pending_trash')", (job_id,)).fetchall()
            restored, errors, seen = 0, [], set()
            for row in rows:
                entry = json.loads(row[0])
                if entry['trash'] in seen:
                    continue
                seen.add(entry['trash'])
                try:
                    old = inside(entry['source'], [root])
                    trash = inside(entry['trash'], [trash_root])
                    if not trash.is_file():
                        continue
                    if old.exists():
                        raise ValueError('源位置已有文件，未覆盖')
                    old.parent.mkdir(parents=True, exist_ok=True)
                    trash.rename(old)
                    self.journal(job_id, 'restored', entry)
                    restored += 1
                except (ValueError, OSError) as e:
                    errors.append(str(e))
            return {'restored': restored, 'errors': errors}

class Handler(BaseHTTPRequestHandler):
    server_version = 'STRMBridge/' + __version__
    def log_message(self, format, *args):
        # Do not log capability URLs, credentials, or filenames.
        pass

    @property
    def state(self):
        return self.server.state

    def authorized(self):
        if os.environ.get('AUTH_DISABLED') == '1':
            return True
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
            token = jar['session'].value
            stamp, signature = token.split('.', 1)
            expected = hmac.new(self.state.password.encode(), stamp.encode(), hashlib.sha256).hexdigest()
            return abs(time.time() - int(stamp)) < 86400 * 7 and hmac.compare_digest(signature, expected)
        except (KeyError, ValueError, cookies.CookieError):
            return False

    def respond(self, data, code=200, content_type='application/json; charset=utf-8', headers=None):
        body = json.dumps(data, ensure_ascii=False).encode() if content_type.startswith('application/json') else data
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://qrcodeapi.115.com; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_PROPFIND(self):
        if webdav.is_path(urlsplit(self.path).path):
            return webdav.handle(self)
        return self.respond({'error': '目录不存在'}, 404)

    def do_OPTIONS(self):
        if webdav.is_path(urlsplit(self.path).path):
            return webdav.handle(self)
        return self.respond(b'', 200, 'text/plain', {'Allow': 'OPTIONS, GET, HEAD, POST'})

    def dav_write(self):
        if webdav.is_path(urlsplit(self.path).path):
            return webdav.handle(self)
        return self.respond({'error': '不支持此操作'}, 405)

    do_PUT = do_DELETE = do_MKCOL = do_MOVE = do_COPY = do_PROPPATCH = do_LOCK = do_UNLOCK = dav_write

    def do_GET(self):
        try:
            self.get_route()
        except (ValueError, OSError, KeyError) as e:
            self.respond({'error': str(e)}, 400)
        except Exception:
            self.respond({'error': '服务请求失败，请查看任务记录或检查连接'}, 500)

    def get_route(self):
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        if webdav.is_path(path):
            return webdav.handle(self)
        if path == '/health':
            return self.respond({'status': 'ok', 'version': __version__})
        if path.startswith('/emby-play/'):
            return emby.playback(self)
        if path.startswith('/play/'):
            parts = path.split('/')
            if len(parts) != 5 or not hmac.compare_digest(self.state.sign(parts[2]), parts[3]):
                return self.respond({'error': '播放链接无效'}, 403)
            with self.state.lock:
                row = self.state.db.execute('SELECT path FROM media WHERE id=?', (parts[2],)).fetchone()
            if not row:
                return self.respond({'error': '影片记录不存在'}, 404)
            try:
                if row[0].startswith('cloud:'):
                    location = self.state.clouds.resolve(json.loads(row[0][6:]), self.headers.get('User-Agent', ''))
                else:
                    p = inside(row[0], self.state.config['source_roots'])
                    location = resolve(p, dict(self.state.config), self.headers.get('User-Agent', ''))
                return self.respond(b'', 302, 'application/octet-stream', {'Location': location})
            except Exception as e:
                return self.respond({'error': str(e)}, 502)
        if path.startswith('/api/'):
            if not self.authorized():
                return self.respond({'error': '请登录'}, 401)
            if path == '/api/config':
                secret_keys = ('cd2_token', 'cd2_password', 'openlist_token', 'openlist_password')
                config = {k: v for k, v in self.state.config.items() if k not in secret_keys}
                config.update(cd2_token_set=bool(self.state.config.get('cd2_token')), cd2_password_set=bool(self.state.config.get('cd2_password')))
                config.update(openlist_token_set=bool(self.state.config.get('openlist_token')), openlist_password_set=bool(self.state.config.get('openlist_password')))
                return self.respond(dict(config=config, video_extensions=sorted(VIDEO), subtitle_extensions=sorted(SUB),
                                         webdav_auth_required=os.environ.get('AUTH_DISABLED') != '1', version=__version__))
            if path == '/api/appearance':
                return self.respond(self.state.wallpapers.status())
            if path == '/api/clouds':
                return self.respond({'providers': CATALOG, 'accounts': self.state.clouds.list_accounts()})
            if path == '/api/emby/status':
                return self.respond(emby.status(self.state))
            if path == '/api/appearance/bing':
                return self.respond(self.state.wallpapers.bing())
            if path.startswith('/api/appearance/image/'):
                data, mime = self.state.wallpapers.image(path.rsplit('/', 1)[-1])
                return self.respond(data, content_type=mime)
            if path == '/api/browse':
                kind = query.get('kind', ['source'])[0]
                if kind not in ('source', 'target', 'cloud_source', 'upload_source', 'upload_files', 'upload_target'):
                    raise ValueError('目录选择类型无效')
                roots = self.state.uploads.roots() if kind in ('upload_source', 'upload_files') else self.state.config['source_roots' if kind == 'source' else 'target_roots']
                requested = query.get('path', [''])[0]
                if kind in ('source', 'cloud_source', 'upload_target') and is_cloud(requested):
                    if kind in ('cloud_source','upload_target'):
                        from .cloud import parse_uri
                        account, _ = parse_uri(requested)
                        a = self.state.clouds.account(account)
                        if a['provider'] != '115' or a['credentials'].get('auth_mode') != 'cookie':
                            raise ValueError('请选择使用 Cookie / 扫码登录的 115 账号')
                    return self.respond(self.state.clouds.browse(requested))
                if not requested:
                    if kind in ('cloud_source','upload_target'):
                        cloud_dirs = [{'name': a['name'], 'path': a['source']} for a in self.state.clouds.list_accounts()
                                      if a['provider'] == '115' and a['credentials'].get('auth_mode') == 'cookie']
                        return self.respond({'path': '', 'directories': cloud_dirs})
                    cloud_dirs = [{'name': a['name'], 'path': a['source']} for a in self.state.clouds.list_accounts()] if kind == 'source' else []
                    return self.respond({'roots': roots, 'path': '', 'directories': cloud_dirs + [{'name': r, 'path': r} for r in roots]})
                if kind in ('cloud_source','upload_target'):
                    raise ValueError('请选择 115 网盘文件夹')
                p = inside(requested, roots)
                if not p.is_dir():
                    raise ValueError('目录不存在或挂载未就绪')
                if kind == 'upload_files':
                    offset = max(0, int(query.get('offset', ['0'])[0]))
                    with os.scandir(p) as iterator:
                        entries = sorted((e for e in iterator if not e.is_symlink() and e.name not in SKIP_DIRS
                                          and (e.is_dir(follow_symlinks=False) or e.is_file(follow_symlinks=False))),
                                         key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.casefold()))
                    rows = [{'name': e.name, 'path': str(p / e.name), 'folder': e.is_dir(follow_symlinks=False),
                             'size': 0 if e.is_dir(follow_symlinks=False) else e.stat(follow_symlinks=False).st_size}
                            for e in entries[offset:offset + 200]]
                    return self.respond({'roots': roots, 'path': str(p), 'parent': str(p.parent) if str(p) not in roots else '',
                                         'entries': rows, 'offset': offset, 'total': len(entries), 'more': offset + 200 < len(entries)})
                directories = [{'name': x.name, 'path': str(x)} for x in p.iterdir() if x.is_dir() and not x.is_symlink() and x.name not in SKIP_DIRS]
                return self.respond({'roots': roots, 'path': str(p), 'parent': str(p.parent) if str(p) not in roots else '', 'directories': sorted(directories, key=lambda d: d['name'].casefold())})
            if path == '/api/generated':
                return self.respond(self.state.generated.list(query.get('offset',['0'])[0],query.get('search',[''])[0]))
            if path == '/api/videos/scans':
                return self.respond(self.state.videos.list())
            if path.startswith('/api/videos/rows/'):
                return self.respond(self.state.videos.rows(path.rsplit('/',1)[-1],query.get('offset',['0'])[0],query.get('filter',['low'])[0]))
            if path.startswith('/api/videos/ids/'):
                identifier=path.rsplit('/',1)[-1]
                self.state.videos.get(identifier)
                with self.state.lock:
                    rows=self.state.db.execute("SELECT id FROM video_rows WHERE scan=? AND json_extract(value,'$.low')=1 AND json_extract(value,'$.operation') IS NULL LIMIT 5001",(identifier,)).fetchall()
                return self.respond({'ids':[row[0] for row in rows]})
            if path == '/api/jobs':
                return self.respond(self.state.jobs())
            if path == '/api/watches':
                return self.respond(self.state.watcher.list())
            if path == '/api/uploads':
                return self.respond(self.state.uploads.list('watch'))
            if path == '/api/uploads/manual':
                return self.respond(self.state.uploads.list('manual', 50))
            if path.startswith('/api/uploads/staging/'):
                return self.respond(self.state.uploads.browser_task(path.rsplit('/', 1)[-1]))
            if path.startswith('/api/jobs/'):
                offset = max(0, int(query.get('offset', ['0'])[0]))
                job_id = path.rsplit('/', 1)[-1]
                job = self.state.job(job_id, include_plan=False) if query.get('summary') == ['1'] else self.state.job_preview(job_id, offset)
                return self.respond(job)
            return self.respond({'error': '接口不存在'}, 404)
        name = 'index.html' if path == '/' else path.lstrip('/')
        if name not in ('index.html', 'app.js', 'confirmation.js', 'style.css', 'appearance.js', 'appearance.css', 'clouds.js', 'uploads.js', 'generated.js', 'cloud-tools.js', 'logo.svg'):
            return self.respond({'error': '页面不存在'}, 404)
        return self.respond((STATIC / name).read_bytes(), content_type=mimetypes.guess_type(name)[0] + '; charset=utf-8')

    def do_POST(self):
        if webdav.is_path(urlsplit(self.path).path):
            return webdav.handle(self)
        try:
            self.post_route()
        except (ValueError, OSError, KeyError, TypeError) as e:
            self.respond({'error': str(e)}, 400)
        except Exception:
            self.respond({'error': '操作失败，请检查连接或任务记录'}, 500)

    def post_route(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        # Fetch Metadata also authenticates browser same-origin requests when a local
        # browser gateway normalizes the Origin header by stripping its port.
        metadata_same_origin = (self.headers.get('Sec-Fetch-Site') == 'same-origin' and origin and
                                urlsplit(origin).hostname == urlsplit('http://' + host).hostname)
        if origin and urlsplit(origin).netloc != host and not metadata_same_origin:
            return self.respond({'error': '跨站请求已拒绝'}, 403)
        if self.path == '/api/appearance/upload':
            if not self.authorized():
                return self.respond({'error': '请登录'}, 401)
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_IMAGE:
                return self.respond({'error': '背景图片需要在 10MB 以内'}, 413)
            return self.respond(self.state.wallpapers.upload(self.rfile.read(size)))
        if self.path.startswith('/api/uploads/staging/') and self.path.endswith('/chunk'):
            if not self.authorized():
                return self.respond({'error': '请登录'}, 401)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                return self.respond({'error': '需要文件数据'}, 415)
            identifier = self.path.split('/')[4]
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8 * 1024 * 1024:
                self.close_connection = True
                return self.respond({'error': '每次传输数据需要在 8MB 以内'}, 413)
            try:
                return self.respond(self.state.uploads.browser_chunk(identifier,
                    int(self.headers.get('X-Upload-Index', '-1')), int(self.headers.get('X-Upload-Offset', '-1')), length, self.rfile))
            except Exception:
                self.close_connection = True
                raise
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond({'error': '需要 JSON 请求'}, 415)
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0 or length > 65536:
            return self.respond({'error': '请求过大'}, 413)
        data = json.loads(self.rfile.read(length) or b'{}')
        if self.path == '/api/login':
            with self.state.lock:
                if not hasattr(self.state, 'login_attempts'):
                    self.state.login_attempts = {}
                attempts = self.state.login_attempts.setdefault(self.client_address[0], [])
                attempts[:] = [t for t in attempts if time.time() - t < 60]
                if len(attempts) >= 10:
                    return self.respond({'error': '尝试过于频繁，请一分钟后再试'}, 429)
                attempts.append(time.time())
            if not hmac.compare_digest(str(data.get('password', '')), self.state.password):
                return self.respond({'error': '管理密码不正确'}, 401)
            stamp = str(int(time.time()))
            token = stamp + '.' + hmac.new(self.state.password.encode(), stamp.encode(), hashlib.sha256).hexdigest()
            return self.respond({'ok': True}, headers={'Set-Cookie': 'session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800'})
        if not self.authorized():
            return self.respond({'error': '请登录'}, 401)
        if self.path == '/api/logout':
            return self.respond({'ok': True}, headers={'Set-Cookie': 'session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict'})
        if self.path == '/api/generated/delete':
            return self.respond(self.state.generated.delete(data.get('ids')))
        if self.path == '/api/generated/regenerate':
            record = self.state.generated.get(data['id'])
            return self.respond(self.state.start_scan(record['options'], regenerate=data['id']), 202)
        if self.path == '/api/scan':
            return self.respond(self.state.start_scan(data), 202)
        if self.path == '/api/reconcile':
            options = dict(data, reconcile=True, cleanup=False, subtitle_mode='copy', mode='redirect')
            return self.respond(self.state.start_scan(options), 202)
        if self.path == '/api/videos/scan':
            return self.respond(self.state.videos.start(data), 202)
        if self.path == '/api/videos/move-preview':
            return self.respond(self.state.videos.move_preview(data))
        if self.path == '/api/videos/batch':
            return self.respond(self.state.videos.batch(data), 202)
        if self.path == '/api/videos/cancel':
            self.state.videos.get(data['id'])
            self.state.cancelled.add(data['id'])
            return self.respond({'ok':True})
        if self.path == '/api/clouds/login/start':
            return self.respond(self.state.cloud_login.start(data.get('provider')))
        if self.path == '/api/clouds/login/status':
            return self.respond(self.state.cloud_login.status(data.get('id')))
        if self.path in ('/api/clouds/save', '/api/clouds/delete', '/api/clouds/test'):
            if self.state.busy:
                raise ValueError('请等待当前任务完成')
            if self.path.endswith('/save'):
                return self.respond(self.state.clouds.save(data))
            if self.path.endswith('/delete'):
                return self.respond(self.state.clouds.delete(data['id']))
            return self.respond(self.state.clouds.test(data['id']))
        if self.path == '/api/watches':
            return self.respond(self.state.watcher.save(data))
        if self.path == '/api/uploads':
            return self.respond(self.state.uploads.save(data))
        if self.path == '/api/uploads/manual':
            return self.respond(self.state.uploads.manual(data), 202)
        if self.path == '/api/uploads/staging':
            return self.respond(self.state.uploads.browser_begin(data), 202)
        if self.path.startswith('/api/uploads/staging/') and self.path.endswith('/finish'):
            return self.respond(self.state.uploads.browser_finish(self.path.split('/')[4]), 202)
        if self.path in ('/api/uploads/manual/pause', '/api/uploads/manual/resume', '/api/uploads/manual/retry', '/api/uploads/manual/delete'):
            return self.respond(self.state.uploads.manual_control(data['id'], self.path.rsplit('/', 1)[-1]))
        if self.path in ('/api/uploads/check', '/api/uploads/retry', '/api/uploads/delete'):
            return self.respond(self.state.uploads.control(data['id'],
                retry=self.path.endswith('/retry'), delete=self.path.endswith('/delete')))
        if self.path == '/api/watches/delete':
            return self.respond(self.state.watcher.delete(data['id']))
        if self.path == '/api/watches/check':
            with self.state.lock:
                watch = next((w for w in self.state.watcher.list() if w['id'] == data['id']), None)
                if not watch:
                    raise ValueError('监控不存在')
                if not watch['enabled']:
                    raise ValueError('请先启用监控')
                watch['next_run'] = 0
                self.state.watcher.put(watch)
            return self.respond({'ok': True})
        if self.path == '/api/run':
            return self.respond(self.state.run(data['id']), 202)
        if self.path == '/api/cancel':
            self.state.cancelled.add(data['id'])
            return self.respond({'ok': True})
        if self.path == '/api/restore':
            return self.respond(self.state.restore(data['id']))
        if self.path == '/api/config':
            with self.state.lock:
                if self.state.busy:
                    raise ValueError('任务运行时不能修改设置')
                c = dict(self.state.config)
                for k in ('public_url', 'emby_url', 'webdav_enabled', 'provider', 'default_mode', 'openlist_url', 'openlist_user', 'openlist_password', 'openlist_token', 'url_template', 'cd2_url', 'cd2_user', 'cd2_password', 'cd2_token', 'mappings'):
                    if k in data:
                        c[k] = data[k]
                valid_url(c['public_url'])
                if not isinstance(c.get('webdav_enabled', False), bool):
                    raise ValueError('WebDAV 开关无效')
                if c.get('emby_url'):
                    valid_url(c['emby_url'])
                if c.get('provider') not in ('none', 'openlist', 'template', 'cd2'):
                    raise ValueError('直链连接方式无效')
                if c.get('default_mode') not in ('path', 'redirect'):
                    raise ValueError('默认播放入口无效')
                if c['provider'] == 'cd2':
                    valid_url(c.get('cd2_url', ''))
                elif c['provider'] == 'openlist':
                    valid_url(c.get('openlist_url', ''))
                elif c['provider'] == 'template':
                    if '{path}' not in c.get('url_template', ''):
                        raise ValueError('HTTP 地址模板需要包含 {path}')
                    valid_url(c['url_template'].replace('{path}', '/test'))
                if not isinstance(c.get('mappings'), list):
                    raise ValueError('目录对应设置无效')
                for m in c['mappings']:
                    inside(m['local'], c['source_roots'])
                    if not isinstance(m['cloud'], str) or not m['cloud'].startswith('/') or '..' in m['cloud'].split('/'):
                        raise ValueError('服务端路径必须以 / 开头，不能包含 ..')
                self.state.config = c
                self.state.save_config()
            return self.respond({'ok': True})
        if self.path in ('/api/test-connection', '/api/test-cd2'):
            if data.get('path'):
                p = inside(data['path'], self.state.config['source_roots'])
                location = resolve(p, dict(self.state.config), self.headers.get('User-Agent', ''))
                return self.respond({'ok': True, 'host': urlsplit(location).hostname, 'message': '已获得 302 直链（未下载正片）'})
            return self.respond({'ok': True, 'message': test_connection(dict(self.state.config))})
        return self.respond({'error': '接口不存在'}, 404)

def main():
    state = State(os.environ.get('DATA_DIR', '/data'))
    server = ThreadingHTTPServer((os.environ.get('BIND_HOST', '0.0.0.0'), int(os.environ.get('PORT', '18642'))), Handler)
    server.state = state
    state.watcher.start()
    state.uploads.start()
    print('STRM Bridge ' + __version__ + ' is ready. Admin password: ' + str(state.data / 'admin-password.txt'), flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
