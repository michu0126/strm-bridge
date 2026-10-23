"""Optional playback adapters; folder scanning never depends on a cloud service."""
from pathlib import Path, PurePosixPath
import base64
import json
import struct
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError

def valid_url(value):
    p = urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.netloc or p.username or p.password or any(c in value for c in '\r\n'):
        raise ValueError('请输入有效的 HTTP/HTTPS 地址，不能在地址中包含账号密码')
    return value

def cloud_path(local, config):
    p = Path(local).resolve()
    mappings = sorted(config.get('mappings', []), key=lambda m: len(m['local']), reverse=True)
    for m in mappings:
        root = Path(m['local']).resolve()
        if p.is_relative_to(root):
            return '/' + str(PurePosixPath(m['cloud'].lstrip('/')) / p.relative_to(root).as_posix()).strip('/')
    raise ValueError('未配置此挂载路径对应的直链目录；仅生成挂载路径时无需此设置')

def varint(n):
    data = bytearray()
    while n > 127:
        data.append((n & 127) | 128)
        n >>= 7
    data.append(n)
    return bytes(data)

def string_field(n, text):
    data = text.encode('utf-8')
    return varint(n << 3 | 2) + varint(len(data)) + data

def fields(data):
    pos = 0
    def readint():
        nonlocal pos
        n = shift = 0
        while pos < len(data) and shift <= 63:
            b = data[pos]
            pos += 1
            n |= (b & 127) << shift
            if b < 128:
                return n
            shift += 7
        raise ValueError('无效 protobuf 数据')
    out = {}
    while pos < len(data):
        key = readint()
        wire = key & 7
        if wire == 0:
            v = readint()
        elif wire == 2:
            size = readint()
            v = data[pos:pos + size]
            pos += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            v = data[pos:pos + size]
            pos += size
        else:
            raise ValueError('不支持的 protobuf 字段')
        out.setdefault(key >> 3, []).append(v)
    return out

def rpc(origin, method, payload=b'', token=''):
    valid_url(origin)
    req = Request(origin.rstrip('/') + '/clouddrive.CloudDriveFileSrv/' + method,
                  data=b'\0' + struct.pack('>I', len(payload)) + payload,
                  headers={'Content-Type': 'application/grpc-web+proto', 'X-Grpc-Web': '1',
                           **({'Authorization': 'Bearer ' + token} if token else {})})
    with build_opener(ProxyHandler({})).open(req, timeout=25) as response:
        data = response.read(4 * 1024 * 1024)
        if response.headers.get('grpc-status', '0') != '0':
            raise ValueError('CD2 拒绝请求，请检查认证')
    message, trailers = b'', b''
    while len(data) >= 5:
        flag, size = data[0], struct.unpack('>I', data[1:5])[0]
        if len(data) < size + 5:
            raise ValueError('CD2 响应不完整')
        block, data = data[5:5 + size], data[5 + size:]
        if flag & 128:
            trailers += block
        elif flag == 0:
            message += block
        else:
            raise ValueError('CD2 返回了不支持的压缩数据')
    trailer_text = trailers.decode('utf-8', errors='replace')
    status = next((x.partition(':')[2].strip() for x in trailer_text.splitlines() if x.startswith('grpc-status:')), '0')
    if status != '0':
        raise ValueError('CD2 API 请求失败（grpc-status=' + status + '），请检查认证、路径或版本')
    return fields(message)

def cd2_token(config):
    if config.get('cd2_token'):
        return config['cd2_token']
    if config.get('cd2_user') and config.get('cd2_password'):
        f = rpc(config['cd2_url'], 'GetToken', string_field(1, config['cd2_user']) + string_field(2, config['cd2_password']))
        if f.get(1, [0])[0] and f.get(3):
            return f[3][0].decode()
    raise ValueError('请先在连接设置中填写 CD2 API 令牌或账号密码')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def json_api(origin, endpoint, data, token='', user_agent=''):
    valid_url(origin)
    req = Request(origin.rstrip('/') + endpoint, data=json.dumps(data).encode(),
                  headers={'Content-Type': 'application/json',
                           **({'Authorization': token} if token else {}),
                           **({'User-Agent': user_agent} if user_agent else {})})
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=25) as r:
            result = json.loads(r.read(4 * 1024 * 1024))
    except HTTPError as e:
        raise ValueError('直链服务返回 HTTP ' + str(e.code)) from None
    if result.get('code') != 200:
        raise ValueError('直链服务请求失败：' + str(result.get('message', '未知错误')))
    return result.get('data') or {}


