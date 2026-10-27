"""Cookie and account login protocols matching OpenList's personal-drive modes."""
import base64
import json
import secrets
import time
import zlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, parse_qs

from .cloud_providers import Provider, CloudError, item, valid_url, final_redirect
from . import cipher115


def normalize_cookie(value):
    value = str(value).strip()
    if value.lower().startswith('cookie:'):
        value = value[7:].strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CloudError('Cookie 中不能包含换行或控制字符，请只粘贴 Cookie 请求头的值')
    return value.rstrip('; ').strip()


class Cookie115(Provider):
    root = '0'
    base = 'https://webapi.115.com'

    def request(self, endpoint, method='GET', query=None, body=None, form=None, headers=None):
        d = self.raw(endpoint if endpoint.startswith('https://') else self.base+endpoint, method,
                     query=query, body=body, form=form,
                     headers={'Cookie': self.c['cookie'], 'Referer': 'https://115.com/',
                              'User-Agent': 'Mozilla/5.0 115Browser/27.0.5.7', **(headers or {})})
        if not d.get('state') or d.get('_http_status', 200) >= 400:
            code = d.get('errno', d.get('errNo', d.get('code', '')))
            raise CloudError('115 请求失败（代码 '+str(code)+'），请检查 Cookie 是否有效及下载权限')
        return d

    def list(self, parent):
        offset, seen = 0, set()
        while True:
            # New uploads append to this order instead of shifting earlier pages.
            query = {'aid': 1, 'cid': parent, 'offset': offset, 'limit': 200,
                     'show_dir': 1, 'cur': 1, 'format': 'json', 'o': 'user_ptime',
                     'asc': 1, 'fc_mix': 1, 'custom_order': 1}
            # The retired 115Browser agent is rejected by the directory API.
            # Keep the full-paging web API; the APS API repeats large directories.
            d = self.request('/files', query=query, headers={'User-Agent':
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'})
            if 'count' not in d or not isinstance(d.get('data'), list):
                raise CloudError('115 目录响应不完整，请重新扫描')
            rows = d['data']
            for r in rows:
                folder = not bool(r.get('fid'))
                identifier = str(r['cid'] if folder else r['fid'])
                if identifier in seen:
                    raise CloudError('115 分页重复，请重新扫描')
                seen.add(identifier)
                yield item(identifier, r['n'], folder, r.get('s'), r.get('te', r.get('t', 0)), pickcode=r.get('pc', ''))
            offset += len(rows)
            if offset >= int(d['count']):
                return
            if not rows:
                raise CloudError('115 目录未读取完整，请重试')

    def link(self, obj, agent):
        key = secrets.token_bytes(16)
        payload = cipher115.encode(json.dumps({'pickcode': obj['pickcode']}, separators=(',', ':')).encode(), key)
        d = self.request('https://proapi.115.com/app/chrome/downurl', 'POST',
                         query={'t': int(time.time())}, form={'data': payload}, headers={'User-Agent': agent})
        try:
            rows = json.loads(cipher115.decode(d['data'], key))
            row = next(iter(rows.values()))
            url = row.get('url')
            if not isinstance(url, dict) or not url.get('url'):
                raise ValueError('empty url')
            return valid_url(url['url']), {}
        except (ValueError, KeyError, StopIteration, TypeError):
            raise CloudError('115 未提供可播放地址，请检查该文件及账号下载权限') from None

    def mkdir(self, parent, name):
        d = self.request('/files/add', 'POST', form={'pid': parent, 'cname': name})
        return str(d['cid'])

    def move(self, obj, destination):
        self.request('/files/move', 'POST', form={'pid': destination, 'fid[0]': obj['id']})
        return dict(obj, parent=destination)


def sign123(path):
    now = time.time()
    stamp, random = str(int(now)), str(secrets.randbelow(10_000_001))
    date = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M')
    letters = 'adefghlmyijnopkqrstubcvwsz'
    key = str(zlib.crc32(''.join(letters[int(c)] for c in date).encode()))
    checksum = zlib.crc32('|'.join((stamp, random, path, 'web', '3', key)).encode())
    return {key: stamp+'-'+random+'-'+str(checksum)}


class Web123(Provider):
    root = '0'
    base = 'https://yun.123pan.com/b/api'

    def refresh(self):
        if not self.c.get('username') or not self.c.get('password'):
            raise CloudError('123 登录已过期，请更新网页登录令牌或填写账号密码')
        user = self.c['username']
        body = {'mail': user, 'password': self.c['password'], 'type': 2} if '@' in user else {
            'passport': user, 'password': self.c['password'], 'remember': True}
        d = self.raw('https://login.123pan.com/api/user/sign_in', 'POST', body=body,
                     headers={'Origin': 'https://yun.123pan.com', 'Referer': 'https://yun.123pan.com/',
                              'platform': 'web', 'app-version': '3', 'User-Agent': 'Dart/2.19(dart:io)'})
        if d.get('code') != 200 or not d.get('data', {}).get('token'):
            raise CloudError('123 登录失败，请先在官网完成短信验证，或复制有效的网页登录令牌')
        self.c['web_token'] = d['data']['token']
        self.save()

    def request(self, endpoint, method='GET', query=None, body=None, **kwargs):
        if not self.c.get('web_token'):
            self.refresh()
        for attempt in range(2):
            url = self.base+endpoint
            d = self.raw(url, method, query={**(query or {}), **sign123(urlsplit(url).path)}, body=body,
                         headers={'Authorization': 'Bearer '+self.c['web_token'],
                                  'Origin': 'https://yun.123pan.com', 'Referer': 'https://yun.123pan.com/',
                                  'platform': 'web', 'app-version': '3', 'User-Agent': 'Mozilla/5.0'})
            if d.get('code') == 401 and attempt == 0:
                self.refresh()
                continue
            if d.get('code') != 0 or d.get('_http_status', 200) >= 400:
                raise CloudError('123 请求失败（代码 '+str(d.get('code', ''))+'），请检查登录状态或网盘权限')
            return d

    def list(self, parent):
        page, seen = 1, set()
        while True:
            d = self.request('/file/list/new', query={'driveId': 0, 'limit': 100, 'next': 0,
                'orderBy': 'file_id', 'orderDirection': 'desc', 'parentFileId': parent, 'trashed': 'false',
                'SearchData': '', 'Page': page, 'OnlyLookAbnormalFile': 0, 'event': 'homeListFile',
                'operateType': 4, 'inDirectSpace': 'false'})['data']
            rows = d.get('InfoList')
            if not isinstance(rows, list):
                raise CloudError('123 目录响应不完整')
            for r in rows:
                identifier = str(r['FileId'])
                if identifier in seen:
                    raise CloudError('123 分页重复，请重新扫描')
                seen.add(identifier)
                if str(r.get('Trashed', 0)).lower() in ('1', 'true'):
                    continue
                yield item(identifier, r['FileName'], int(r['Type']) == 1, r.get('Size'), r.get('UpdateAt'),
                           etag=r.get('Etag', ''), s3key=r.get('S3KeyFlag', ''))
            total = int(d.get('Total', len(seen)))
            if str(d.get('Next')) == '-1' or len(seen) >= total:
                if len(seen) < total:
                    raise CloudError('123 目录数量不一致，请重试')
                return
            if not rows:
                raise CloudError('123 目录未读取完整，请重试')
            page += 1

    def link(self, obj, agent):
        d = self.request('/file/download_info', 'POST', body={'driveId': 0, 'etag': obj.get('etag', ''),
            'fileId': int(obj['id']), 'fileName': obj['name'], 's3keyFlag': obj.get('s3key', ''),
            'size': obj['size'], 'type': 0})['data']
        url = valid_url(d['DownloadUrl'])
        params = parse_qs(urlsplit(url).query).get('params')
        if params:
            try:
                url = valid_url(base64.b64decode(params[0]).decode())
            except (ValueError, UnicodeError):
                raise CloudError('123 下载链接格式无效') from None
        referer = 'https://yun.123pan.com/'
        return final_redirect(url, {'Referer': referer, 'User-Agent': agent}), {'Referer': referer}

    def mkdir(self, parent, name):
        self.request('/file/upload_request', 'POST', body={'driveId': 0, 'etag': '', 'fileName': name,
                     'parentFileId': parent, 'size': 0, 'type': 1})
        rows = [r for r in self.list(parent) if r['folder'] and r['name'] == name]
        if len(rows) != 1:
            raise CloudError('无法确认 123 新建文件夹')
        return rows[0]['id']

    def move(self, obj, destination):
        self.request('/file/mod_pid', 'POST', body={'fileIdList': [{'FileId': obj['id']}], 'parentFileId': destination})
        return dict(obj, parent=destination)
