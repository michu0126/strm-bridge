import base64
import copy
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer

from bridge.server import Handler, State
from bridge.cloud_providers import CloudError, item


class WebDAVTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'AUTH_DISABLED': '1'})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(self.temp.name)
        self.state.config['webdav_enabled'] = True
        a = self.state.clouds.save({'provider': '115', 'name': '我的 115', 'credentials': {
            'auth_mode': 'cookie', 'cookie': 'UID=example; CID=example; SEID=example'}})
        self.aid = a['id']
        self.base = '/dav/' + self.aid + '/'
        self.rows = {'0': [item('10', '电影 & 中文', True), item('20', '预告[1].mp4', size=1234)],
                     '10': [item('11', '测试 #1.mkv', size=987654321, modified=1700000000),
                            item('12', '测试 #1.zh.ass', size=1024)]}
        self.provider = Mock()
        self.provider.list.side_effect = lambda parent: copy.deepcopy(self.rows[parent])
        self.provider.link.return_value = ('https://cdn.example/video.mkv?temporary=download', {})
        self.client_patch = patch.object(self.state.clouds, 'client')
        self.client_patch.start().return_value.__enter__.return_value = self.provider
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.state = self.state
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.client_patch.stop()
        self.state.watcher.close()
        self.state.pool.shutdown(wait=True)
        self.state.db.close()
        self.temp.cleanup()
        self.env.stop()

    def request(self, method='PROPFIND', path='/dav/', headers=None, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        c.request(method, quote(path, safe='/%'), body=body, headers=headers or {})
        r = c.getresponse()
        result = r.status, dict(r.headers), r.read()
        c.close()
        return result

    def names(self, body):
        return [n.text for n in ET.fromstring(body).findall('.//{DAV:}displayname')]

    def movie(self):
        return self.base + '电影 & 中文/测试 #1.mkv'

    def test_disabled_endpoint_and_existing_strm_entry_are_independent(self):
        self.state.config['webdav_enabled'] = False
        self.assertEqual(self.request('OPTIONS')[0], 404)
        self.provider.list.assert_not_called()
        url = self.state.playback({'account': self.aid, 'object': item('11', 'movie.mkv', size=100)})
        from urllib.parse import urlsplit
        code, headers, _ = self.request('GET', urlsplit(url).path)
        self.assertEqual(code, 302)
        self.assertEqual(headers['Location'], self.provider.link.return_value[0])

    def test_options_advertises_only_supported_methods(self):
        code, headers, _ = self.request('OPTIONS')
        self.assertEqual(code, 200)
        self.assertEqual(headers['DAV'], '1')
        self.assertNotIn('PUT', headers['Allow'])
        self.provider.list.assert_not_called()

    def test_root_lists_cloud_accounts_without_loading_their_files(self):
        code, _, body = self.request(headers={'Depth': '1'})
        self.assertEqual(code, 207)
        self.assertEqual(self.names(body), ['我的网盘', '我的 115'])
        hrefs = [n.text for n in ET.fromstring(body).findall('.//{DAV:}href')]
        self.assertEqual(hrefs, ['/dav/', self.base])
        self.assertNotIn('cookie', body.decode().lower())
        self.provider.list.assert_not_called()

    def test_directory_lists_unicode_escaped_paths_and_content_metadata(self):
        code, _, body = self.request(path=self.base)
        self.assertEqual(code, 207)
        self.assertEqual(self.names(body), ['我的 115', '电影 & 中文', '预告[1].mp4'])
        self.assertIn(quote(self.base + '电影 & 中文/', safe='/').encode(), body)
        self.assertIn(b'<D:getcontentlength>1234</D:getcontentlength>', body)
        self.assertIn(b'<D:collection', body)

    def test_depth_zero_and_file_propfind_do_not_list_descendants(self):
        code, _, body = self.request(path=self.base, headers={'Depth': '0'})
        self.assertEqual(code, 207)
        self.assertEqual(self.names(body), ['我的 115'])
        self.provider.list.assert_not_called()
        code, _, body = self.request(path=self.movie())
        self.assertEqual(code, 207)
        self.assertEqual(self.names(body), ['测试 #1.mkv'])
        self.assertIn(b'987654321', body)

    def test_deep_paths_work_without_prior_browsing_or_strm_generation(self):
        code, headers, body = self.request('GET', self.movie(), {
            'User-Agent': 'PotPlayer/260819', 'Range': 'bytes=1024-'})
        self.assertEqual(code, 302)
        self.assertEqual(body, b'')
        self.assertEqual(headers['Location'], self.provider.link.return_value[0])
        obj, agent = self.provider.link.call_args.args
        self.assertEqual((obj['id'], agent), ('11', 'PotPlayer/260819'))
        self.assertEqual(self.state.db.execute('select count(*) from media').fetchone()[0], 0)

    def test_head_returns_size_without_resolving_or_downloading_video(self):
        code, headers, body = self.request('HEAD', self.movie())
        self.assertEqual(code, 200)
        self.assertEqual(headers['Content-Length'], '987654321')
        self.assertEqual(headers['Accept-Ranges'], 'bytes')
        self.assertEqual(body, b'')
        self.provider.link.assert_not_called()

    def test_no_user_agent_and_subtitle_download_are_supported(self):
        code, _, _ = self.request('GET', self.base + '电影 & 中文/测试 #1.zh.ass')
        self.assertEqual(code, 302)
        self.assertEqual(self.provider.link.call_args.args[1], '')

    def test_cache_reuses_lists_and_invalidates_after_account_update(self):
        self.assertEqual(self.request(path=self.base)[0], 207)
        self.assertEqual(self.request(path=self.base)[0], 207)
        self.assertEqual(self.provider.list.call_count, 1)
        with patch('bridge.webdav.time.monotonic', return_value=10**12):
            self.rows['0'].append(item('30', 'new.mp4', size=100))
            self.assertIn('new.mp4', self.names(self.request(path=self.base)[2]))
        self.state.clouds.account(self.aid)['revision'] += 1
        self.request(path=self.base)
        self.assertEqual(self.provider.list.call_count, 3)

    def test_requested_and_unknown_properties_have_separate_statuses(self):
        body = b'<D:propfind xmlns:D="DAV:" xmlns:X="custom:"><D:prop><D:displayname/><X:unknown/></D:prop></D:propfind>'
        code, _, result = self.request(path=self.base, headers={'Depth': '0'}, body=body)
        self.assertEqual(code, 207)
        self.assertEqual(self.names(result), ['我的 115'])
        self.assertIn(b'HTTP/1.1 404 Not Found', result)
        self.assertNotIn(b'getcontentlength', result)

    def test_bad_xml_and_unbounded_depth_do_not_scan_cloud(self):
        for body in [b'<broken', b'<!DOCTYPE x [<!ENTITY a "x">]><x/>']:
            self.assertEqual(self.request(body=body)[0], 400)
        self.assertEqual(self.request(headers={'Depth': 'infinity'})[0], 403)
        self.assertEqual(self.request(headers={'Depth': '2'})[0], 400)
        self.assertEqual(self.request(headers={'Content-Length': '65537'})[0], 413)
        self.provider.list.assert_not_called()

    def test_traversal_invalid_accounts_and_missing_names_are_rejected(self):
        for path in ['/dav/../data', '/dav/%2e%2e/', self.base + '%2fsecret', '/dav/unknown/']:
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.request(path=self.base + 'missing.mp4')[0], 404)

    def test_all_writes_are_refused_without_any_provider_changes(self):
        for method in ['PUT', 'POST', 'DELETE', 'MOVE', 'COPY', 'MKCOL', 'LOCK', 'UNLOCK', 'PROPPATCH']:
            self.assertEqual(self.request(method, self.base)[0], 405)
        self.provider.list.assert_not_called()
        self.provider.move.assert_not_called()
        self.provider.mkdir.assert_not_called()

    def test_cloud_failure_is_reported_not_an_empty_folder(self):
        self.provider.list.side_effect = CloudError('115 授权失效')
        code, _, body = self.request(path=self.base)
        self.assertEqual(code, 502)
        self.assertIn('115 授权失效', json.loads(body)['error'])
        self.assertEqual(self.state.dav_cache.rows, {})

    def test_basic_auth_uses_existing_management_password(self):
        with patch.dict(os.environ, {'AUTH_DISABLED': '0'}):
            code, headers, _ = self.request()
            self.assertEqual(code, 401)
            self.assertIn('Basic', headers['WWW-Authenticate'])
            for value in ['Basic !!!', 'Bearer not-a-password', 'Basic ' + base64.b64encode(b'admin:wrong').decode()]:
                self.assertEqual(self.request(headers={'Authorization': value})[0], 401)
            value = base64.b64encode(('admin:' + self.state.password).encode()).decode()
            self.assertEqual(self.request(headers={'Authorization': 'Basic ' + value})[0], 207)


if __name__ == '__main__':
    unittest.main()
