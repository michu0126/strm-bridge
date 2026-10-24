"""Short-lived 115 QR login sessions; credentials are saved only by account save."""
import threading
import time
import uuid
from urllib.parse import urlencode

from .cloud_providers import http_json, CloudError
from .cloud_cookie import normalize_cookie


class CloudLogin:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}

    def start(self, provider):
        if provider != '115':
            raise CloudError('此网盘请使用表单中的授权入口')
        d = http_json('https://qrcodeapi.115.com/api/1.0/web/1.0/token/')
        if not d.get('state') or not all(d.get('data', {}).get(k) for k in ('uid', 'time', 'sign')):
            raise CloudError('115 二维码获取失败，请稍后重试或粘贴 Cookie')
        token = {k: d['data'][k] for k in ('uid', 'time', 'sign')}
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > time.time()}
            if len(self.sessions) >= 20:
                raise CloudError('请关闭多余登录窗口后重试')
            identifier = uuid.uuid4().hex
            self.sessions[identifier] = {'token': token, 'expires': time.time()+240, 'polling': False}
        return {'id': identifier, 'image': 'https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode?'+urlencode({'uid': token['uid']}),
                'message': '使用 115 App 扫码并确认；同类型网页登录可能会下线。'}

    def status(self, identifier):
        with self.lock:
            s = self.sessions.get(identifier)
            if not s or s['expires'] <= time.time():
                self.sessions.pop(identifier, None)
                return {'status': 'expired', 'message': '二维码已过期，请刷新'}
            if s['polling']:
                return {'status': 'waiting', 'message': '等待扫码确认'}
            s['polling'] = True
        try:
            d = http_json('https://qrcodeapi.115.com/get/status/', query=s['token'])
            status = d.get('data', {}).get('status')
            if status == 2:
                result = http_json('https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/', 'POST',
                                   form={'app': 'web', 'account': s['token']['uid']})
                cookie = result.get('data', {}).get('cookie', {})
                if not result.get('state') or not all(cookie.get(k) for k in ('UID', 'CID', 'SEID')):
                    raise CloudError('115 扫码登录未取得 Cookie，请重新扫码')
                with self.lock:
                    self.sessions.pop(identifier, None)
                return {'status': 'confirmed', 'cookie': normalize_cookie('; '.join(k+'='+str(v) for k, v in cookie.items())),
                        'message': '登录成功，点击保存并连接完成添加'}
            if status in (-1, -2) or (not d.get('state') and d.get('code') in (40199002, 40199001)):
                with self.lock:
                    self.sessions.pop(identifier, None)
                return {'status': 'expired', 'message': '二维码已过期或取消，请刷新'}
            return {'status': 'scanned' if status == 1 else 'waiting',
                    'message': '已扫码，请在手机上确认' if status == 1 else '等待 115 App 扫码'}
        finally:
            with self.lock:
                s['polling'] = False
