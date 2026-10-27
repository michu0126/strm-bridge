"""Account storage, cloud directory plans and recoverable remote moves."""
from contextlib import contextmanager
from collections import Counter, OrderedDict
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

from .cloud_providers import PROVIDERS, CATALOG, SECRET_FIELDS, CloudError, item, create_provider
from .core import inside, media_extensions, SUB, SKIP_DIRS, CLEANUP_LIMIT, subtitle_match, subtitle_tail, exclusive_write, config_fingerprint
from .resolver import valid_url
from .progress import ScanProgress


def is_cloud(source):
    return isinstance(source, str) and source.startswith('cloud://')


def uri(account, identifier):
    return 'cloud://' + account + '/' + quote(str(identifier), safe='')


def parse_uri(source):
    p = urlsplit(source)
    if p.scheme != 'cloud' or not re.fullmatch('[a-f0-9]{32}', p.netloc) or not p.path or p.query or p.fragment:
        raise CloudError('请选择有效的网盘文件夹')
    return p.netloc, unquote(p.path[1:])


def safe_name(name):
    if not name or name in ('.', '..') or any(c in name for c in '/\\\x00'):
        raise CloudError('网盘包含无法保存到媒体库的文件名')
    if any(ord(c) < 32 for c in name):
        raise CloudError('网盘文件名包含控制字符')
    return name


def signature(obj):
    return [obj['id'], obj['size'], obj['modified']]


def scan_names(rows):
    """Keep every same-named cloud item with stable, distinct local paths."""
    counts = Counter(obj['name'] for obj in rows)
    used = set(counts)
    names = {}
    for obj in sorted(rows, key=lambda obj: obj['id']):
        name = obj['name']
        if counts[name] > 1:
            suffix = '' if obj['folder'] else PurePosixPath(name).suffix
            stem = name[:-len(suffix)] if suffix else name
            tag = hashlib.sha256(obj['id'].encode()).hexdigest()[:12]
            candidate = stem + '.' + tag + suffix
            number = 1
            while candidate in used:
                candidate = stem + '.' + tag + '.' + str(number) + suffix
                number += 1
            used.add(candidate)
            name = candidate
        names[obj['id']] = name
    return names


class DownloadRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        valid_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            for key in ('Cookie', 'Authorization'):
                redirected.remove_header(key)
        return redirected


