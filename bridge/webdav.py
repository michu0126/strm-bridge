"""Read-only cloud folders for WebDAV players; video bytes stay on the CDN."""
import base64
import binascii
from email.utils import formatdate
import hashlib
import hmac
import html
import mimetypes
import threading
import time
from urllib.parse import quote, unquote, urlsplit
import xml.etree.ElementTree as ET

from .cloud_providers import CloudError

DAV = 'DAV:'
ET.register_namespace('D', DAV)
ALLOW = 'OPTIONS, PROPFIND, GET, HEAD'
PROPERTIES = ('displayname', 'resourcetype', 'getcontentlength', 'getcontenttype',
              'getlastmodified', 'creationdate', 'getetag', 'supportedlock', 'lockdiscovery')


class Missing(ValueError):
    pass


class DirectoryCache:
    def __init__(self, state):
        self.state = state
        self.lock = threading.Lock()
        self.rows = {}

    def children(self, account, node):
        clouds = self.state.clouds
        with clouds.client(account) as client:
            key = (account, clouds.account(account)['revision'], node['id'])
            now = time.monotonic()
            with self.lock:
                cached = self.rows.get(key)
                if cached and now - cached[0] < 10:
                    return cached[1]
            rows = clouds.entries(account, node['id'], client)
            with self.lock:
                if len(self.rows) >= 128:
                    self.rows.pop(next(iter(self.rows)))
                self.rows[key] = (time.monotonic(), rows)
            return rows


def is_path(path):
    return path == '/dav' or path.startswith('/dav/')


def _authorized(handler):
    if handler.authorized():
        return True
    try:
        scheme, value = handler.headers.get('Authorization', '').split(' ', 1)
        if scheme.lower() != 'basic':
            return False
        user, password = base64.b64decode(value, validate=True).decode('utf-8').split(':', 1)
        return user == 'admin' and hmac.compare_digest(password.encode(), handler.state.password.encode())
    except (ValueError, UnicodeError, binascii.Error):
        return False


def _text(value):
    return ''.join(c if ord(c) in (9, 10, 13) or 32 <= ord(c) <= 0xD7FF or
                   0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF else '\ufffd'
                   for c in str(value))


def _root():
    return dict(id='', name='我的网盘', folder=True, size=0, modified=0)


def _locate(state, path):
    if len(path) > 8192:
        raise Missing()
    raw = path[len('/dav'):].strip('/')
    parts = [unquote(p, errors='strict') for p in raw.split('/')] if raw else []
    if len(parts) > 64 or any(not p or p in ('.', '..') or any(c in p for c in '/\\\x00') for p in parts):
        raise Missing()
    if not parts:
        return None, _root(), '/dav/'
    account = next((a for a in state.clouds.list_accounts() if a['id'] == parts[0]), None)
    if account is None:
        raise Missing()
    node = dict(state.clouds.node(account['id'], account['root']), name=account['name'])
    for part in parts[1:]:
        if not node['folder']:
            raise Missing()
        node = next((n for n in state.dav_cache.children(account['id'], node) if n['name'] == part), None)
        if node is None:
            raise Missing()
    canonical = '/dav/' + '/'.join(parts) + ('/' if node['folder'] else '')
    return account['id'], node, canonical


def _children(state, account, node, path):
    if account is None:
        return [(a['id'], dict(state.clouds.node(a['id'], a['root']), name=a['name']),
                 '/dav/' + a['id'] + '/') for a in state.clouds.list_accounts()]
    return [(account, n, path + n['name'] + ('/' if n['folder'] else ''))
            for n in sorted(state.dav_cache.children(account, node), key=lambda n: (not n['folder'], n['name'].casefold()))]


def _etag(account, node):
    value = repr((account, node['id'], node.get('size', 0), node.get('modified', 0)))
    return '"' + hashlib.sha256(value.encode()).hexdigest()[:24] + '"'


def _property(name, account, node, names_only=False):
    element = ET.Element('{' + DAV + '}' + name)
    if names_only:
        return element
    stamp = max(0, min(float(node.get('modified') or 0), 253402300799))
    values = {'displayname': _text(node['name']), 'getcontentlength': str(node.get('size', 0)),
              'getcontenttype': 'httpd/unix-directory' if node['folder'] else (mimetypes.guess_type(node['name'])[0] or 'application/octet-stream'),
              'getlastmodified': formatdate(stamp, usegmt=True),
              'creationdate': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stamp)),
              'getetag': _etag(account, node)}
    if name == 'resourcetype' and node['folder']:
        ET.SubElement(element, '{DAV:}collection')
    elif name in values:
        element.text = values[name]
    return element


