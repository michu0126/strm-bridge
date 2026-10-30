"""Streaming 115 uploads. Only multipart state, never account keys, is checkpointed."""
import hashlib
import base64
import hmac
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree.ElementTree import fromstring, ParseError

from .cloud_providers import CloudError


class UploadCancelled(CloudError):
    pass


class UploadTransientError(CloudError):
    def __init__(self, phase, detail, http_status=None):
        self.phase, self.detail, self.http_status = phase, detail, http_status
        super().__init__('115 ' + phase + '暂时失败（' + detail + '）')


def multipart_init(sdk, session, token):
    """Match the empty OSS subresources used by the native 115 Go driver.

    p115oss 0.1.2's generic signer drops empty query values. Normalize and
    sign this one request explicitly, including sequential SHA1 processing.
    """
    def request(url, **kwargs):
        from urllib3_future_request import request as send
        headers = kwargs['headers']
        p = urlsplit(url)
        url = p._replace(query='sequential&uploads&x-oss-enable-sha1').geturl()
        oss_headers = '\n'.join(k + ':' + v for k, v in sorted(headers.items()) if k.startswith('x-oss-'))
        canonical = ('POST\n' + headers.get('content-md5', '') + '\n' + headers.get('content-type', '') +
                     '\n' + headers['date'] + '\n' + oss_headers + '\n/' + session['bucket'] + p.path + '?sequential&uploads&x-oss-enable-sha1')
        signature = base64.b64encode(hmac.digest(token['AccessKeySecret'].encode(), canonical.encode(), 'sha1')).decode()
        headers['authorization'] = 'OSS ' + token['AccessKeyId'] + ':' + signature
        return send(url=url, **kwargs)

    return sdk.oss_multipart_upload_init(session['object'], token=token, bucket=session['bucket'],
        endpoint='https://oss-cn-shenzhen.aliyuncs.com', params={'uploads': ''},
        headers={'content-type': 'application/octet-stream', 'user-agent': 'aliyun-sdk-android/2.9.1'}, request=request, timeout=120)


def digest(path, cancel=lambda: False, progress=lambda *a: None):
    total = Path(path).stat().st_size
    done, h = 0, hashlib.sha1()
    with open(path, 'rb') as source:
        while True:
            if cancel():
                raise UploadCancelled('上传已暂停')
            block = source.read(1024 * 1024)
            if not block:
                break
            h.update(block)
            done += len(block)
            progress('hashing', done, total)
    return h.hexdigest().upper()