class Clouds:
    def __init__(self, state):
        self.state = state
        self.path = state.data / 'cloud-accounts.json'
        self.lock = threading.RLock()
        self.account_locks = {}
        self.clients = {}
        self.accounts = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        with state.lock:
            state.db.execute('CREATE TABLE IF NOT EXISTS cloud_nodes(account TEXT,id TEXT,value TEXT,PRIMARY KEY(account,id))')
            state.db.execute('CREATE TABLE IF NOT EXISTS scan_directories(scan TEXT,parent TEXT,checked REAL,value TEXT,PRIMARY KEY(scan,parent))')
            state.db.commit()

    def persist(self):
        with self.lock:
            p = self.path.with_suffix('.tmp')
            p.write_text(json.dumps(self.accounts, ensure_ascii=False, indent=2), encoding='utf-8')
            p.chmod(0o600)
            p.replace(self.path)

    def public(self, account):
        a = copy.deepcopy(account)
        credentials = a.pop('credentials')
        a['credentials'] = {k: v for k, v in credentials.items() if k not in SECRET_FIELDS and k != 'expires_at'}
        a['configured'] = [k for k in SECRET_FIELDS if credentials.get(k)]
        a['source'] = uri(a['id'], a['root'])
        return a

    def list_accounts(self):
        with self.lock:
            return [self.public(a) for a in self.accounts.values()]

    def account(self, identifier):
        with self.lock:
            if identifier not in self.accounts:
                raise CloudError('网盘账号不存在，请重新添加或选择')
            return self.accounts[identifier]

    @contextmanager
    def client(self, identifier):
        with self.lock:
            lock = self.account_locks.setdefault(identifier, threading.RLock())
        with lock:
            a = self.account(identifier)
            if identifier not in self.clients:
                self.clients[identifier] = create_provider(a['provider'], a['credentials'], self.persist)
            yield self.clients[identifier]

    def save(self, data):
        identifier = data.get('id') or uuid.uuid4().hex
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            raise CloudError('网盘账号 ID 无效')
        with self.lock:
            lock = self.account_locks.setdefault(identifier, threading.RLock())
        with lock, self.lock:
            old = self.accounts.get(identifier)
            if data.get('id') and not old:
                raise CloudError('网盘账号不存在')
            provider = data.get('provider', old['provider'] if old else '')
            if provider not in PROVIDERS or (old and old['provider'] != provider):
                raise CloudError('网盘类型无效；不同类型请另行添加')
            spec = next(x for x in CATALOG if x['id'] == provider)
            credentials = dict(old['credentials']) if old else {}
            incoming = data.get('credentials', {})
            mode = incoming.get('auth_mode') or credentials.get('auth_mode') or ('cookie' if provider == '189' or incoming.get('cookie') else 'open')
            method = next((m for m in spec['methods'] if m['id'] == mode), None)
            if not method:
                raise CloudError('请选择有效的登录方式')
            previous_mode = credentials.get('auth_mode', 'cookie' if provider == '189' else 'open')
            if mode != previous_mode:
                credentials = {}
            if any(incoming.get(k) and incoming[k] != credentials.get(k) for k in ('refresh_token', 'client_id', 'client_secret')) and not incoming.get('access_token'):
                credentials.pop('access_token', None)
                credentials.pop('expires_at', None)
                if provider == 'aliyun' and not incoming.get('drive_id'):
                    credentials.pop('drive_id', None)
            if any(incoming.get(k) and incoming[k] != credentials.get(k) for k in ('username', 'password')):
                credentials.pop('web_token', None)
            for key in method['fields']:
                value = data.get('credentials', {}).get(key)
                if value:
                    credentials[key] = str(value).strip()
            credentials['auth_mode'] = mode
            if credentials.get('cookie'):
                from .cloud_cookie import normalize_cookie
                credentials['cookie'] = normalize_cookie(credentials['cookie'])
            if credentials.get('web_token', '').lower().startswith('bearer '):
                credentials['web_token'] = credentials['web_token'][7:].strip()
            if data.get('credentials', {}).get('access_token'):
                credentials.pop('expires_at', None)
            valid = bool(credentials.get('access_token'))
            if mode == 'cookie':
                valid = bool(credentials.get('cookie'))
                if valid and provider == '115':
                    keys = {p.partition('=')[0].strip() for p in credentials['cookie'].split(';')}
                    valid = {'UID', 'CID', 'SEID'} <= keys
                    if not valid:
                        raise CloudError('115 Cookie 需要包含 UID、CID、SEID')
            elif mode == 'online':
                valid = bool(credentials.get('refresh_token'))
            elif mode in ('password', 'web'):
                valid = bool(credentials.get('web_token')) or all(credentials.get(k) for k in ('username', 'password'))
            elif provider == '115':
                valid = valid or bool(credentials.get('refresh_token'))
            elif provider in ('baidu', 'aliyun'):
                valid = valid or all(credentials.get(k) for k in ('refresh_token', 'client_id', 'client_secret'))
            elif provider == '123':
                valid = valid or all(credentials.get(k) for k in ('client_id', 'client_secret'))
            elif provider == '189':
                valid = bool(credentials.get('cookie'))
            if not valid:
                raise CloudError('请填写完整的网盘授权信息')
            name = str(data.get('name', '')).strip() or spec['name']
            root = str(data.get('root') or spec['root']).strip()
            account = dict(id=identifier, provider=provider, name=name[:80], root=root, credentials=credentials,
                           revision=(old['revision'] + 1 if old else 1), status='untested', error='')
            self.accounts[identifier] = account
            self.clients.pop(identifier, None)
            self.persist()
            result = self.public(account)
        self.remember(identifier, dict(item(root, name, True), parent=None, path='/'))
        return result

    def delete(self, identifier):
        self.account(identifier)
        with self.client(identifier), self.lock:
            self.accounts.pop(identifier)
            self.clients.pop(identifier, None)
            self.persist()
        with self.state.lock:
            for w in self.state.watcher.list():
                if w['options']['source'].startswith('cloud://' + identifier + '/'):
                    w.update(enabled=False, last_error='网盘账号已移除')
                    self.state.watcher.put(w)
        if hasattr(self.state, 'uploads'):
            self.state.uploads.account_removed(identifier)
        return {'ok': True}

    def remember(self, account, obj):
        with self.state.lock:
            self.state.db.execute('INSERT OR REPLACE INTO cloud_nodes VALUES(?,?,?)', (account, obj['id'], json.dumps(obj, ensure_ascii=False)))
            self.state.db.commit()

    def node(self, account, identifier):
        a = self.account(account)
        if identifier == a['root']:
            return dict(item(identifier, a['name'], True), parent=None, path='/')
        with self.state.lock:
            row = self.state.db.execute('SELECT value FROM cloud_nodes WHERE account=? AND id=?', (account, identifier)).fetchone()
        if not row:
            raise CloudError('请从网盘根目录逐级选择文件夹')
        return json.loads(row[0])

    def entries(self, account, parent, client, cancel=lambda: False, progress=lambda n: None, allow_duplicates=False):
        directory = self.node(account, parent)
        if not directory['folder']:
            raise CloudError('所选位置不是文件夹')
        rows, names, ids = [], set(), set()
        for obj in client.list(parent):
            if cancel():
                raise CloudError('任务已取消')
            safe_name(obj['name'])
            if obj['id'] in ids:
                raise CloudError('云端目录分页返回了重复 ID，请重新扫描')
            if obj['name'] in names and not allow_duplicates:
                raise CloudError('云端目录包含重名条目')
            names.add(obj['name'])
            ids.add(obj['id'])
            obj.update(parent=parent, path=directory['path'].rstrip('/') + '/' + obj['name'])
            rows.append(obj)
            if len(rows) % 200 == 0:
                progress(len(rows))
        with self.state.lock:
            self.state.db.executemany('INSERT OR REPLACE INTO cloud_nodes VALUES(?,?,?)',
                                     [(account, obj['id'], json.dumps(obj, ensure_ascii=False)) for obj in rows])
            self.state.db.commit()
        return rows

    def scan_entries(self, key, account, parent, cancel, progress):
        # Only unfinished scans are resumed. A completed scan removes its checkpoints,
        # so the next manual scan or monitor pass always checks the live drive again.
        with self.state.lock:
            row = self.state.db.execute('SELECT value FROM scan_directories WHERE scan=? AND parent=? AND checked>?',
                                        (key, parent, time.time() - 86400)).fetchone()
        if row:
            return json.loads(row[0]), True
        if cancel():
            raise CloudError('任务已取消')
        with self.client(account) as client:
            rows = self.entries(account, parent, client, cancel, progress, allow_duplicates=True)
        with self.state.lock:
            self.state.db.execute('INSERT OR REPLACE INTO scan_directories VALUES(?,?,?,?)',
                                  (key, parent, time.time(), json.dumps(rows, ensure_ascii=False)))
            self.state.db.commit()
        return rows, False

    def browse(self, source):
        account, identifier = parse_uri(source)
        with self.client(account) as client:
            directory = self.node(account, identifier)
            rows = self.entries(account, identifier, client, allow_duplicates=True)
        return {'path': source, 'label': self.account(account)['name'] + directory['path'],
                'parent': uri(account, directory['parent']) if directory['parent'] is not None else '',
                'directories': [{'name': x['name'], 'path': uri(account, x['id'])} for x in sorted(rows, key=lambda x: x['name'].casefold()) if x['folder'] and x['name'] not in SKIP_DIRS],
                'files': len([x for x in rows if not x['folder']])}

    def test(self, identifier):
        a = self.account(identifier)
        try:
            result = self.browse(uri(identifier, a['root']))
            with self.lock:
                a.update(status='connected', error='', checked=time.time())
                self.persist()
            return {'ok': True, 'message': '已连接，可以浏览云端目录', 'directories': len(result['directories']), 'files': result['files']}
        except Exception as e:
            with self.lock:
                a.update(status='error', error=str(e), checked=time.time())
                self.persist()
            raise

    def plan(self, options, config, cancel=lambda: False, progress=lambda info: None):
        account, root = parse_uri(options['source'])
        dst = inside(options['target'], config['target_roots'])
        if not dst.is_dir():
            raise CloudError('请选择已有的媒体库文件夹')
        if options.get('kind') not in ('movie', 'tv', 'jav') or options.get('subtitle_mode', 'copy') not in ('copy', 'move'):
            raise CloudError('媒体类型或字幕规则无效')
        if options.get('cleanup') and options['kind'] != 'jav':
            raise CloudError('仅 JAV 支持小文件清理')
        valid_url(config['public_url'])
        ext = media_extensions(options.get('extra_extensions', ''))
        files, visited, todo = {}, set(), [(root, PurePosixPath('.'))]
        revision = self.account(account)['revision']
        key = hashlib.sha256((account + ':' + str(revision) + ':' + root).encode()).hexdigest()
        report = ScanProgress(progress)
        report.update(force=True)
        cached = 0
        seen_files = 0
        while todo:
            if cancel():
                raise CloudError('任务已取消')
            parent, relative = todo.pop()
            if parent in visited:
                raise CloudError('云端目录出现循环，请重新扫描')
            report.update(current=str(relative), pending=len(todo) + 1)
            rows, reused = self.scan_entries(key, account, parent, cancel,
                lambda n: report.update(current=str(relative), entries_in_directory=n))
            cached += int(reused)
            visited.add(parent)
            names = scan_names(rows)
            for obj in rows:
                p = relative / names[obj['id']]
                if obj['folder']:
                    if obj['name'] not in SKIP_DIRS:
                        todo.append((obj['id'], p))
                else:
                    seen_files += 1
                    suffix = p.suffix.lower().lstrip('.')
                    if options.get('cleanup') or suffix in ext or (options['kind'] != 'jav' and suffix in SUB):
                        files[p] = obj
            report.update(directories=len(visited), files=seen_files, pending=len(todo), cached_directories=cached)
        if self.account(account)['revision'] != revision:
            raise CloudError('网盘账号已变更，请重新扫描')
        videos = [p for p in files if p.suffix.lower().lstrip('.') in ext]
        report.update(force=True, phase='planning', videos=len(videos), current='整理生成清单')
        trash = []
        if options.get('cleanup'):
            parents = {p.parent for p in videos}
            trash = [p for p, obj in files.items() if p.parent in parents and obj['size'] < CLEANUP_LIMIT]
            trash_set = set(trash)
            videos = [p for p in videos if p not in trash_set]
        actions, warnings, used, targets = [], [], set(), {}

        def action(kind, p, target=None):
            obj = files[p]
            a = {'type': kind, 'source': self.account(account)['name'] + obj['path'], 'remote': obj, 'signature': signature(obj)}
            if target is not None:
                a['target'] = str(target)
            return a

        for p in videos:
            if cancel():
                raise CloudError('任务已取消')
            report.update(current=str(p))
            out = inside(dst / str(p.with_suffix('.strm')), [dst])
            if out in used:
                out = out.with_name(p.name + '.strm')
            if out in used:
                raise CloudError('目标文件名冲突')
            used.add(out); targets[p] = out
            actions.append(action('strm', p, out))
        if options['kind'] != 'jav':
            by_parent = {}
            for video in videos:
                by_parent.setdefault(video.parent, []).append(video)
            for p in files:
                if cancel():
                    raise CloudError('任务已取消')
                if p.suffix.lower().lstrip('.') not in SUB:
                    continue
                parent = p.parent.parent if p.parent.name.lower() in ('subs', 'subtitles', '字幕') else p.parent
                video = subtitle_match(p, list(dict.fromkeys(by_parent.get(p.parent, []) + by_parent.get(parent, []))))
                if video is None:
                    warnings.append('字幕匹配不唯一，已跳过：' + str(p))
                    continue
                out = targets[video].parent / (targets[video].stem + subtitle_tail(p, video))
                if out in used:
                    group = hashlib.sha256(str(p.with_suffix('')).encode()).hexdigest()[:6]
                    out = out.with_name(targets[video].stem + '.' + group + subtitle_tail(p, video))
                if out in used:
                    raise CloudError('字幕文件名冲突')
                used.add(out)
                actions.append(action(options.get('subtitle_mode', 'copy'), p, out))
        actions.extend(action('trash', p) for p in trash)
        with self.state.lock:
            self.state.db.execute('DELETE FROM scan_directories WHERE scan=? OR checked<?', (key, time.time() - 86400))
            self.state.db.commit()
        report.update(force=True, phase='ready', current='', pending=0)
        return {'id': uuid.uuid4().hex, 'created': time.time(), 'account': account, 'account_revision': revision,
                'options': dict(options, target=str(dst), mode='redirect'), 'actions': actions, 'warnings': warnings,
                'counts': {k: sum(a['type'] == k for a in actions) for k in ('strm', 'copy', 'move', 'trash')},
                'config_fingerprint': config_fingerprint(config)}

    def download(self, client, obj, target, cancel):
        url, headers = client.link(obj, 'STRMBridge/2.0')
        valid_url(url)
        if obj['size'] > 512 * 1024 * 1024:
            raise CloudError('外挂字幕超过 512MB，请手动整理')
        def write(f):
            total = 0
            try:
                with build_opener(ProxyHandler({}), DownloadRedirect()).open(Request(url, headers=headers), timeout=45) as r:
                    while True:
                        if cancel():
                            raise CloudError('任务已取消')
                        block = r.read(256 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > 512 * 1024 * 1024 or total > obj['size']:
                            raise CloudError('字幕大小与云端记录不符')
                        f.write(block)
                if total != obj['size']:
                    raise CloudError('字幕下载不完整，已保留云端原文件')
            except (HTTPError, URLError, TimeoutError):
                raise CloudError('字幕下载失败，已保留云端原文件') from None
        exclusive_write(target, write)

    def directory(self, account, client, parent, name):
        matches = [r for r in self.entries(account, parent, client) if r['name'] == name]
        if matches:
            if not matches[0]['folder']:
                raise CloudError('回收站目录名称被文件占用')
            return matches[0]['id']
        identifier = str(client.mkdir(parent, name))
        base = self.node(account, parent)
        self.remember(account, dict(item(identifier, name, True), parent=parent, path=base['path'].rstrip('/') + '/' + name))
        return identifier

    def quarantine(self, account, client, obj, root, plan_id, journal):
        current = next((r for r in self.entries(account, obj['parent'], client) if r['id'] == obj['id']), None)
        if current is None or signature(current) != signature(obj):
            raise CloudError('云端文件已变更，请重新扫描')
        trash = self.directory(account, client, root, '.strm-trash')
        batch = self.directory(account, client, trash, plan_id)
        slot = self.directory(account, client, batch, hashlib.sha256(obj['id'].encode()).hexdigest()[:24])
        entry = {'account': account, 'original': obj, 'trash_parent': slot}
        journal('cloud_pending_trash', entry)
        moved = client.move(obj, slot)
        entry['moved'] = moved
        journal('cloud_trash', entry)

    def execute(self, plan, config, playback, progress=lambda *a: None, cancel=lambda: False, journal=lambda *a: None):
        account = plan['account']
        if config_fingerprint(config) != plan['config_fingerprint'] or self.account(account)['revision'] != plan['account_revision']:
            raise CloudError('设置或网盘账号已变更，请重新预览')
        if time.time() - plan['created'] > 3600:
            raise CloudError('预览已过期，请重新扫描')
        _, root = parse_uri(plan['options']['source'])
        dst = inside(plan['options']['target'], config['target_roots'])
        result = {'created': 0, 'skipped': 0, 'subtitles': 0, 'trashed': 0, 'errors': [], 'cancelled': False}
        checked = OrderedDict()
        for i, action in enumerate(plan['actions']):
            if cancel():
                result['cancelled'] = True
                break
            obj = action['remote']
            try:
                with self.client(account) as client:
                    if self.account(account)['revision'] != plan['account_revision']:
                        raise CloudError('网盘账号已变更，请重新扫描')
                    kind = action['type']
                    if kind == 'trash':
                        if result['errors']:
                            raise CloudError('生成发生错误，本次清理已停止')
                        if plan['options']['kind'] != 'jav' or not plan['options'].get('cleanup') or obj['size'] >= CLEANUP_LIMIT:
                            raise CloudError('不允许清理此文件')
                        self.quarantine(account, client, obj, root, plan['id'], journal)
                        result['trashed'] += 1
                    else:
                        if obj['parent'] not in checked:
                            if len(checked) >= 32:
                                checked.popitem(last=False)
                            checked[obj['parent']] = {r['id']: r for r in self.entries(account, obj['parent'], client, cancel, allow_duplicates=True)}
                        current = checked[obj['parent']].get(obj['id'])
                        if current is None or signature(current) != action['signature']:
                            raise CloudError('云端文件已变更，请重新扫描')
                        target = inside(action['target'], [dst])
                        if kind == 'strm':
                            data = (playback({'account': account, 'object': obj}) + '\n').encode()
                            if target.exists():
                                if target.stat().st_size == len(data) and target.read_bytes() == data:
                                    result['skipped'] += 1
                                else:
                                    raise CloudError('目标文件已存在且内容不同，未覆盖')
                            else:
                                exclusive_write(target, lambda f: f.write(data))
                                result['created'] += 1
                        elif kind in ('copy', 'move'):
                            if target.exists():
                                # Never remove a source on the strength of a same-sized local file.
                                result['skipped'] += 1
                                if kind == 'move':
                                    raise CloudError('字幕目标已存在，已保留云端原文件')
                            else:
                                self.download(client, obj, target, cancel)
                                result['subtitles'] += 1
                                if kind == 'move':
                                    self.quarantine(account, client, obj, root, plan['id'], journal)
                        else:
                            raise CloudError('未知操作')
            except Exception as e:
                result['errors'].append({'path': action['source'], 'message': str(e)})
            progress(i + 1, len(plan['actions']), action['source'])
        return result

    def restore(self, job_id):
        with self.state.lock:
            rows = self.state.db.execute("SELECT value FROM journal WHERE job=? AND kind IN ('cloud_pending_trash','cloud_trash')", (job_id,)).fetchall()
        entries = {}
        for row in rows:
            e = json.loads(row[0])
            entries[(e['account'], e['original']['id'])] = e
        restored, errors = 0, []
        for (account, _), entry in entries.items():
            obj = entry['original']
            try:
                with self.client(account) as client:
                    candidates = [r for r in self.entries(account, entry['trash_parent'], client) if r['name'] == obj['name'] and r['size'] == obj['size']]
                    if not candidates:
                        continue
                    if len(candidates) != 1:
                        raise CloudError('回收站内容不唯一，未移动')
                    if any(r['name'] == obj['name'] for r in self.entries(account, obj['parent'], client)):
                        raise CloudError('云端原位置已有同名文件，未覆盖')
                    client.move(candidates[0], obj['parent'])
                    self.state.journal(job_id, 'cloud_restored', entry)
                    restored += 1
            except Exception as e:
                errors.append(str(e))
        return {'restored': restored, 'errors': errors}

    def resolve(self, reference, agent):
        # Download URLs may be bound to the player's UA, including its absence.
        agent = agent or ''
        with self.client(reference['account']) as client:
            url, headers = client.link(reference['object'], agent)
        required = headers.get('User-Agent')
        if required is not None and required != agent:
            raise CloudError('此网盘要求播放器 User-Agent 为 ' + required)
        if any(k.lower() != 'user-agent' for k in headers):
            raise CloudError('此网盘下载需要额外请求头，无法通过普通 302 传递')
        return valid_url(url)
