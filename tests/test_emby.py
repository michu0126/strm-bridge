import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from bridge.server import Handler, State


class EmbyGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(self.temp.name)
        self.user = 'b' * 32
        self.token = 'example-player-token'
        self.sources = [{'Id': 'source-1', 'Path': self.state.playback({
            'account': 'a' * 32, 'object': {'id': 'video', 'name': 'movie.mp4'}})}]
        self.upstream_requests = []
        owner = self

        class Emby(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_HEAD(self):
                self.do_GET()
            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                token = self.headers.get('X-Emby-Token') or query.get('api_key', [''])[0]
                owner.upstream_requests.append((parsed.path, self.headers.get('Range')))
                if token != owner.token:
                    return self.reply(401, b'Unauthorized')
                if parsed.path.startswith('/Users/'):
                    if not parsed.path.startswith('/Users/' + owner.user + '/'):
                        return self.reply(403, b'Forbidden')
                    return self.reply(200, json.dumps({'MediaSources': owner.sources}).encode())
                self.reply(206, b'ok', {'Content-Range': 'bytes 0-1/100', 'Content-Type': 'video/mp4'})
            def reply(self, status, body, headers=None):
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

        self.emby = ThreadingHTTPServer(('127.0.0.1', 0), Emby)
        self.state.config['emby_url'] = 'http://127.0.0.1:' + str(self.emby.server_port)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.state = self.state
        for server in (self.emby, self.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.provider = Mock()
        self.provider.link.return_value = ('https://cdn.example/movie.mp4?download=short-lived', {})
        self.client_patch = patch.object(self.state.clouds, 'client')
        self.client_patch.start().return_value.__enter__.return_value = self.provider

    def tearDown(self):
        self.client_patch.stop()
        for server in (self.server, self.emby):
            server.shutdown()
            server.server_close()
        self.state.watcher.close()
        self.state.pool.shutdown(wait=True)
        self.state.db.close()
        self.temp.cleanup()

    def request(self, suffix='original.mp4?MediaSourceId=source-1', headers=None, method='GET', authorized=True):
        base = {'X-Emby-Token': self.token, 'X-Emby-Authorization': 'MediaBrowser UserId="' + self.user + '"'} if authorized else {}
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, '/emby-play/123/' + suffix, headers=dict(base, **(headers or {})))
        response = connection.getresponse()
        result = response.status, dict(response.headers), response.read()
        connection.close()
        return result

    def test_authenticated_original_redirects_directly_to_cdn_without_video_relay(self):
        status, headers, body = self.request(headers={'User-Agent': 'ExamplePlayer/1.0', 'Range': 'bytes=10-'})
        self.assertEqual(status, 302)
        self.assertEqual(headers['X-STRM-Bridge'], 'emby-302')
        self.assertEqual(urlsplit(headers['Location']).netloc, 'cdn.example')
        self.assertNotIn(self.token, headers['Location'])
        self.assertEqual(body, b'')
        self.assertEqual(self.upstream_requests, [('/Users/' + self.user + '/Items/123', None)])
        self.assertEqual(self.provider.link.call_args.args[1], 'ExamplePlayer/1.0')
        self.assertEqual(self.state.emby_stats['redirects'], 1)

    def test_missing_agent_stays_empty_and_head_returns_no_body(self):
        status, headers, body = self.request(method='HEAD')
        self.assertEqual(status, 302)
        self.assertEqual(body, b'')
        self.assertEqual(headers['Content-Length'], '0')
        self.assertEqual(self.provider.link.call_args.args[1], '')

    def test_query_credentials_are_accepted_without_storing_a_server_token(self):
        status, _, _ = self.request('stream.mp4?UserId=' + self.user + '&api_key=' + self.token, authorized=False)
        self.assertEqual(status, 302)
        self.assertNotIn(self.token, json.dumps(self.state.config))

    def test_invalid_token_never_resolves_cloud_link(self):
        status, _, _ = self.request(headers={'X-Emby-Token': 'invalid'})
        self.assertEqual(status, 401)
        self.provider.link.assert_not_called()

    def test_other_users_library_access_is_checked_by_emby(self):
        status, _, _ = self.request(headers={'X-Emby-Authorization': 'MediaBrowser UserId="' + 'c' * 32 + '"'})
        self.assertEqual(status, 403)
        self.provider.link.assert_not_called()

    def test_missing_credentials_preserve_embys_auth_rejection(self):
        status, _, _ = self.request(authorized=False)
        self.assertEqual(status, 401)
        self.provider.link.assert_not_called()

    def test_other_media_and_unknown_versions_keep_normal_range_streaming(self):
        for sources, suffix in [([{'Id': 'source-1', 'Path': '/media/local.mp4'}], 'original.mp4'),
                                (self.sources, 'original.mp4?MediaSourceId=unknown')]:
            self.sources = sources
            status, headers, body = self.request(suffix, headers={'Range': 'bytes=0-1'})
            self.assertEqual(status, 206)
            self.assertEqual(headers['Content-Range'], 'bytes 0-1/100')
            self.assertEqual(headers['X-STRM-Bridge'], 'emby-relay')
            self.assertEqual(body, b'ok')
        self.provider.link.assert_not_called()

    def test_tampered_capability_does_not_resolve_media(self):
        self.sources[0]['Path'] = self.sources[0]['Path'].replace('/play/', '/play/x')
        self.assertEqual(self.request()[0], 206)
        self.provider.link.assert_not_called()

    def test_transcoding_and_subtitle_requests_are_not_replaced_with_raw_video(self):
        for suffix in ['stream.mp4?Static=false', 'stream.mp4?VideoCodec=h264', 'Subtitles/0/Stream.srt']:
            self.assertEqual(self.request(suffix)[0], 206)
        self.provider.link.assert_not_called()

    def test_disabled_gateway_and_traversal_are_rejected(self):
        self.assertEqual(self.request('../original.mp4')[0], 400)
        self.state.config['emby_url'] = ''
        self.assertEqual(self.request()[0], 404)
        self.assertEqual(self.upstream_requests, [])


if __name__ == '__main__':
    unittest.main()
