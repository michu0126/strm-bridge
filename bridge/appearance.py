"""Wallpaper storage and a daily cache of Bing's public wallpaper."""
from datetime import date
import hashlib
import json
from pathlib import Path
import threading
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

MAX_IMAGE = 10 * 1024 * 1024


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'):
        return 'image/avif'
    raise ValueError('请选择 JPG、PNG、WebP、GIF 或 AVIF 图片')


class Wallpapers:
    def __init__(self, data):
        self.root = Path(data) / 'appearance'
        self.root.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.bing_lock = threading.Lock()

    def status(self):
        p = self.root / 'custom'
        return {'custom_url': '/api/appearance/image/custom?v=' + str(p.stat().st_mtime_ns) if p.exists() else ''}

    def upload(self, data):
        if not data or len(data) > MAX_IMAGE:
            raise ValueError('背景图片需要在 10MB 以内')
        image_type(data)
        with self.lock:
            temp = self.root / 'custom.tmp'
            temp.write_bytes(data)
            temp.replace(self.root / 'custom')
        return {'url': '/api/appearance/image/custom?v=' + hashlib.sha256(data).hexdigest()[:16]}

    def image(self, name):
        if name != 'custom' and (not name.startswith('bing-') or len(name) != 13 or not name[5:].isdigit()):
            raise ValueError('背景图片不存在')
        data = (self.root / name).read_bytes()
        return data, image_type(data)

    def bing(self):
        today = date.today().strftime('%Y%m%d')
        name = 'bing-' + today
        meta = self.root / (name + '.json')
        with self.bing_lock:
            if meta.exists() and (self.root / name).exists():
                try:
                    return json.loads(meta.read_text(encoding='utf-8'))
                except (ValueError, OSError):
                    pass
            opener = build_opener(ProxyHandler({}))
            last_error = None
            for origin in ('https://www.bing.com', 'https://cn.bing.com'):
                try:
                    req = Request(origin + '/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=zh-CN', headers={'User-Agent': 'STRMBridge/1.3'})
                    with opener.open(req, timeout=12) as r:
                        info = json.loads(r.read(1024 * 1024))['images'][0]
                    url = urljoin(origin, info['url'])
                    parsed = urlsplit(url)
                    if parsed.scheme != 'https' or not (parsed.hostname == 'bing.com' or parsed.hostname.endswith('.bing.com')):
                        raise ValueError('Bing 返回的图片地址无效')
                    with opener.open(Request(url, headers={'User-Agent': 'STRMBridge/1.3'}), timeout=18) as r:
                        data = r.read(MAX_IMAGE + 1)
                    if len(data) > MAX_IMAGE:
                        raise ValueError('Bing 壁纸超过大小限制')
                    image_type(data)
                    temp = self.root / (name + '.tmp')
                    temp.write_bytes(data);temp.replace(self.root / name)
                    result = {'url': '/api/appearance/image/' + name, 'copyright': info.get('copyright', ''), 'date': today}
                    meta.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
                    keep = sorted(self.root.glob('bing-????????'), reverse=True)[:3]
                    for old in self.root.glob('bing-????????'):
                        if old not in keep:
                            old.unlink(missing_ok=True)
                            old.with_suffix('.json').unlink(missing_ok=True)
                    return result
                except Exception as e:
                    last_error = e
            raise ValueError('暂时无法获取 Bing 每日壁纸，请稍后重试或上传图片') from last_error
