"""Native cloud API adapters. No mount, external file server or token relay."""
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urljoin
from urllib.request import Request, build_opener, ProxyHandler

from .resolver import NoRedirect, valid_url


class CloudError(ValueError):
    pass


def http_json(url, method='GET', query=None, body=None, form=None, headers=None):
    if query:
        url += ('&' if '?' in url else '?') + urlencode(query)
    hdr = {'Accept': 'application/json', 'User-Agent': 'STRMBridge/2.0', **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdr['Content-Type'] = 'application/json'
    if form is not None:
        data = urlencode(form).encode()
        hdr['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url, data=data, headers=hdr, method=method), timeout=30) as r:
            raw = r.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise CloudError('网盘响应过大')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise CloudError('网盘响应格式不正确')
        return result
    except HTTPError as e:
        # Return structured API errors, never echo credential-bearing request URLs.
        try:
            result = json.loads(e.read(65536))
            if isinstance(result, dict):
                result['_http_status'] = e.code
                return result
        except (ValueError, OSError):
            pass
        raise CloudError('网盘接口返回 HTTP ' + str(e.code)) from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise CloudError('网盘连接超时或返回了无效数据，请检查授权和网络') from None


def timestamp(value):
    try:
        n = float(value)
        return n / 1000 if n > 100_000_000_000 else n
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() if dt.tzinfo is None else dt.timestamp()
        except ValueError:
            return 0


def item(identifier, name, folder=False, size=0, modified=0, **extra):
    return dict(id=str(identifier), name=str(name), folder=bool(folder), size=int(size or 0), modified=timestamp(modified), **extra)


def final_redirect(url, headers=None):
    """Resolve ticket endpoints without reading media bytes or forwarding API credentials."""
    for _ in range(5):
        valid_url(url)
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url, method='HEAD', headers=headers or {}), timeout=25):
                return url
        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                url = urljoin(url, e.headers.get('Location', ''))
                continue
            if e.code == 405:
                return url
            raise CloudError('网盘下载地址返回 HTTP ' + str(e.code)) from None
        except (URLError, TimeoutError):
            raise CloudError('网盘下载地址连接失败') from None
    raise CloudError('网盘下载地址重定向次数过多')