def _propfind(handler, account, node, path):
    depth = handler.headers.get('Depth', '1').lower()
    if depth == 'infinity':
        return handler.respond(b'<D:error xmlns:D="DAV:"><D:propfind-finite-depth/></D:error>',
                               403, 'application/xml; charset=utf-8')
    if depth not in ('0', '1'):
        return handler.respond({'error': 'Depth 仅支持 0 或 1'}, 400)
    length = int(handler.headers.get('Content-Length', '0'))
    if not 0 <= length <= 65536:
        return handler.respond({'error': '目录请求过大'}, 413)
    body = handler.rfile.read(length)
    requested, names_only = None, False
    if body:
        if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
            raise ValueError('无效的 XML 请求')
        request = ET.fromstring(body)
        if request.tag != '{DAV:}propfind':
            raise ValueError('无效的目录请求')
        prop = request.find('{DAV:}prop')
        if prop is not None:
            requested = [p.tag for p in prop]
        names_only = request.find('{DAV:}propname') is not None
    resources = [(account, node, path)]
    if depth == '1' and node['folder']:
        resources.extend(_children(handler.state, account, node, path))
    document = ET.Element('{DAV:}multistatus')
    for aid, entry, href in resources:
        response = ET.SubElement(document, '{DAV:}response')
        ET.SubElement(response, '{DAV:}href').text = quote(href, safe='/')
        found, missing = [], []
        for tag in requested if requested is not None else ['{DAV:}' + n for n in PROPERTIES]:
            name = tag[len('{DAV:}'):] if tag.startswith('{DAV:}') else ''
            if name in PROPERTIES:
                found.append(_property(name, aid, entry, names_only))
            else:
                missing.append(ET.Element(tag))
        for properties, status in [(found, '200 OK'), (missing, '404 Not Found')]:
            if not properties:
                continue
            propstat = ET.SubElement(response, '{DAV:}propstat')
            prop = ET.SubElement(propstat, '{DAV:}prop')
            prop.extend(properties)
            ET.SubElement(propstat, '{DAV:}status').text = 'HTTP/1.1 ' + status
    return handler.respond(ET.tostring(document, encoding='utf-8', xml_declaration=True),
                           207, 'application/xml; charset=utf-8', {'DAV': '1'})


def handle(handler):
    if not handler.state.config.get('webdav_enabled'):
        return handler.respond({'error': 'WebDAV 尚未启用'}, 404)
    if not _authorized(handler):
        return handler.respond({'error': '请使用工作台管理密码登录'}, 401, headers={
            'WWW-Authenticate': 'Basic realm="STRM Bridge WebDAV", charset="UTF-8"'})
    if handler.command == 'OPTIONS':
        return handler.respond(b'', 200, 'text/plain', {'DAV': '1', 'Allow': ALLOW})
    if handler.command not in ('PROPFIND', 'GET', 'HEAD'):
        return handler.respond({'error': 'WebDAV 仅支持浏览和播放'}, 405, headers={'Allow': ALLOW})
    try:
        account, node, path = _locate(handler.state, urlsplit(handler.path).path)
        if handler.command == 'PROPFIND':
            return _propfind(handler, account, node, path)
        if node['folder']:
            links = ['<li><a href="' + quote(href, safe='/') + '">' + html.escape(_text(n['name'])) +
                     ('/' if n['folder'] else '') + '</a></li>' for _, n, href in _children(handler.state, account, node, path)]
            body = ('<!doctype html><meta charset="utf-8"><title>我的网盘</title><h1>' +
                    html.escape(_text(node['name'])) + '</h1><ul>' + ''.join(links) + '</ul>').encode()
            return handler.respond(body, 200, 'text/html; charset=utf-8', {'DAV': '1'})
        headers = {'ETag': _etag(account, node), 'Accept-Ranges': 'bytes', 'DAV': '1'}
        if handler.command == 'HEAD':
            handler.send_response(200)
            handler.send_header('Content-Type', mimetypes.guess_type(node['name'])[0] or 'application/octet-stream')
            handler.send_header('Content-Length', str(node['size']))
            for key, value in headers.items():
                handler.send_header(key, value)
            handler.end_headers()
            return
        location = handler.state.clouds.resolve({'account': account, 'object': node}, handler.headers.get('User-Agent', ''))
        return handler.respond(b'', 302, 'application/octet-stream', dict(headers, Location=location, Vary='User-Agent'))
    except Missing:
        return handler.respond({'error': '网盘目录或文件不存在'}, 404)
    except CloudError as exc:
        return handler.respond({'error': str(exc)}, 502)
    except (ValueError, UnicodeError, ET.ParseError):
        return handler.respond({'error': 'WebDAV 请求无效'}, 400)
    except (OSError, KeyError):
        return handler.respond({'error': '网盘暂时不可用，请检查连接'}, 502)
