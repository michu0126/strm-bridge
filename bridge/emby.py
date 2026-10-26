"""Optional Emby video gateway; reuse each player's existing Emby authorization."""
import hmac
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .core import inside
from .resolver import resolve, valid_url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def status(state):
    with state.lock:
        return dict(configured=bool(state.config.get('emby_url')), **getattr(state, 'emby_stats', {}))


def record(state, kind, item, code):
    with state.lock:
        stats = getattr(state, 'emby_stats', {})
        stats[kind] = stats.get(kind, 0) + 1
        stats.update(last_result=kind, last_item=item, last_status=code, last_time=time.time())
        state.emby_stats = stats


def _credentials(handler, query):
    authorization = handler.headers.get('X-Emby-Authorization') or handler.headers.get('Authorization', '')
    fields = dict((k.lower(), quoted or bare) for k, quoted, bare in re.findall(
        r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', authorization))
    token = handler.headers.get('X-Emby-Token') or query.get('x-emby-token', [''])[0] or query.get('api_key', [''])[0] or fields.get('token', '')
    user = fields.get('userid') or query.get('userid', [''])[0]
    return token, user


def _source(state, source):
    path = source.get('Path', '')
    if not isinstance(path, str):
        return None
    parts = urlsplit(path).path.split('/')
    if len(parts) != 5 or parts[1] != 'play' or not hmac.compare_digest(state.sign(parts[2]), parts[3]):
        return None
    with state.lock:
        row = state.db.execute('SELECT path FROM media WHERE id=?', (parts[2],)).fetchone()
    return row[0] if row else None


def _relay(handler, opener, url, item):
    # Preserve normal Emby behavior for local media, subtitles, and transcoding.
    headers = {k: v for k, v in handler.headers.items() if k.lower() in {
        'authorization', 'x-emby-authorization', 'x-emby-token', 'user-agent',
        'range', 'if-range', 'if-none-match', 'if-modified-since', 'accept',
        'host', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-port', 'x-forwarded-proto'}}
    try:
        response = opener.open(Request(url, headers=headers, method=handler.command), timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        record(handler.state, 'fallbacks', item, response.status)
        handler.send_response(response.status)
        allowed = {'content-type', 'content-length', 'content-range', 'accept-ranges',
                   'content-disposition', 'etag', 'last-modified', 'location', 'cache-control'}
        for key, value in response.headers.items():
            if key.lower() in allowed:
                handler.send_header(key, value)
        handler.send_header('X-STRM-Bridge', 'emby-relay')
        handler.send_header('Connection', 'close')
        handler.close_connection = True
        handler.end_headers()
        if handler.command != 'HEAD':
            try:
                while True:
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    handler.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                pass


def playback(handler):
    state = handler.state
    base = state.config.get('emby_url', '').rstrip('/')
    if not base:
        return handler.respond({'error': '请先设置 Emby 服务地址'}, 404)
    parsed = urlsplit(handler.path)
    relative = parsed.path[len('/emby-play/'):]
    item, separator, tail = relative.partition('/')
    if not separator or not re.fullmatch(r'[a-fA-F0-9-]{1,64}', item) or any(
            segment in ('.', '..') for segment in unquote(relative).split('/')):
        return handler.respond({'error': '影片请求路径无效'}, 400)
    query = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
    token, user = _credentials(handler, query)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    upstream = base + '/Videos/' + relative + ('?' + parsed.query if parsed.query else '')
    original = re.fullmatch(r'(?:original|stream)(?:\.[a-zA-Z0-9]+)?', tail, re.I)
    transcode = query.get('static', [''])[0].lower() == 'false' or any(
        query.get(key, ['copy'])[0].lower() != 'copy' for key in ('videocodec', 'audiocodec'))
    try:
        if not original or transcode or not token or not re.fullmatch(r'[a-fA-F0-9-]{1,64}', user):
            return _relay(handler, opener, upstream, item)
        # This user-scoped Emby endpoint checks the caller's token and library access.
        info_url = base + '/Users/' + user + '/Items/' + item
        try:
            with opener.open(Request(info_url, headers={'X-Emby-Token': token,
                    'User-Agent': handler.headers.get('User-Agent', '')}), timeout=15) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError('Emby 影片信息过大')
                info = json.loads(data)
                if not isinstance(info, dict):
                    raise ValueError('Emby 影片信息无效')
        except HTTPError as exc:
            code = exc.code if exc.code in (401, 403, 404) else 502
            exc.close()
            record(state, 'errors', item, code)
            return handler.respond({'error': 'Emby 拒绝了此影片请求'}, code)
        sources = [source for source in (info.get('MediaSources') or []) if isinstance(source, dict)]
        selected = query.get('mediasourceid', [''])[0]
        source = next((s for s in sources if str(s.get('Id')) == selected), None) if selected else next(iter(sources), None)
        stored = _source(state, source) if source else None
        if stored is None:
            return _relay(handler, opener, upstream, item)
        agent = handler.headers.get('User-Agent', '')
        if stored.startswith('cloud:'):
            location = state.clouds.resolve(json.loads(stored[6:]), agent)
        else:
            location = resolve(inside(stored, state.config['source_roots']), dict(state.config), agent)
        record(state, 'redirects', item, 302)
        return handler.respond(b'', 302, 'application/octet-stream', {
            'Location': valid_url(location), 'Vary': 'User-Agent', 'X-STRM-Bridge': 'emby-302'})
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        record(state, 'errors', item, 502)
        return handler.respond({'error': 'Emby 直链请求失败，请检查连接和网盘授权'}, 502)