def upload(client, path, parent, sha1, session=None, checkpoint=lambda d: None,
           progress=lambda *a: None, cancel=lambda: False):
    """Use the p115oss SDK; buffers stay bounded by one small part."""
    try:
        import p115oss as sdk
    except ImportError:
        raise CloudError('上传组件尚未安装，请使用包含上传功能的新镜像') from None
    path = Path(path)
    size = path.stat().st_size

    def check():
        if cancel():
            raise UploadCancelled('上传已暂停')

    phase = '读取上传权限'
    try:
        check()
        token = None
        if not session:
            info = client.request('https://proapi.115.com/app/uploadinfo', 'POST')
            if not info.get('user_id') or not info.get('userkey'):
                raise CloudError('115 未提供上传权限，请更新网盘登录授权')
            if str(info.get('upload_allowed', '1')).lower() in ('0', 'false'):
                raise CloudError('115 账号当前不允许上传')
            if info.get('size_limit') and size > int(info['size_limit']):
                raise CloudError('文件超过此 115 账号的单文件上传上限')
            phase = '初始化上传'
            initialized = sdk.upload_file_init(str(path), pid=parent, filename=path.name,
                filesize=size, filesha1=sha1, user_id=info['user_id'], user_key=info['userkey'], timeout=30)
            if not initialized.get('state'):
                raise CloudError('115 未接受上传，请检查账号权限或登录状态')
            if initialized.get('reuse'):
                check()
                progress('verifying', size, size)
                return {'rapid': True}
            data = initialized['data']
            # upload_file_init also returns the user key: explicitly whitelist fields.
            session = {k: data[k] for k in ('object', 'bucket', 'callback')}
            session.update(sha1=sha1, size=size, parent=parent,
                           partsize=max(8 * 1024 * 1024, ((size + 9998) // 9999 + 1048575) // 1048576 * 1048576))
            phase = '获取存储授权'
            token = sdk.upload_token(timeout=30)
            if size == 0:
                phase = '提交空文件'
                result = sdk.oss_upload(session['object'], b'', session['callback'],
                    bucket=session['bucket'], token=token, endpoint='https://oss-cn-shenzhen.aliyuncs.com', timeout=120)
                if not result.get('state'):
                    raise CloudError('115 未确认空文件上传成功')
                progress('verifying', 0, 0)
                return {'rapid': False}
            phase = '创建续传会话'
            session['upload_id'] = multipart_init(sdk, session, token)
            checkpoint(session)
        if session['sha1'] != sha1 or session['size'] != size or session['parent'] != parent:
            raise CloudError('上传续传记录与当前文件不一致，请重试此文件')
        phase = '获取存储授权'
        options = {'bucket': session['bucket'], 'token': token or sdk.upload_token(timeout=30),
                   'endpoint': 'https://oss-cn-shenzhen.aliyuncs.com', 'timeout': 120,
                   'headers': {'user-agent': 'aliyun-sdk-android/2.9.1'}}
        phase = '读取续传分片'
        parts = list(sdk.oss_multipart_part_iter(session['object'], session['upload_id'], **options))
        done = sum(p['Size'] for p in parts)
        if (done > size or any(int(p['PartNumber']) != i + 1 for i, p in enumerate(parts)) or
                any(p['Size'] != session['partsize'] for p in parts[:-1]) or
                (parts and parts[-1]['Size'] != session['partsize'] and done != size)):
            raise CloudError('115 续传分片不完整，请重试此文件')
        progress('uploading', done, size)

        def report(amount):
            nonlocal done
            check()
            done += amount
            progress('uploading', min(done, size), size)

        phase = '上传分片'
        with path.open('rb') as source:
            source.seek(done)
            for part in sdk.oss_multipart_upload_part_iter(session['object'], session['upload_id'], source,
                    part_number_start=len(parts) + 1, partsize=session['partsize'], reporthook=report, **options):
                parts.append(part)
                check()
        if sum(p['Size'] for p in parts) != size:
            raise CloudError('115 上传字节数不完整，等待重试')
        check()
        progress('verifying', size, size)
        phase = '提交上传结果'
        result = sdk.oss_multipart_upload_complete(session['object'], session['upload_id'],
                                                  parts, session['callback'], **options)
        if not result.get('state'):
            raise CloudError('115 上传回调未确认成功，等待核对目标目录')
        return {'rapid': False}
    except UploadCancelled:
        raise
    except CloudError as exc:
        if str(exc) == '网盘连接超时或返回了无效数据，请检查授权和网络':
            raise UploadTransientError(phase, '网盘连接超时或响应无效') from None
        raise
    except Exception as exc:
        code = ''
        try:
            code = fromstring(exc.fp.data).findtext('Code') or ''
        except (AttributeError, ValueError, TypeError, ParseError):
            pass
        if code in ('NoSuchUpload', 'InvalidPart') or 'NoSuchUpload' in str(exc) or 'InvalidPart' in str(exc):
            checkpoint(None)
            raise UploadTransientError(phase, '续传会话已失效') from None
        # SDK errors can contain signed URLs, callbacks or request payloads.
        detail = ('HTTP ' + str(exc.code)) if hasattr(exc, 'code') else type(exc).__name__
        if code and code.isalnum():
            detail += ' / ' + code
        if code == 'AccessDenied':
            raise CloudError('115 存储端拒绝上传（' + detail + '），请先确认 115 官网可以正常上传') from None
        status = getattr(exc, 'code', None)
        transient = (status in (401, 408, 429, 500, 502, 503, 504) or
            code in ('ExpiredToken', 'SecurityTokenExpired', 'RequestTimeout', 'ServiceUnavailable', 'InternalError', 'SlowDown') or
            type(exc).__name__ in ('JSONDecodeError', 'TimeoutError', 'ConnectTimeoutError', 'ReadTimeoutError',
                                  'ConnectionError', 'ProtocolError', 'RemoteDisconnected', 'URLError', 'SSLError', 'ParseError') or
            (isinstance(exc, IndexError) and phase == '初始化上传'))
        if transient:
            raise UploadTransientError(phase, detail, status) from None
        raise CloudError('115 ' + phase + '失败（' + detail + '），稍后可重试') from None
