"""Persistent successful outputs; deleting a record requests regeneration, not deletion."""
import copy
import hashlib
import json
from pathlib import Path
import time
import uuid

from .core import config_fingerprint, inside, signature, exclusive_write


class GeneratedRecords:
    def __init__(self, state):
        self.state = state
        with state.lock:
            state.db.executescript('''CREATE TABLE IF NOT EXISTS generated(
                id TEXT PRIMARY KEY,target TEXT NOT NULL,value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS generated_target ON generated(target);
                CREATE TABLE IF NOT EXISTS generation_retry(id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generation_meta(key TEXT PRIMARY KEY,value TEXT);''')

    @staticmethod
    def identifier(plan, action):
        source = ('cloud:' + plan['account'] + ':' + str(action['remote']['id'])) if 'remote' in action else action['source']
        return hashlib.sha256((source + '\0' + action['target']).encode()).hexdigest()

    def flush(self):
        with self.state.lock:
            self.state.db.commit()

    def record(self, plan, action, data):
        target = Path(action['target'])
        value = {'id': self.identifier(plan, action), 'source': action['source'], 'target': str(target),
                 'created': time.time(), 'kind': plan['options'].get('kind', 'movie'),
                 'options': plan['options'], 'action': {k:v for k,v in action.items() if k != 'managed_hash'},
                 'config_fingerprint': plan['config_fingerprint'], 'target_signature': signature(target),
                 'content_hash': hashlib.sha256(data).hexdigest()}
        if 'account' in plan:
            value['account'] = plan['account']
        with self.state.lock:
            self.state.db.execute('INSERT OR REPLACE INTO generated VALUES(?,?,?)', (value['id'], str(target), json.dumps(value, ensure_ascii=False)))
            self.state.db.execute('DELETE FROM generation_retry WHERE id=?', (value['id'],))

    def filter(self, plan, config):
        with self.state.lock:
            rows = self.state.db.execute('SELECT id,value FROM generated').fetchall()
            retries = self.state.db.execute('SELECT id,value FROM generation_retry').fetchall()
        records = {key:json.loads(value) for key,value in rows}
        removed = {key:json.loads(value) for key,value in retries}
        actions, skipped = [], 0
        for action in plan['actions']:
            if action['type'] != 'strm':
                actions.append(action)
                continue
            key = self.identifier(plan, action)
            record = records.get(key)
            target = Path(action['target'])
            same_output = False
            if record:
                try:
                    same_output = signature(target) == record['target_signature']
                except OSError:
                    pass
                source_unchanged = action['signature'] == record['action']['signature']
                if action.get('remote',{}).get('sha1'):
                    source_unchanged = source_unchanged and action['remote']['sha1'].upper() == record['action'].get('remote',{}).get('sha1','').upper()
                if same_output and source_unchanged and record['config_fingerprint'] == plan['config_fingerprint']:
                    skipped += 1
                    continue
            managed = record or removed.get(key)
            if managed:
                action['managed_hash'] = managed['content_hash']
            actions.append(action)
        plan['actions'] = actions
        plan['counts'] = {k:sum(a['type']==k for a in actions) for k in ('strm','copy','move','trash','delete')}
        plan['counts']['recorded'] = skipped
        return plan

    def list(self, offset=0, search=''):
        search = str(search)[:200]
        where, params = ('WHERE instr(lower(value),lower(?))>0', [search]) if search else ('', [])
        with self.state.lock:
            total = self.state.db.execute('SELECT count(*) FROM generated '+where, params).fetchone()[0]
            rows = self.state.db.execute('SELECT value FROM generated '+where+' ORDER BY rowid DESC LIMIT 100 OFFSET ?', params+[max(0,int(offset))]).fetchall()
        # Internal action metadata stays on the server.
        return {'total':total,'offset':max(0,int(offset)), 'records':[
            {k:r[k] for k in ('id','source','target','created','kind')} for r in map(lambda row:json.loads(row[0]), rows)]}

    def get(self, identifier):
        with self.state.lock:
            row = self.state.db.execute('SELECT value FROM generated WHERE id=? UNION ALL SELECT value FROM generation_retry WHERE id=? LIMIT 1', (identifier,identifier)).fetchone()
        if not row:
            raise ValueError('生成记录不存在')
        return json.loads(row[0])

    def delete(self, identifiers):
        if not isinstance(identifiers,list) or not 1 <= len(identifiers) <= 1000 or any(not isinstance(i,str) for i in identifiers):
            raise ValueError('请选择要删除的生成记录')
        with self.state.lock:
            if self.state.busy:
                raise ValueError('请等待当前生成任务完成后再删除记录')
            count = 0
            for key in set(identifiers):
                row = self.state.db.execute('SELECT value FROM generated WHERE id=?', (key,)).fetchone()
                if row:
                    self.state.db.execute('INSERT OR REPLACE INTO generation_retry VALUES(?,?)', (key,row[0]))
                    self.state.db.execute('DELETE FROM generated WHERE id=?', (key,))
                    count += 1
            self.state.db.commit()
        return {'deleted':count}

    def write(self, target, data, action):
        if not target.exists():
            exclusive_write(target, lambda f:f.write(data))
            return 'created'
        if target.stat().st_size > 16384:
            raise ValueError('已有 STRM 内容不同，已保留原文件')
        previous = target.read_bytes()
        if previous == data and not action.get('managed_hash'):
            return 'skipped'
        expected = action.get('managed_hash')
        if not expected or hashlib.sha256(previous).hexdigest() != expected:
            if previous == data:
                return 'skipped'
            raise ValueError('目标文件已被修改，已保留原文件')
        backup = self.state.data / 'strm-backups' / (uuid.uuid4().hex+'.strm')
        backup.parent.mkdir(parents=True,exist_ok=True)
        backup.write_bytes(previous)
        temp = target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temp.write_bytes(data)
            # Refuse replacing an output changed since the preview.
            if target.read_bytes() != previous:
                raise ValueError('目标文件在生成期间发生变化，请重新预览')
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        return 'created'

    def single_plan(self, identifier, config):
        record = self.get(identifier)
        action = copy.deepcopy(record['action'])
        options = dict(record['options'], cleanup=False)
        inside(action['target'], config['target_roots'])
        inside(options['target'], config['target_roots'])
        plan = {'id':uuid.uuid4().hex,'created':time.time(),'options':options,'actions':[action],
                'warnings':[], 'counts':{'strm':1,'copy':0,'move':0,'trash':0,'recorded':0},
                'config_fingerprint':config_fingerprint(config)}
        if 'account' in record:
            from .cloud import signature as remote_signature
            account = record['account']
            with self.state.clouds.client(account) as client:
                current = next((o for o in self.state.clouds.entries(account,action['remote']['parent'],client,allow_duplicates=True) if o['id']==action['remote']['id']),None)
            if current is None:
                raise ValueError('网盘原文件不存在，请重新选择目录扫描')
            action.update(remote=current,signature=remote_signature(current))
            plan.update(account=account,account_revision=self.state.clouds.account(account)['revision'])
        else:
            source = inside(action['source'], config['source_roots'])
            action['signature'] = signature(source)
        action['managed_hash'] = record['content_hash']
        return plan

    def backfill(self, cancel=lambda:False):
        """Import legacy successful outputs only after checking their actual content."""
        with self.state.lock:
            if self.state.db.execute("SELECT 1 FROM generation_meta WHERE key='legacy-import-v1'").fetchone():
                return
            rows = self.state.db.execute("SELECT value FROM job_plans UNION ALL SELECT json_extract(value,'$.plan') FROM jobs WHERE json_type(value,'$.plan')='object'").fetchall()
        seen = set()
        for row in rows:
            if cancel():
                raise ValueError('任务已取消')
            plan = json.loads(row[0])
            if 'options' not in plan or 'config_fingerprint' not in plan:
                continue
            for action in plan.get('actions',[]):
                if action.get('type')!='strm' or 'signature' not in action:
                    continue
                key = self.identifier(plan,action)
                if key in seen:
                    continue
                seen.add(key)
                with self.state.lock:
                    exists = self.state.db.execute('SELECT 1 FROM generated WHERE id=? UNION ALL SELECT 1 FROM generation_retry WHERE id=?', (key,key)).fetchone()
                if exists:
                    continue
                try:
                    target = inside(action['target'],self.state.config['target_roots'])
                    if target.stat().st_size > 16384:
                        continue
                    source = {'account':plan['account'],'object':action['remote']} if 'remote' in action else Path(action['source'])
                    content = str(source) if plan['options'].get('mode')=='path' else self.state.playback(source, register=False)
                    data = (content+'\n').encode()
                    if target.read_bytes() == data:
                        self.record(plan,action,data)
                except (OSError,ValueError,KeyError):
                    continue
            self.flush()
        with self.state.lock:
            self.state.db.execute("INSERT OR REPLACE INTO generation_meta VALUES('legacy-import-v1','done')")
            self.state.db.commit()
