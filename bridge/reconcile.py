"""Reconcile only recorded STRM outputs against a complete, fresh cloud snapshot."""
import hashlib
import json
from pathlib import Path

from .cloud import parse_uri
from .core import inside


class Reconciler:
    def __init__(self, state):
        self.state = state

    def validate(self, options, live=True):
        account, root = parse_uri(options['source'])
        if self.state.clouds.account(account)['provider'] != '115':
            raise ValueError('目录核对目前支持 115 网盘')
        if not live:
            return account
        with self.state.clouds.client(account) as client:
            # A stale/deleted folder must not be interpreted as an empty drive.
            if callable(getattr(client, 'directory_path', None)):
                client.directory_path(root)
            else:
                raise ValueError('目录核对需要使用 Cookie / 扫码登录的 115 账号')
        return account

    def records(self, options):
        target = str(Path(options['target']).resolve())
        with self.state.lock:
            rows = self.state.db.execute('SELECT value FROM generated UNION ALL SELECT value FROM generation_retry').fetchall()
        matching, roots = {}, {}
        for row in rows:
            r=json.loads(row[0]);o=r.get('options',{})
            if o.get('source')!=options['source'] or r.get('action',{}).get('type')!='strm':
                continue
            value=o['target']
            if value not in roots:roots[value]=str(Path(value).resolve())
            if roots[value]==target:matching[r['id']]=r
        return matching

    def prepare(self, plan):
        records = self.records(plan['options'])
        current = {a['remote']['id']: a for a in plan['actions'] if a['type'] == 'strm'}
        targets = {a['target']: a for a in current.values()}
        retired, renamed = [], 0
        for record in records.values():
            old = record['action']
            now = current.get(old['remote']['id'])
            if now and now['target'] == record['target']:
                continue
            if now:
                renamed += 1
            replacement = targets.get(record['target']) or now
            retired.append({'type': 'retire', 'source': record['source'], 'target': record['target'],
                'record_id': record['id'], 'content_hash': record['content_hash'],
                'remote': old['remote'], 'reason': '改名或移动' if now else '源文件已移出或删除',
                'replacement_id': self.state.generated.identifier(plan, replacement) if replacement else None,
                'replacement_target': replacement['target'] if replacement else None,
                'keep_target': record['target'] in targets})
        # Replacements at the same path (including swapped filenames) must check
        # the hash of the output actually occupying that path.
        owners = {r['target']: r for r in records.values()}
        for action in current.values():
            if action['target'] in owners:
                action['managed_hash'] = owners[action['target']]['content_hash']
        plan['retire_actions'] = retired
        plan['reconcile_summary'] = {'renamed': renamed, 'deleted': len(retired)-renamed}
        return plan

    def finish_plan(self, plan):
        retire = plan.pop('retire_actions', [])
        plan['counts']['updated'] = sum(a['type']=='strm' and bool(a.get('managed_hash')) for a in plan['actions'])
        plan['actions'].extend(retire)
        plan['counts']['retire'] = len(retire)
        plan['warnings'].append('核对仅同步此目录组合已记录的 STRM；失效入口移入目标目录的 .strm-sync-trash，可在任务记录恢复。')
        return plan

    def execute(self, plan, config, result, progress, cancel, journal):
        actions = [a for a in plan['actions'] if a['type']=='retire']
        result['retired'] = 0
        if cancel():
            result['cancelled'] = True
        if not actions or result['errors'] or result['cancelled']:
            return result
        # Recheck at execution time: an old preview or a restored cloud file
        # cannot cause removal based solely on its earlier absence.
        self.validate(plan['options'])
        fresh = self.state.clouds.plan(plan['options'], config, cancel)
        current = {a['remote']['id']: a for a in fresh['actions'] if a['type']=='strm'}
        root = inside(plan['options']['target'], config['target_roots'])
        for index, action in enumerate(actions):
            if cancel():
                result['cancelled'] = True
                break
            try:
                identifier = action['record_id']
                record = self.state.generated.get(identifier)
                if record['content_hash'] != action['content_hash']:
                    raise ValueError('成功记录已变更，请重新核对')
                now = current.get(action['remote']['id'])
                if now and now['target'] == action['target']:
                    raise ValueError('云端文件已恢复，保留原 STRM，请重新核对')
                if action['replacement_id']:
                    replacement = self.state.generated.get(action['replacement_id'])
                    new = inside(action['replacement_target'], [root])
                    if not new.is_file() or hashlib.sha256(new.read_bytes()).hexdigest() != replacement['content_hash']:
                        raise ValueError('新的 STRM 尚未校验成功，保留原入口')
                    if now and now['target'] != action['replacement_target'] and not action['keep_target']:
                        raise ValueError('云端路径再次变化，保留原入口，请重新核对')
                target = inside(action['target'], [root])
                entry = {'record': record, 'source': str(target), 'trash': None}
                if not action['keep_target'] and target.exists():
                    if not target.is_file() or target.stat().st_size > 16384 or hashlib.sha256(target.read_bytes()).hexdigest() != action['content_hash']:
                        raise ValueError('STRM 已被手动修改，保留文件')
                    trash = inside(root / '.strm-sync-trash' / plan['id'] / (str(target.relative_to(root))+'.bak'), [root])
                    trash.parent.mkdir(parents=True, exist_ok=True)
                    if trash.exists():
                        raise ValueError('核对回收记录已存在，请重新核对')
                    entry['trash'] = str(trash)
                    journal('sync_pending', entry)
                    target.rename(trash)
                journal('sync_retired', entry)
                with self.state.lock:
                    self.state.db.execute('DELETE FROM generated WHERE id=?', (identifier,))
                    self.state.db.execute('DELETE FROM generation_retry WHERE id=?', (identifier,))
                    self.state.db.commit()
                result['retired'] += 1
                # Remove only empty, formerly managed directories; never recurse.
                parent = target.parent
                while parent != root and parent.is_relative_to(root):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            except (ValueError, OSError) as exc:
                result['errors'].append({'path': action['target'], 'message': str(exc)})
            progress(len(plan['actions'])-len(actions)+index+1, len(plan['actions']), action['target'])
        return result

    def restore(self, job):
        root = inside(job['plan']['options']['target'], self.state.config['target_roots'])
        trash_root = root / '.strm-sync-trash' / job['plan']['id']
        with self.state.lock:
            rows = self.state.db.execute("SELECT value FROM journal WHERE job=? AND kind IN ('sync_pending','sync_retired')", (job['id'],)).fetchall()
        restored, errors, seen = 0, [], set()
        for row in rows:
            entry = json.loads(row[0])
            if not entry.get('trash') or entry['trash'] in seen:
                continue
            seen.add(entry['trash'])
            try:
                source, trash = inside(entry['source'], [root]), inside(entry['trash'], [trash_root])
                if not trash.exists():
                    continue
                if source.exists():
                    raise ValueError('原位置已有文件，未覆盖：'+str(source))
                record = entry['record']
                if hashlib.sha256(trash.read_bytes()).hexdigest() != record['content_hash']:
                    raise ValueError('回收副本已变更，未恢复')
                source.parent.mkdir(parents=True, exist_ok=True)
                trash.rename(source)
                self.state.generated.record({'options':record['options'],'config_fingerprint':record['config_fingerprint'],'account':record['account']}, record['action'], source.read_bytes())
                restored += 1
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
        self.state.generated.flush()
        return {'restored':restored,'errors':errors}
