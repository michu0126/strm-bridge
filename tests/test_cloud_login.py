import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from bridge.cloud_cookie import Cookie115, Web123, normalize_cookie, sign123
from bridge.cloud_login import CloudLogin
from bridge.cloud_providers import Aliyun, Baidu, CloudError, create_provider
from bridge.server import State
from bridge import cipher115


class LoginProtocols(unittest.TestCase):
    def test_cookie_accepts_header_prefix_and_trailing_semicolon(self):
        self.assertEqual(normalize_cookie(' Cookie: UID=u; CID=c; SEID=s; '), 'UID=u; CID=c; SEID=s')
        with self.assertRaises(CloudError): normalize_cookie('x=y\r\nInjected: yes')

    def test_115_cookie_listing_and_pagination(self):
        client = Cookie115({'cookie': 'UID=u;CID=c;SEID=s'})
        with patch.object(client, 'raw', side_effect=[
            {'state': True, 'count': 2, 'data': [{'cid': '10', 'n': '电影', 't': '1700000000'}]},
            {'state': True, 'count': 2, 'data': [{'fid': '20', 'cid': '0', 'n': 'film.mkv', 's': '100', 'pc': 'pick', 't': '2026-10-23 15:00'}]}
        ]) as call:
            rows = list(client.list('0'))
        self.assertTrue(rows[0]['folder']); self.assertFalse(rows[1]['folder'])
        self.assertEqual(rows[1]['pickcode'], 'pick'); self.assertGreater(rows[1]['modified'], 0)
        self.assertEqual(call.call_args_list[1].kwargs['query']['offset'], 1)
        self.assertIn('Cookie', call.call_args.kwargs['headers'])
        self.assertNotIn('Authorization', call.call_args.kwargs['headers'])

    def test_115_cookie_failure_is_not_empty_directory(self):
        client = Cookie115({'cookie': 'x'})
        for response in ({'state': False, 'errno': 99}, {'state': True, 'count': 2, 'data': []}):
            with patch.object(client, 'raw', return_value=response):
                with self.assertRaises(CloudError): list(client.list('0'))

    def test_115_large_directory_keeps_full_paging_api_and_browser_agent(self):
        client = Cookie115({'cookie': 'UID=u;CID=c;SEID=s'})
        def page(url, method='GET', **kwargs):
            self.assertEqual(url, 'https://webapi.115.com/files')
            self.assertIn('Chrome/', kwargs['headers']['User-Agent'])
            self.assertNotIn('115Browser/', kwargs['headers']['User-Agent'])
            query = kwargs['query']
            self.assertEqual(query['aid'], 1)
            self.assertEqual(query['fc_mix'], 1)
            offset = query['offset']
            return {'state': True, 'count': 1401, 'data': [
                {'cid': str(i), 'n': 'folder-'+str(i)}
                for i in range(offset, min(offset + 200, 1401))]}
        with patch.object(client, 'raw', side_effect=page) as call:
            self.assertEqual(len(list(client.list('0'))), 1401)
        self.assertEqual(call.call_args_list[-1].kwargs['query']['offset'], 1400)

    def test_115_upload_during_paging_does_not_shift_previously_read_items(self):
        client = Cookie115({'cookie': 'x'})
        objects = [{'cid': str(i), 'n': 'ZZZ-'+str(i).zfill(4), 't': i} for i in range(400)]
        def page(url, method='GET', **kwargs):
            query = kwargs['query']
            rows = sorted(objects, key=lambda r: r['t'] if query['o'] == 'user_ptime' else r['n'])
            offset = query['offset']
            response = {'state': True, 'count': len(rows), 'data': rows[offset:offset+200]}
            if offset == 0:
                objects.append({'cid': '400', 'n': 'AAA-new-upload', 't': 400})
            return response
        with patch.object(client, 'raw', side_effect=page):
            rows = list(client.list('0'))
        self.assertEqual({r['id'] for r in rows}, {str(i) for i in range(401)})

    def test_115_directory_failure_is_not_an_empty_listing(self):
        client = Cookie115({'cookie': 'x'})
        with patch.object(client, 'raw', side_effect=CloudError('HTTP 405')) as call:
            with self.assertRaises(CloudError): list(client.list('0'))
        self.assertEqual(call.call_count, 1)

    def test_115_repeated_directory_page_is_rejected(self):
        client = Cookie115({'cookie': 'x'})
        response = {'state': True, 'count': 2, 'data': [{'cid': '10', 'n': 'folder'}]}
        with patch.object(client, 'raw', return_value=response) as call:
            with self.assertRaises(CloudError): list(client.list('0'))
        self.assertEqual(call.call_count, 2)

    def test_115_download_uses_pickcode_encryption_and_player_agent(self):
        client = Cookie115({'cookie': 'x'})
        with patch.object(client, 'raw', return_value={'state': True, 'data': 'encrypted'}) as call, patch('bridge.cipher115.decode', return_value=b'{"123":{"url":{"url":"https://cdn.example/movie"}}}'), patch('bridge.cipher115.encode', return_value='WIRE') as enc:
            link, headers = client.link({'pickcode': 'pick'}, 'EmbyPlayer')
        self.assertEqual(json.loads(enc.call_args.args[0]), {'pickcode': 'pick'})
        self.assertEqual(call.call_args.kwargs['form'], {'data': 'WIRE'})
        self.assertEqual(call.call_args.kwargs['headers']['User-Agent'], 'EmbyPlayer')
        self.assertEqual(link, 'https://cdn.example/movie'); self.assertFalse(headers)

    def test_115_bad_download_does_not_return_false_success(self):
        client = Cookie115({'cookie': 'x'})
        with patch.object(client, 'raw', return_value={'state': True, 'data': 'not base64'}):
            with self.assertRaises(CloudError): client.link({'pickcode': 'p'}, 'Emby')

    def test_123_password_signin_and_signed_requests(self):
        client = Web123({'username': 'user@example.com', 'password': 'secret'}, Mock())
        with patch.object(client, 'raw', side_effect=[{'code': 200, 'data': {'token': 'logged-in'}},
            {'code': 0, 'data': {'InfoList': [{'FileId': 20, 'FileName': 'film.mkv', 'Type': 0, 'Size': 100, 'Etag': 'hash', 'S3KeyFlag': 'flag'}], 'Next': '-1', 'Total': 1}}]) as call:
            rows = list(client.list('0'))
        self.assertEqual(call.call_args_list[0].kwargs['body']['mail'], 'user@example.com')
        self.assertEqual(call.call_args.kwargs['headers']['Authorization'], 'Bearer logged-in')
        self.assertEqual(rows[0]['s3key'], 'flag'); client.save.assert_called_once()
        self.assertTrue(any(k.isdigit() for k in call.call_args.kwargs['query']))

    def test_123_expiry_relogs_only_once(self):
        client = Web123({'web_token': 'old', 'username': '13800000000', 'password': 'pw'})
        with patch.object(client, 'raw', side_effect=[{'code': 401}, {'code': 200, 'data': {'token': 'new'}}, {'code': 401}]) as call:
            with self.assertRaises(CloudError): list(client.list('0'))
        self.assertEqual(call.call_count, 3)

    def test_123_risk_verification_is_reported(self):
        client = Web123({'username': 'u', 'password': 'p'})
        with patch.object(client, 'raw', return_value={'code': 400, 'message': 'captcha'}):
            with self.assertRaisesRegex(CloudError, '短信验证'): list(client.list('0'))

    def test_online_refresh_matches_documented_services(self):
        for cls, service, driver in [(Aliyun, 'alicloud', 'alicloud_qr'), (Baidu, 'baiduyun', 'baiduyun_go')]:
            client = cls({'auth_mode': 'online', 'refresh_token': 'original'}, Mock())
            with patch.object(client, 'raw', return_value={'access_token': 'new-access', 'refresh_token': 'rotated'}) as call:
                client.refresh()
            self.assertEqual(call.call_args.args[0], 'https://api.oplist.org/'+service+'/renewapi')
            self.assertEqual(call.call_args.kwargs['query']['driver_txt'], driver)
            self.assertEqual(client.c['refresh_token'], 'rotated'); client.save.assert_called_once()

    def test_direct_oauth_never_uses_relay(self):
        client = Aliyun({'auth_mode': 'open', 'refresh_token': 'r', 'client_id': 'id', 'client_secret': 'secret'})
        with patch.object(client, 'raw', return_value={'access_token': 'a', 'refresh_token': 'r'}) as call: client.refresh()
        self.assertTrue(call.call_args.args[0].startswith('https://openapi.alipan.com/'))

    def test_qr_confirm_exchange_and_one_time_result(self):
        login = CloudLogin()
        responses = [{'state': True, 'data': {'uid': 'qr-id', 'time': 123, 'sign': 'sig'}},
                     {'state': True, 'data': {'status': 1}}, {'state': True, 'data': {'status': 2}},
                     {'state': True, 'data': {'cookie': {'UID': 'u', 'CID': 'c', 'SEID': 's'}}}]
        with patch('bridge.cloud_login.http_json', side_effect=responses):
            qr = login.start('115')
            self.assertEqual(login.status(qr['id'])['status'], 'scanned')
            result = login.status(qr['id'])
            self.assertEqual(result['status'], 'confirmed'); self.assertIn('SEID=s', result['cookie'])
            self.assertEqual(login.status(qr['id'])['status'], 'expired')

    def test_qr_expiry_does_not_request_network(self):
        login = CloudLogin(); login.sessions['expired'] = {'expires': 0}
        with patch('bridge.cloud_login.http_json') as call:
            self.assertEqual(login.status('expired')['status'], 'expired')
            self.assertFalse(call.called)

    def test_cipher_request_wire_size_and_rejects_malformed_response(self):
        import base64
        self.assertEqual(len(base64.b64decode(cipher115.encode(b'{"pickcode":"x"}', bytes(range(16))))), 128)
        with self.assertRaises(ValueError): cipher115.decode('AAAA', bytes(16))


class AccountLoginModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.state = State(Path(self.tmp.name)/'data')
    def tearDown(self):
        self.state.watcher.close(); self.state.pool.shutdown(wait=True); self.state.db.close(); self.tmp.cleanup()
    def save(self, provider, credentials, **kwargs):
        return self.state.clouds.save({'provider': provider, 'credentials': credentials, **kwargs})

    def test_cookie_saved_redacted_and_reused_without_open_token(self):
        a = self.save('115', {'auth_mode': 'cookie', 'cookie': 'Cookie: UID=u;CID=c;SEID=s;'})
        self.assertNotIn('SEID=s', json.dumps(a)); self.assertIn('cookie', a['configured'])
        with self.state.clouds.client(a['id']) as client: self.assertIsInstance(client, Cookie115)
        self.save('115', {'auth_mode': 'cookie', 'cookie': ''}, id=a['id'])
        self.assertEqual(self.state.clouds.accounts[a['id']]['credentials']['cookie'], 'UID=u;CID=c;SEID=s')

    def test_changing_login_mode_drops_incompatible_secrets(self):
        a = self.save('115', {'access_token': 'OPEN-SECRET'})
        self.save('115', {'auth_mode': 'cookie', 'cookie': 'UID=u;CID=c;SEID=s'}, id=a['id'])
        self.assertNotIn('access_token', self.state.clouds.accounts[a['id']]['credentials'])

    def test_online_mode_accepts_only_refresh_token(self):
        for provider in ('baidu', 'aliyun'):
            a = self.save(provider, {'auth_mode': 'online', 'refresh_token': 'PRIVATE-REFRESH'})
            self.assertNotIn('PRIVATE-REFRESH', json.dumps(a)); self.assertEqual(a['credentials']['auth_mode'], 'online')

    def test_123_password_hidden_and_change_invalidates_login(self):
        a = self.save('123', {'auth_mode': 'password', 'username': 'user', 'password': 'PRIVATE-PASSWORD'})
        self.assertNotIn('PRIVATE-PASSWORD', json.dumps(a))
        private = self.state.clouds.accounts[a['id']]['credentials']; private['web_token'] = 'old'
        self.save('123', {'auth_mode': 'password', 'password': 'changed'}, id=a['id'])
        self.assertNotIn('web_token', self.state.clouds.accounts[a['id']]['credentials'])

    def test_123_bearer_prefix_removed(self):
        a = self.save('123', {'auth_mode': 'web', 'web_token': 'Bearer PRIVATE'})
        self.assertNotIn('PRIVATE', json.dumps(a))
        self.assertEqual(self.state.clouds.accounts[a['id']]['credentials']['web_token'], 'PRIVATE')

    def test_invalid_or_mismatched_mode_rejected(self):
        for provider, credentials in [('115', {'auth_mode': 'cookie', 'cookie': 'x=y'}),
                                      ('189', {'auth_mode': 'online', 'refresh_token': 'r'})]:
            with self.assertRaises(CloudError): self.save(provider, credentials)


if __name__ == '__main__': unittest.main()
