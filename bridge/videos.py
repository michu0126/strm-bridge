"""Persistent source-resolution scans and explicitly selected cloud file actions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import PurePosixPath
import threading
import time
import uuid

from .cloud import parse_uri, signature, RecursivePathConflict
from .core import VIDEO, SKIP_DIRS
from .progress import ScanProgress
from .cloud_providers import CloudError


def media_signature(obj):
    return json.dumps([obj['id'],obj['size'],obj.get('sha1') or obj['modified']],sort_keys=True)


def below(width, height, threshold=1080, rule='tier'):
    if not width or not height:
        return False
    return height < threshold if rule=='height' else min(width,height) < threshold and max(width,height) < round(threshold*16/9)


class VideoLibrary:
    def __init__(self,state):
        self.state=state;self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cloud-video')
        self.stop=threading.Event();self.active=None
        with state.lock:
            state.db.executescript('''CREATE TABLE IF NOT EXISTS video_scans(id TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS video_rows(scan TEXT,id TEXT,value TEXT,PRIMARY KEY(scan,id));
                CREATE TABLE IF NOT EXISTS video_metadata(account TEXT,id TEXT,signature TEXT,value TEXT,PRIMARY KEY(account,id));''')
            for key,value in state.db.execute('SELECT id,value FROM video_scans').fetchall():
                scan=json.loads(value)
                if scan['status'] in ('scanning','running'):
                    scan.update(status='interrupted',error='服务重启，已保存检测结果，可继续扫描')
                    self.put(scan)
            state.db.commit()

    def validate_source(self,source):
        account,root=parse_uri(source);a=self.state.clouds.account(account)
        if a['provider']!='115' or a['credentials'].get('auth_mode')!='cookie':
            raise ValueError('分辨率筛选需要使用 Cookie / 扫码登录的 115 账号')
        return account,root

    def put(self,scan):
        with self.state.lock:
            self.state.db.execute('INSERT OR REPLACE INTO video_scans VALUES(?,?)',(scan['id'],json.dumps(scan,ensure_ascii=False)))
            self.state.db.commit()
        job=dict(scan,type='video_scan' if scan.get('operation') is None else 'cloud_batch',options=scan['options'])
        self.state.put_job(job)

    def get(self,identifier):
        with self.state.lock:
            row=self.state.db.execute('SELECT value FROM video_scans WHERE id=?',(identifier,)).fetchone()
        if not row:raise ValueError('视频检测任务不存在')
        return json.loads(row[0])

    def list(self):
        with self.state.lock:
            rows=self.state.db.execute('SELECT value FROM video_scans ORDER BY rowid DESC LIMIT 30').fetchall()
        return [json.loads(r[0]) for r in rows]

    def rows(self,identifier,offset=0,filter_='low'):
        scan=self.get(identifier)
        if filter_ not in ('low','known','unknown','all'):raise ValueError('视频筛选条件无效')
        where={'low':"AND json_extract(value,'$.low')=1",'known':"AND json_extract(value,'$.width')>0 AND json_extract(value,'$.height')>0",'unknown':"AND (json_extract(value,'$.width')=0 OR json_extract(value,'$.height')=0)",'all':''}[filter_]
        with self.state.lock:
            total=self.state.db.execute('SELECT count(*) FROM video_rows WHERE scan=? '+where,(identifier,)).fetchone()[0]
            rows=self.state.db.execute('SELECT value FROM video_rows WHERE scan=? '+where+' ORDER BY rowid LIMIT 100 OFFSET ?',(identifier,max(0,int(offset)))).fetchall()
        public=[]
        for row in rows:
            r=json.loads(row[0]);public.append({k:r.get(k) for k in ('id','name','path','size','width','height','low','error','operation')})
        return {'scan':scan,'total':total,'offset':max(0,int(offset)),'rows':public}

    def start(self,data):
        account,root=self.validate_source(data['source'])
        threshold=int(data.get('height',1080));rule=data.get('rule','tier')
        if threshold not in (480,720,1080,1440,2160) or rule not in ('tier','height'):
            raise ValueError('分辨率阈值或判定方式无效')
        with self.state.lock:
            if self.active:raise ValueError('已有视频检测或批量操作正在运行，请等待或暂停')
            identifier=uuid.uuid4().hex;self.active=identifier
        scan={'id':identifier,'status':'scanning','created':time.time(),'options':{'source':data['source'],'height':threshold,'rule':rule},
              'account':account,'revision':self.state.clouds.account(account)['revision'],'done':0,'total':0,'known':0,'unknown':0,'matched':0,'cached':0,'error':''}
        self.put(scan);self.pool.submit(self.scan,scan,data.get('refresh') is True)
        return scan

    def cancelled(self,identifier):
        return self.stop.is_set() or identifier in self.state.cancelled

    def collect(self,source,cancel,progress):
        account,root=self.validate_source(source)
        with self.state.clouds.client(account) as client:client.directory_path(root)
        report=ScanProgress(progress)
        try:
            objects=self.state.clouds.recursive_files(account,root,cancel,report).values()
        except RecursivePathConflict:
            stack,seen,objects=[root],set(),[]
            while stack:
                if cancel():raise ValueError('任务已取消')
                parent=stack.pop()
                if parent in seen:raise ValueError('网盘目录出现循环')
                seen.add(parent)
                with self.state.clouds.client(account) as client:rows=self.state.clouds.entries(account,parent,client,cancel,allow_duplicates=True)
                for obj in rows:
                    if obj['folder']:
                        if obj['name'] not in SKIP_DIRS:stack.append(obj['id'])
                    else:objects.append(obj)
        return [o for o in objects if PurePosixPath(o['name']).suffix.lower().lstrip('.') in VIDEO]

    def scan(self,scan,refresh):
        state=self.state;identifier=scan['id'];cancel=lambda:self.cancelled(identifier)
        try:
            def listing(info):
                scan['listing']=info;self.put(scan)
            objects=self.collect(scan['options']['source'],cancel,listing);scan['total']=len(objects);self.put(scan)
            failures=0;last=0
            for obj in objects:
                if cancel():break
                if state.clouds.account(scan['account'])['revision']!=scan['revision']:raise ValueError('网盘授权已更新，请继续扫描')
                fingerprint=media_signature(obj)
                with state.lock:
                    row=state.db.execute('SELECT signature,value FROM video_metadata WHERE account=? AND id=?',(scan['account'],obj['id'])).fetchone()
                meta=json.loads(row[1]) if row and row[0]==fingerprint and not refresh else None
                if meta and not meta.get('width') and time.time()-meta.get('checked',0)>600:meta=None
                if meta:scan['cached']+=1
                else:
                    try:
                        with state.clouds.client(scan['account']) as client:meta=client.video_info(obj)
                        meta=dict(meta,checked=time.time(),error='');failures=0
                    except (ValueError,OSError) as exc:
                        meta={'width':0,'height':0,'checked':time.time(),'error':str(exc)};failures+=1
                    with state.lock:
                        state.db.execute('INSERT OR REPLACE INTO video_metadata VALUES(?,?,?,?)',(scan['account'],obj['id'],fingerprint,json.dumps(meta)))
                record=dict(meta,id=obj['id'],name=obj['name'],path=obj['path'],size=obj['size'],remote=obj,
                    low=below(meta['width'],meta['height'],scan['options']['height'],scan['options']['rule']))
                with state.lock:state.db.execute('INSERT OR REPLACE INTO video_rows VALUES(?,?,?)',(identifier,obj['id'],json.dumps(record,ensure_ascii=False)))
                scan['done']+=1;scan['matched']+=int(record['low']);scan['known' if meta['width'] and meta['height'] else 'unknown']+=1;scan['current']=obj['path']
                if time.monotonic()-last>=1 or scan['done']==scan['total']:
                    self.put(scan);last=time.monotonic()
                if failures>=10:raise ValueError('连续 10 个文件无法读取视频信息，已停止请求；已检测结果保留，可稍后继续')
                if not meta.get('error') and not row:self.stop.wait(.08)
            scan['status']='cancelled' if cancel() else 'completed'
        except Exception as exc:
            scan.update(status='cancelled' if cancel() else 'partial',error=str(exc))
        finally:
            self.put(scan)
            with state.lock:self.active=None

    def batch(self,data):
        source=self.get(data['scan_id'])
        ids=data.get('ids');operation=data.get('operation')
        if operation not in ('move','delete') or not isinstance(ids,list) or not 1<=len(ids)<=5000 or any(not isinstance(i,str) for i in ids):
            raise ValueError('请选择要移动或删除的视频（最多 5000 个）')
        account,_=self.validate_source(source['options']['source']);destination=None
        if self.state.clouds.account(account)['revision']!=source['revision']:
            raise ValueError('网盘授权已更新，请继续检测后再操作')
        if operation=='move':
            dest_account,destination=parse_uri(data['target'])
            if account!=dest_account:raise ValueError('只能移动到同一 115 账号的文件夹')
            if not self.state.clouds.node(account,destination)['folder']:raise ValueError('请选择目标文件夹')
        records=[]
        with self.state.lock:
            if self.active:raise ValueError('请先暂停视频检测，待当前文件处理完成后再批量操作')
            if self.state.busy:raise ValueError('请等待当前 STRM 任务完成后再执行网盘批量操作')
            for key in dict.fromkeys(ids):
                row=self.state.db.execute('SELECT value FROM video_rows WHERE scan=? AND id=?',(source['id'],key)).fetchone()
                if not row:raise ValueError('视频检测结果不存在，请刷新')
                record=json.loads(row[0])
                if not below(record['width'],record['height'],source['options']['height'],source['options']['rule']):
                    raise ValueError('只能对已确认低于阈值的视频执行此操作，未知分辨率不会参与')
                records.append(record)
            identifier=uuid.uuid4().hex;self.active=identifier;self.state.busy='cloud-batch:'+identifier
        task={'id':identifier,'status':'running','created':time.time(),'operation':operation,'options':source['options'],
            'source_scan':source['id'],'account':account,'revision':source['revision'],'destination':destination,
            'done':0,'total':len(records),'result':{'moved':0,'deleted':0,'errors':[]},'error':''}
        self.put(task);self.pool.submit(self.run_batch,task,records)
        return task

    def run_batch(self,task,records):
        state=self.state;identifier=task['id'];cancel=lambda:self.cancelled(identifier)
        try:
            for record in records:
                if cancel():break
                obj=record['remote'];task['current']=record['path']
                try:
                    with state.clouds.client(task['account']) as client:
                        if state.clouds.account(task['account'])['revision']!=task['revision']:raise ValueError('网盘授权已变更，请重新检测')
                        rows=state.clouds.entries(task['account'],obj['parent'],client,allow_duplicates=True)
                        current=next((r for r in rows if r['id']==obj['id']),None)
                        if current is None or media_signature(current)!=media_signature(obj) or current['name']!=obj['name']:
                            raise ValueError('源文件已变更，请重新检测后操作')
                        if task['operation']=='move':
                            client.directory_path(task['destination'])
                            if current['parent']==task['destination']:raise ValueError('文件已在目标目录')
                            siblings=state.clouds.entries(task['account'],task['destination'],client,allow_duplicates=True)
                            if any(o['name']==current['name'] for o in siblings):raise ValueError('目标已有同名文件，未覆盖')
                            client.move(current,task['destination'])
                            verified=state.clouds.entries(task['account'],task['destination'],client,allow_duplicates=True)
                            if not any(o['id']==current['id'] for o in verified):raise ValueError('未确认移动结果，请检查云端目录')
                            task['result']['moved']+=1
                        else:
                            client.delete(current)
                            verified=state.clouds.entries(task['account'],current['parent'],client,allow_duplicates=True)
                            if any(o['id']==current['id'] for o in verified):raise ValueError('未确认删除结果，请检查云端目录')
                            task['result']['deleted']+=1
                    record['operation']=task['operation']
                    with state.lock:
                        state.db.execute('UPDATE video_rows SET value=? WHERE scan=? AND id=?',(json.dumps(record,ensure_ascii=False),task['source_scan'],record['id']))
                except (ValueError,OSError) as exc:task['result']['errors'].append({'path':record['path'],'message':str(exc)})
                task['done']+=1;self.put(task)
            task['status']='cancelled' if cancel() else ('partial' if task['result']['errors'] else 'completed')
        except Exception as exc:task.update(status='failed',error=str(exc))
        finally:
            self.put(task)
            with state.lock:
                for watch in state.watcher.list():
                    if watch['enabled'] and watch['options'].get('reconcile') and watch['options']['source'].startswith('cloud://'+task['account']+'/'):
                        watch['next_run']=0;state.watcher.put(watch)
                self.active=None
                if state.busy=='cloud-batch:'+identifier:state.busy=None

    def close(self):
        self.stop.set();self.pool.shutdown(wait=True)