def openlist_token(config):
    if config.get('openlist_token'):
        return config['openlist_token']
    if config.get('openlist_user') and config.get('openlist_password'):
        return json_api(config['openlist_url'], '/api/auth/login',
                        {'username': config['openlist_user'], 'password': config['openlist_password']})['token']
    return ''


def resolve(local, config, user_agent=''):
    provider = config.get('provider', 'cd2')
    if provider == 'none':
        raise ValueError('未配置可选的直链服务。可直接选择「挂载路径」生成 STRM')
    if provider == 'template':
        path = cloud_path(local, config)
        template = config.get('url_template', '')
        if '{path}' not in template:
            raise ValueError('HTTP 地址模板需要包含 {path}')
        return valid_url(template.replace('{path}', quote(path, safe='/')))
    if provider == 'openlist':
        path = cloud_path(local, config)
        origin = config['openlist_url'].rstrip('/')
        info = json_api(origin, '/api/fs/get', {'path': path, 'password': ''}, openlist_token(config), user_agent)
        direct = valid_url(info.get('raw_url', ''))
        parsed, base = urlsplit(direct), urlsplit(origin)
        if parsed.netloc == base.netloc and parsed.path.startswith(('/p/', '/proxy/')):
            raise ValueError('当前存储开启了代理，无法提供网盘直链；请在存储设置中关闭 Web 代理')
        return direct
    if provider != 'cd2':
        raise ValueError('未知的直链连接方式')
    return resolve_cd2(local, config, user_agent)


def test_connection(config):
    provider = config.get('provider', 'cd2')
    if provider == 'openlist':
        json_api(config['openlist_url'], '/api/fs/list', {'path': '/', 'page': 1, 'per_page': 1}, openlist_token(config))
        return '文件服务已连接。扫描文件夹不依赖此连接'
    if provider == 'cd2':
        rpc(config['cd2_url'], 'GetSystemInfo')
        return 'CD2 服务已连接'
    if provider == 'template':
        valid_url(config.get('url_template', '').replace('{path}', '/test'))
        return '地址模板有效。请选择视频测试实际跳转'
    return '独立文件夹模式，无需连接其他容器'


def resolve_cd2(local, config, user_agent=''):
    path = cloud_path(local, config)
    origin = config['cd2_url'].rstrip('/')
    token = cd2_token(config)
    # v1.1.2 request field 4 is get_direct_url. Field 3 is lazy_read, left false.
    info = rpc(origin, 'GetDownloadUrlPath', string_field(1, path) + b'\x20\x01', token)
    direct = info.get(3, [b''])[0].decode()
    required_agent = info.get(4, [b''])[0].decode()
    headers = {}
    for entry in info.get(5, []):
        pair = fields(entry)
        headers[pair.get(1, [b''])[0].decode()] = pair.get(2, [b''])[0].decode()
    if headers:
        raise ValueError('此网盘直链要求附加请求头，普通 HTTP 302 无法传递。请使用支持相应请求头的播放器或网盘配置')
    if direct:
        valid_url(direct)
        if required_agent and user_agent != required_agent:
            raise ValueError('此网盘直链要求特定 User-Agent，请将播放器 User-Agent 设置为：' + required_agent)
        return direct
    download = info.get(1, [b''])[0].decode()
    if not download:
        raise ValueError('CD2 未返回直链，请在该云盘设置中启用「支持直链」并检查会员状态')
    parsed_origin = urlsplit(origin)
    download = download.replace('{SCHEME}', parsed_origin.scheme).replace('{HOST}', parsed_origin.netloc).replace('{PREVIEW}', 'False')
    url = urljoin(origin + '/', download)
    valid_url(url)
    # A CD2 redirect-only endpoint is resolved without following it or reading media data.
    req = Request(url, method='GET', headers={'User-Agent': user_agent or 'STRMBridge/1.0', 'Range': 'bytes=0-0', 'Authorization': 'Bearer ' + token})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=25) as r:
            raise ValueError('CD2 返回媒体流而非直链，请启用 CD2 的直链下载或使用支持直链的网盘')
    except HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            location = urljoin(url, e.headers.get('Location', ''))
            valid_url(location)
            return location
        raise ValueError('CD2 下载接口返回 HTTP ' + str(e.code)) from None