class Provider:
    root = '0'
    auth_codes = set()

    def __init__(self, credentials, save=lambda: None):
        self.c = credentials
        self.save = save
        self.last_request = 0

    def raw(self, *args, **kwargs):
        time.sleep(max(0, 0.35 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        return http_json(*args, **kwargs)

    def tokens(self, data):
        access = data.get('access_token') or data.get('accessToken')
        if not access:
            raise CloudError('授权刷新失败，请重新填写网盘授权')
        self.c['access_token'] = access
        if data.get('refresh_token'):
            self.c['refresh_token'] = data['refresh_token']
        self.c['expires_at'] = time.time() + int(data.get('expires_in', 3600))
        if data.get('expiredAt'):
            self.c['expires_at'] = timestamp(data['expiredAt'])
        self.save()

    def token(self):
        if not self.c.get('access_token') or (self.c.get('expires_at') and self.c['expires_at'] < time.time() + 60):
            self.refresh()
        return self.c.get('access_token', '')

    def refresh(self):
        raise CloudError('授权已过期，请编辑网盘并更新授权')

    def request(self, endpoint, method='GET', query=None, body=None, form=None, headers=None):
        for attempt in range(2):
            token = self.token()
            hdr = {'Authorization': 'Bearer ' + token, **(headers or {})}
            result = self.raw(self.base + endpoint, method, query=query, body=body, form=form, headers=hdr)
            code = str(result.get('code', result.get('errno', result.get('error', ''))))
            if attempt == 0 and (code in self.auth_codes or (isinstance(self, Cloud115) and code.startswith('401')) or result.get('_http_status') == 401):
                self.refresh()
                continue
            self.check(result)
            return result
        raise CloudError('网盘授权失效')

    def check(self, result):
        code = result.get('code', 0)
        if result.get('_http_status', 200) >= 400 or code not in (0, '0', None, '') or result.get('error'):
            raise CloudError('网盘接口拒绝请求（代码 ' + str(code) + '），请检查授权或账号权限')

    def mkdir(self, parent, name):
        raise NotImplementedError

    def move(self, obj, destination):
        raise NotImplementedError

    def delete(self, obj):
        raise CloudError('当前网盘接口不支持直接删除')


class Cloud115(Provider):
    base = 'https://proapi.115.com'
    auth_codes = {'99', '401', '40140125', '40140126', '40140127'}

    def check(self, d):
        if not d.get('state'):
            raise CloudError('115 请求失败（代码 ' + str(d.get('code', d.get('errno', '未知'))) + '），请检查授权或网盘权限')

    def refresh(self):
        d = self.raw('https://passportapi.115.com/open/refreshToken', 'POST', form={'refresh_token': self.c.get('refresh_token', '')})
        if d.get('code', 0) != 0 or d.get('error') or not d.get('data'):
            raise CloudError('115 刷新授权失败，请更新 Refresh Token')
        self.tokens(d['data'])

    def list(self, parent):
        offset, seen = 0, set()
        while True:
            d = self.request('/open/ufile/files', query={'cid': parent, 'limit': 200, 'offset': offset, 'show_dir': 1, 'cur': 1, 'o': 'file_name', 'asc': 1})
            rows = d.get('data', [])
            for r in rows:
                if str(r['fid']) in seen:
                    raise CloudError('115 分页重复，请稍后重新扫描')
                seen.add(str(r['fid']))
                yield item(r['fid'], r['fn'], str(r['fc']) == '0', r.get('fs'), r.get('upt'), pickcode=r.get('pc', ''))
            offset += len(rows)
            if offset >= int(d['count']):
                return
            if not rows:
                raise CloudError('115 返回了不完整的目录列表')

    def link(self, obj, agent):
        d = self.request('/open/ufile/downurl', 'POST', form={'pick_code': obj['pickcode']}, headers={'User-Agent': agent})['data']
        return valid_url(d[str(obj['id'])]['url']['url']), {'User-Agent': agent}

    def mkdir(self, parent, name):
        return str(self.request('/open/folder/add', 'POST', form={'pid': parent, 'file_name': name})['data']['file_id'])

    def move(self, obj, destination):
        self.request('/open/ufile/move', 'POST', form={'file_ids': obj['id'], 'to_cid': destination})
        return dict(obj, parent=destination)

    def delete(self, obj):
        self.request('/open/ufile/delete', 'POST', form={'file_ids':obj['id'], 'parent_id':obj['parent']})


class Cloud123(Provider):
    base = 'https://open-api.123pan.com'
    auth_codes = {'401'}

    def refresh(self):
        if not self.c.get('client_id') or not self.c.get('client_secret'):
            raise CloudError('123 授权已过期，请填写开放平台 Client ID 和 Client Secret，或更新 Access Token')
        d = self.raw(self.base + '/api/v1/access_token', 'POST', body={'clientID': self.c['client_id'], 'clientSecret': self.c['client_secret']}, headers={'Platform': 'open_platform'})
        self.check(d)
        self.tokens(d['data'])

    def request(self, *args, **kwargs):
        kwargs['headers'] = {'Platform': 'open_platform', **kwargs.get('headers', {})}
        return super().request(*args, **kwargs)

    def list(self, parent):
        last, cursors = 0, set()
        while last != -1:
            if last in cursors:
                raise CloudError('123 云盘分页重复，请重新扫描')
            cursors.add(last)
            d = self.request('/api/v2/file/list', query={'parentFileId': parent, 'limit': 100, 'lastFileId': last})['data']
            for r in d.get('fileList', []):
                if not r.get('trashed'):
                    yield item(r['fileId'], r['filename'], r['type'] == 1, r.get('size'), r.get('updateAt'), etag=r.get('etag', ''))
            last = int(d['lastFileId'])

    def link(self, obj, agent):
        d = self.request('/api/v1/file/download_info', query={'fileId': obj['id']})['data']
        return valid_url(d['downloadUrl']), {}

    def mkdir(self, parent, name):
        self.request('/upload/v1/file/mkdir', 'POST', body={'parentID': str(parent), 'name': name})
        matches = [x for x in self.list(parent) if x['folder'] and x['name'] == name]
        if len(matches) != 1:
            raise CloudError('无法确认 123 云盘新建文件夹')
        return matches[0]['id']

    def move(self, obj, destination):
        self.request('/api/v1/file/move', 'POST', body={'fileIDs': [int(obj['id'])], 'toParentFileID': int(destination)})
        return dict(obj, parent=destination)

    def delete(self, obj):
        self.request('/api/v1/file/trash', 'POST', body={'fileIDs':[int(obj['id'])]})


class Aliyun(Provider):
    root = 'root'
    base = 'https://openapi.alipan.com'
    auth_codes = {'AccessTokenInvalid', 'AccessTokenExpired', 'I400JD'}

    def refresh(self):
        if self.c.get('auth_mode') == 'online':
            return online_refresh(self, 'aliyun')
        if not self.c.get('client_id') or not self.c.get('client_secret'):
            raise CloudError('阿里云盘开放授权续期需要对应应用的 Client ID / Secret；也可重新填写 Access Token')
        d = self.raw(self.base + '/oauth/access_token', 'POST', body={k: self.c.get(k, '') for k in ('client_id', 'client_secret', 'refresh_token')} | {'grant_type': 'refresh_token'})
        self.check(d)
        self.tokens(d)

    def drive(self):
        if not self.c.get('drive_id'):
            d = self.request('/adrive/v1.0/user/getDriveInfo', 'POST', body={})
            self.c['drive_id'] = d.get('resource_drive_id') or d['default_drive_id']
            self.save()
        return self.c['drive_id']

    def list(self, parent):
        marker, cursors = '', set()
        while True:
            if marker in cursors:
                raise CloudError('阿里云盘分页重复，请重新扫描')
            cursors.add(marker)
            d = self.request('/adrive/v1.0/openFile/list', 'POST', body={'drive_id': self.drive(), 'parent_file_id': parent, 'limit': 100, 'marker': marker, 'order_by': 'name', 'order_direction': 'ASC'})
            for r in d.get('items', []):
                yield item(r['file_id'], r['name'], r['type'] == 'folder', r.get('size'), r.get('updated_at'))
            marker = d.get('next_marker', '')
            if not marker:
                return

    def link(self, obj, agent):
        d = self.request('/adrive/v1.0/openFile/getDownloadUrl', 'POST', body={'drive_id': self.drive(), 'file_id': obj['id']})
        return valid_url(d['url']), {}

    def mkdir(self, parent, name):
        return self.request('/adrive/v1.0/openFile/create', 'POST', body={'drive_id': self.drive(), 'parent_file_id': parent, 'name': name, 'type': 'folder', 'check_name_mode': 'refuse'})['file_id']

    def move(self, obj, destination):
        d = self.request('/adrive/v1.0/openFile/move', 'POST', body={'drive_id': self.drive(), 'file_id': obj['id'], 'to_parent_file_id': destination, 'check_name_mode': 'refuse'})
        if d.get('async_task_id'):
            raise CloudError('网盘移动仍在异步处理中，请稍后在任务记录恢复或检查云盘')
        return dict(obj, id=d.get('file_id', obj['id']), parent=destination)


    def delete(self, obj):
        self.request('/adrive/v1.0/openFile/recyclebin/trash', 'POST', body={'drive_id':self.drive(), 'file_id':obj['id']})


class Baidu(Provider):
    root = '/'
    base = 'https://pan.baidu.com/rest/2.0'

    def check(self, d):
        code = d.get('errno', 0)
        if code or d.get('error') or d.get('_http_status', 200) >= 400:
            raise CloudError('百度网盘请求失败（代码 ' + str(code or d.get('error')) + '），请检查授权或下载权限')

    def refresh(self):
        if self.c.get('auth_mode') == 'online':
            return online_refresh(self, 'baidu')
        if not self.c.get('client_id') or not self.c.get('client_secret'):
            raise CloudError('百度授权续期需要对应应用的 AppKey / SecretKey；也可重新填写 Access Token')
        d = self.raw('https://openapi.baidu.com/oauth/2.0/token', 'POST', form={k: self.c.get(k, '') for k in ('client_id', 'client_secret', 'refresh_token')} | {'grant_type': 'refresh_token'})
        self.check(d)
        self.tokens(d)

    def request(self, endpoint, method='GET', query=None, body=None, form=None, headers=None):
        for attempt in range(2):
            d = self.raw(self.base + endpoint, method, query={**(query or {}), 'access_token': self.token()}, form=form, body=body, headers=headers)
            if attempt == 0 and (d.get('errno') in (111, -6, 20016, 20017) or d.get('_http_status') == 401):
                self.refresh()
                continue
            self.check(d)
            return d

    def list(self, parent):
        start, seen = 0, set()
        while True:
            d = self.request('/xpan/file', query={'method': 'list', 'dir': parent, 'start': start, 'limit': 1000, 'order': 'name'})
            rows = d.get('list', [])
            for r in rows:
                if r['path'] in seen:
                    raise CloudError('百度网盘分页重复，请重新扫描')
                seen.add(r['path'])
                yield item(r['path'], r['server_filename'], r['isdir'] == 1, r.get('size'), r.get('server_mtime'), fsid=str(r['fs_id']))
            if len(rows) < 1000:
                return
            start += len(rows)

    def link(self, obj, agent):
        d = self.request('/xpan/multimedia', query={'method': 'filemetas', 'fsids': json.dumps([int(obj['fsid'])]), 'dlink': 1})
        url = d['list'][0]['dlink']
        url += ('&' if '?' in url else '?') + urlencode({'access_token': self.token()})
        return final_redirect(url, {'User-Agent': 'pan.baidu.com'}), {'User-Agent': 'pan.baidu.com'}

    def mkdir(self, parent, name):
        path = parent.rstrip('/') + '/' + name
        self.request('/xpan/file', 'POST', query={'method': 'create'}, form={'path': path, 'size': 0, 'isdir': 1, 'rtype': 0})
        return path

    def move(self, obj, destination):
        d = self.request('/xpan/file', 'POST', query={'method': 'filemanager', 'opera': 'move'}, form={'async': 0, 'ondup': 'fail', 'filelist': json.dumps([{'path': obj['id'], 'dest': destination, 'newname': obj['name']}])})
        if any(r.get('errno', 0) for r in d.get('info', [])) or d.get('taskid'):
            raise CloudError('百度网盘移动未确认完成，请检查云端文件')
        return dict(obj, id=destination.rstrip('/') + '/' + obj['name'], parent=destination)


    def delete(self, obj):
        d=self.request('/xpan/file', 'POST', query={'method':'filemanager','opera':'delete'}, form={'async':'0','filelist':json.dumps([obj['id']]),'ondup':'fail'})
        if any(r.get('errno',0) for r in d.get('info',[])) or d.get('taskid'):
            raise CloudError('百度网盘删除未确认完成，请检查云端文件')


class Tianyi(Provider):
    base = 'https://cloud.189.cn'
    root = '-11'

    def request(self, endpoint, method='GET', query=None, body=None, form=None, headers=None):
        if not self.c.get('cookie'):
            raise CloudError('请填写天翼云盘网页版登录 Cookie')
        d = self.raw(self.base + endpoint, method, query=query, body=body, form=form, headers={'Cookie': self.c['cookie'], 'Referer': self.base + '/', **(headers or {})})
        if d.get('res_code', 0) != 0 or d.get('errorCode') or d.get('_http_status', 200) >= 400:
            raise CloudError('天翼云盘授权过期或权限不足，请更新登录 Cookie')
        return d

    def list(self, parent):
        page, seen = 1, set()
        while True:
            d = self.request('/api/open/file/listFiles.action', query={'folderId': parent, 'pageNum': page, 'pageSize': 60, 'mediaType': 0, 'iconOption': 5, 'orderBy': 'filename', 'descending': 'false'})['fileListAO']
            rows = [(r, True) for r in d.get('folderList', [])] + [(r, False) for r in d.get('fileList', [])]
            if not rows:
                return
            for r, folder in rows:
                if str(r['id']) in seen:
                    raise CloudError('天翼云盘分页重复，请重新扫描')
                seen.add(str(r['id']))
                yield item(r['id'], r['name'], folder, r.get('size'), r.get('lastOpTime'))
            page += 1

    def link(self, obj, agent):
        d = self.request('/api/portal/getFileInfo.action', query={'fileId': obj['id']})
        url = d['downloadUrl']
        return final_redirect('https:' + url if url.startswith('//') else url, {'User-Agent': agent}), {}

    def mkdir(self, parent, name):
        self.request('/api/open/file/createFolder.action', 'POST', form={'parentFolderId': parent, 'folderName': name})
        matches = [r for r in self.list(parent) if r['folder'] and r['name'] == name]
        if len(matches) != 1:
            raise CloudError('无法确认天翼云盘新建文件夹')
        return matches[0]['id']

    def move(self, obj, destination):
        self.request('/api/open/batch/createBatchTask.action', 'POST', form={'type': 'MOVE', 'targetFolderId': destination, 'taskInfos': json.dumps([{'fileId': obj['id'], 'fileName': obj['name'], 'isFolder': int(obj['folder'])}])})
        for _ in range(10):
            matches = [r for r in self.list(destination) if r['id'] == obj['id']]
            if matches:
                return dict(matches[0], parent=destination)
            time.sleep(1)
        raise CloudError('天翼云盘移动仍在处理中，请稍后检查任务记录')


    def delete(self, obj):
        self.request('/api/open/batch/createBatchTask.action', 'POST', form={'type':'DELETE','targetFolderId':'',
            'taskInfos':json.dumps([{'fileId':obj['id'],'fileName':obj['name'],'isFolder':int(obj['folder'])}])})


def online_refresh(client, provider):
    # These are the documented OpenList authorization services. The UI makes this
    # choice explicit; direct OAuth credentials continue to avoid the relay.
    service, driver = ('alicloud', 'alicloud_qr') if provider == 'aliyun' else ('baiduyun', 'baiduyun_go')
    d = client.raw('https://api.oplist.org/'+service+'/renewapi', query={
        'refresh_ui': client.c.get('refresh_token', ''), 'server_use': 'true', 'driver_txt': driver})
    if not d.get('access_token') or not d.get('refresh_token'):
        raise CloudError('OpenList 授权续期失败，请重新从对应授权入口获取 Refresh Token')
    client.tokens(d)


PROVIDERS = {'115': Cloud115, 'baidu': Baidu, 'aliyun': Aliyun, '123': Cloud123, '189': Tianyi}
SECRET_FIELDS = ('access_token', 'refresh_token', 'client_secret', 'cookie', 'password', 'web_token')
CATALOG = [
    {'id': '115', 'name': '115 网盘', 'root': '0', 'default_method': 'cookie', 'methods': [
        {'id': 'cookie', 'name': 'Cookie / 扫码登录', 'fields': ['cookie'], 'qr': True,
         'hint': '粘贴 115 登录 Cookie，或使用手机 115 App 扫码。', 'docs': 'https://doc.oplist.org/guide/drivers/115'},
        {'id': 'open', 'name': '开放平台授权', 'fields': ['refresh_token', 'access_token'],
         'hint': '填写 115 开放平台 Refresh Token。'}]},
    {'id': 'baidu', 'name': '百度网盘', 'root': '/', 'default_method': 'online', 'methods': [
        {'id': 'online', 'name': 'OpenList 授权', 'fields': ['refresh_token'], 'auth_url': 'https://api.oplist.org',
         'hint': '在授权页面选择百度网盘，使用 OpenList 提供的参数，再复制 Refresh Token。续期通过 api.oplist.org。', 'docs': 'https://doc.oplist.org/guide/drivers/baidu'},
        {'id': 'open', 'name': '自有开放平台应用', 'fields': ['access_token', 'refresh_token', 'client_id', 'client_secret'],
         'hint': '使用同一应用的授权与密钥。'}]},
    {'id': 'aliyun', 'name': '阿里云盘', 'root': 'root', 'default_method': 'online', 'methods': [
        {'id': 'online', 'name': 'OpenList 扫码授权', 'fields': ['refresh_token', 'drive_id'], 'auth_url': 'https://api.oplist.org',
         'hint': '在授权页面选择阿里云盘应用登录，使用 OpenList 提供的参数，扫码后复制 Refresh Token。续期通过 api.oplist.org。', 'docs': 'https://doc.oplist.org/guide/drivers/aliyundrive_open'},
        {'id': 'open', 'name': '自有开放平台应用', 'fields': ['access_token', 'refresh_token', 'client_id', 'client_secret', 'drive_id'],
         'hint': '使用开放平台 Token；网页版 refresh_token 不适用。'}]},
    {'id': '123', 'name': '123 云盘', 'root': '0', 'default_method': 'password', 'methods': [
        {'id': 'password', 'name': '账号密码', 'fields': ['username', 'password'],
         'hint': '填写登录手机号或邮箱和密码；遇到短信验证时先在官网完成登录。', 'docs': 'https://doc.oplist.org/guide/drivers/123'},
        {'id': 'web', 'name': '网页登录令牌', 'fields': ['web_token'],
         'hint': '粘贴官网请求头 Authorization 的 Bearer 后面的令牌。', 'docs': 'https://doc.oplist.org/guide/drivers/123'},
        {'id': 'open', 'name': '开放平台授权', 'fields': ['client_id', 'client_secret', 'access_token'],
         'hint': '填写开放平台 Client ID / Secret。'}]},
    {'id': '189', 'name': '天翼云盘', 'root': '-11', 'default_method': 'cookie', 'methods': [
        {'id': 'cookie', 'name': 'Cookie 登录', 'fields': ['cookie'],
         'hint': '粘贴 cloud.189.cn 登录后的 Cookie。', 'docs': 'https://doc.oplist.org/guide/drivers/189'}]},
]
for spec in CATALOG:
    spec['fields'] = list(dict.fromkeys(['auth_mode'] + [f for m in spec['methods'] for f in m['fields']]))


def create_provider(provider, credentials, save):
    mode = credentials.get('auth_mode', 'cookie' if provider == '189' else 'open')
    if provider == '115' and mode == 'cookie':
        from .cloud_cookie import Cookie115
        return Cookie115(credentials, save)
    if provider == '123' and mode in ('password', 'web'):
        from .cloud_cookie import Web123
        return Web123(credentials, save)
    return PROVIDERS[provider](credentials, save)
