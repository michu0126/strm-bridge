from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge.core import make_plan, execute, inside, media_extensions, subtitle_match
from bridge.resolver import fields, string_field, rpc, resolve, cloud_path, varint
from bridge.server import State, Handler

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / 'cloud'
        self.dst = self.root / 'library'
        self.src.mkdir(); self.dst.mkdir()
        self.config = {'source_roots': [str(self.src)], 'target_roots': [str(self.dst)],
                       'public_url': 'http://nas:18642', 'mappings': [{'local': str(self.src), 'cloud': '/115open'}]}
        self.options = {'source': str(self.src), 'target': str(self.dst), 'kind': 'movie', 'mode': 'redirect'}
    def file(self, name, content=b'content'):
        p = self.src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p
    def run_plan(self, **kwargs):
        plan = make_plan(dict(self.options, **kwargs), self.config)
        result = execute(plan, self.config, lambda p: 'http://nas/play/' + p.name)
        return plan, result
    def test_unicode_and_query_characters(self):
        folder = '电影 & # (2026)' if os.name == 'nt' else '电影 & #? (2026)'
        self.file(folder + '/片名 #1.MKV')
        _, r = self.run_plan()
        self.assertEqual(r['created'], 1)
        self.assertTrue((self.dst / folder / '片名 #1.strm').exists())
    def test_short_video_preserved_during_cleanup(self):
        video = self.file('ABC-123.mp4', b'v')
        junk = self.file('advertisement.url')
        plan, r = self.run_plan(kind='jav', cleanup=True)
        self.assertTrue(video.exists()); self.assertFalse(junk.exists())
        self.assertEqual(r['trashed'], 1)
        self.assertTrue((self.src / '.strm-trash' / plan['id'] / junk.name).exists())
    def test_cleanup_default_off(self):
        self.file('movie.mp4'); junk = self.file('other.txt')
        _, r = self.run_plan(kind='jav')
        self.assertEqual(r['trashed'], 0); self.assertTrue(junk.exists())
    def test_cleanup_forbidden_for_movies(self):
        with self.assertRaises(ValueError):
            self.run_plan(cleanup=True)
    def test_threshold_strictly_less_than_decimal_100mb(self):
        self.file('movie.mkv'); big = self.file('large.bin')
        with big.open('wb') as f: f.truncate(100_000_000)
        p, r = self.run_plan(kind='jav', cleanup=True)
        self.assertEqual(r['trashed'], 0); self.assertTrue(big.exists())
    def test_cleanup_does_not_touch_unrelated_directories(self):
        self.file('one/movie.mp4'); other = self.file('two/document.txt')
        _, r = self.run_plan(kind='jav', cleanup=True)
        self.assertTrue(other.exists()); self.assertEqual(r['trashed'], 0)
    def test_named_subtitle_preserves_language(self):
        self.file('film.mkv'); self.file('film.zh-CN.forced.ASS')
        _, r = self.run_plan()
        self.assertEqual(r['subtitles'], 1)
        self.assertTrue((self.dst / 'film.zh-CN.forced.ass').exists())
    def test_single_video_unnamed_subtitles_and_vobsub_pair(self):
        self.file('movie.mkv'); self.file('Subs/Chinese.idx'); self.file('Subs/Chinese.sub')
        _, r = self.run_plan()
        self.assertTrue((self.dst / 'movie.idx').exists()); self.assertTrue((self.dst / 'movie.sub').exists())
        self.assertEqual(r['subtitles'], 2)
    def test_episode_boundary_prevents_e01_matching_e010(self):
        self.file('Series.S01E01.mkv'); self.file('Series.S01E010.mkv')
        self.file('Other.S01E010.chs.srt')
        _, r = self.run_plan(kind='tv')
        self.assertTrue((self.dst / 'Series.S01E010.chs.srt').exists())
        self.assertFalse((self.dst / 'Series.S01E01.chs.srt').exists())
    def test_ambiguous_subtitle_not_attached_to_every_episode(self):
        self.file('Series.S01E01.mkv'); self.file('Series.S01E02.mkv'); self.file('Chinese.srt')
        p, r = self.run_plan(kind='tv')
        self.assertEqual(r['subtitles'], 0); self.assertTrue(p['warnings'])
    def test_move_subtitle_checks_copy_before_removal(self):
        self.file('movie.mkv'); sub = self.file('subtitle.srt', b'hello world')
        _, r = self.run_plan(subtitle_mode='move')
        self.assertFalse(sub.exists()); self.assertEqual((self.dst / 'movie.srt').read_bytes(), b'hello world')
    def test_different_target_subtitle_preserves_both_files(self):
        self.file('movie.mkv'); sub = self.file('movie.srt', b'original')
        (self.dst / 'movie.srt').write_bytes(b'different')
        _, r = self.run_plan(subtitle_mode='move')
        self.assertTrue(sub.exists()); self.assertTrue(r['errors'])
        self.assertEqual((self.dst / 'movie.srt').read_bytes(), b'different')
    def test_existing_identical_strm_is_idempotent(self):
        self.file('movie.mkv'); self.run_plan(); _, r = self.run_plan()
        self.assertEqual(r['created'], 0); self.assertEqual(r['skipped'], 1)
    def test_existing_different_strm_never_overwritten(self):
        self.file('movie.mkv'); (self.dst / 'movie.strm').write_text('existing')
        _, r = self.run_plan()
        self.assertTrue(r['errors']); self.assertEqual((self.dst / 'movie.strm').read_text(), 'existing')
    def test_source_change_after_preview_blocks_action(self):
        video = self.file('movie.mkv')
        p = make_plan(self.options, self.config); video.write_bytes(b'changed content')
        r = execute(p, self.config, lambda p: 'unused')
        self.assertTrue(r['errors']); self.assertFalse((self.dst / 'movie.strm').exists())
    def test_same_size_change_with_modified_timestamp_blocks_action(self):
        video=self.file('movie.mkv',b'old')
        plan=make_plan(self.options,self.config)
        old=video.stat().st_mtime
        video.write_bytes(b'new');os.utime(video,(old+2,old+2))
        result=execute(plan,self.config,lambda p:'unused')
        self.assertTrue(result['errors'])
        self.assertFalse((self.dst/'movie.strm').exists())
    def test_generation_error_prevents_cleanup(self):
        self.file('movie.mkv'); junk = self.file('junk.txt')
        (self.dst / 'movie.strm').write_text('existing')
        _, r = self.run_plan(kind='jav', cleanup=True)
        self.assertTrue(junk.exists()); self.assertEqual(r['trashed'], 0)
    def test_reject_nested_source_and_target(self):
        self.config['target_roots'] = [str(self.src)]
        with self.assertRaises(ValueError): make_plan(dict(self.options, target=str(self.src)), self.config)
    def test_outside_root_rejected(self):
        with self.assertRaises(ValueError): inside(self.root / 'private', [self.src])
    @unittest.skipIf(os.name == 'nt', 'Windows requires symlink privilege')
    def test_symlink_escape_not_scanned(self):
        (self.src / 'escape').symlink_to(self.dst, target_is_directory=True)
        (self.dst / 'private.mkv').write_text('private')
        p = make_plan(self.options, self.config)
        self.assertEqual(p['counts']['strm'], 0)
    def test_custom_format_and_case(self):
        self.file('movie.WEIRDFMT')
        p, r = self.run_plan(extra_extensions='.weirdfmt')
        self.assertEqual(r['created'], 1)
    def test_cannot_add_subtitle_as_video_extension(self):
        with self.assertRaises(ValueError): media_extensions('srt')
    def test_duplicate_video_stems_no_collision(self):
        self.file('movie.mkv'); self.file('movie.mp4')
        p, r = self.run_plan()
        self.assertEqual(r['created'], 2)
        self.assertEqual(len({a['target'] for a in p['actions']}), 2)
    def test_stable_mapping_chinese_url_path(self):
        self.assertEqual(cloud_path(self.src / '片名 #.mkv', self.config), '/115open/片名 #.mkv')
    def test_config_changes_require_new_preview(self):
        self.file('movie.mkv'); p = make_plan(self.options, self.config)
        c = dict(self.config, public_url='http://other:8000')
        with self.assertRaises(ValueError): execute(p, c, lambda p: 'unused')
    def test_expired_plan_requires_preview(self):
        p = make_plan(self.options, self.config); p['created'] -= 3700
        with self.assertRaises(ValueError): execute(p, self.config, lambda p: 'unused')
    def test_folder_mode_requires_no_cloud_service_or_mapping(self):
        self.file('independent.mkv')
        self.config.update(provider='none', default_mode='path', mappings=[])
        opts=dict(self.options,mode='path')
        plan=make_plan(opts,self.config)
        with patch('bridge.resolver.rpc',side_effect=AssertionError('Cloud API must not be called')):
            result=execute(plan,self.config,lambda p: (_ for _ in ()).throw(AssertionError('No playback adapter needed')))
        self.assertEqual(result['created'],1)
        self.assertEqual((self.dst/'independent.strm').read_text().strip(),str((self.src/'independent.mkv').resolve()))

class ResolverTests(unittest.TestCase):
    def test_openlist_maps_path_and_preserves_player_agent(self):
        with tempfile.TemporaryDirectory() as t:
            c={'provider':'openlist','openlist_url':'http://files:5244','openlist_token':'test','mappings':[{'local':t,'cloud':'/115'}]}
            with patch('bridge.resolver.json_api',return_value={'raw_url':'https://cdn.example/file'}) as call:
                self.assertEqual(resolve(Path(t)/'片名 #.mkv',c,'PlayerUA'),'https://cdn.example/file')
                self.assertEqual(call.call_args.args[2]['path'],'/115/片名 #.mkv')
                self.assertEqual(call.call_args.args[4],'PlayerUA')
    def test_openlist_does_not_claim_proxy_url_is_direct(self):
        with tempfile.TemporaryDirectory() as t:
            c={'provider':'openlist','openlist_url':'http://files:5244','mappings':[{'local':t,'cloud':'/115'}]}
            with patch('bridge.resolver.json_api',return_value={'raw_url':'http://files:5244/p/115/file.mkv'}):
                with self.assertRaisesRegex(ValueError,'代理'):resolve(Path(t)/'file.mkv',c)
    def test_http_template_quotes_special_filename_characters(self):
        with tempfile.TemporaryDirectory() as t:
            c={'provider':'template','url_template':'https://files.example/d{path}','mappings':[{'local':t,'cloud':'/115'}]}
            self.assertEqual(resolve(Path(t)/'a #&.mkv',c),'https://files.example/d/115/a%20%23%26.mkv')
    def test_varint_unicode_roundtrip(self):
        self.assertEqual(fields(string_field(1, '电影'*200))[1][0].decode(), '电影'*200)
    def test_direct_url_field_and_request_flag(self):
        with tempfile.TemporaryDirectory() as t:
            c = {'mappings':[{'local':t,'cloud':'/115'}], 'cd2_url':'http://cd2:19798','cd2_token':'unit-test'}
            with patch('bridge.resolver.rpc',return_value={3:[b'https://download.example/video.mp4']}) as call:
                self.assertEqual(resolve(Path(t)/'a.mkv',c),'https://download.example/video.mp4')
                self.assertEqual(fields(call.call_args.args[2])[4],[1])
    def test_reject_additional_headers_that_302_cannot_forward(self):
        with tempfile.TemporaryDirectory() as t:
            c={'mappings':[{'local':t,'cloud':'/115'}],'cd2_url':'http://cd2:19798','cd2_token':'test'}
            with patch('bridge.resolver.rpc',return_value={3:[b'https://cdn.example/v'],5:[string_field(1,'Cookie')+string_field(2,'secret')]}):
                with self.assertRaisesRegex(ValueError,'附加请求头'): resolve(Path(t)/'v.mkv',c)
    def test_invalid_grpc_status_not_treated_as_success(self):
        class MockCD2(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200); self.send_header('grpc-status','16'); self.end_headers()
            def log_message(self,*a): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),MockCD2)
        th=threading.Thread(target=server.serve_forever,daemon=True); th.start()
        try:
            with self.assertRaises(ValueError): rpc('http://127.0.0.1:'+str(server.server_port),'GetToken')
        finally: server.shutdown();server.server_close()

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=State(self.temp.name)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.server.state=self.state
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.cleanup)
    def cleanup(self):
        self.server.shutdown();self.server.server_close();self.state.pool.shutdown(wait=True);self.state.db.close()
    def request(self,path,data=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        h={'Content-Type':'application/json',**(headers or {})}
        c.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,h)
        r=c.getresponse();result=(r.status,dict(r.headers),r.read());c.close();return result
    def login(self):
        status,headers,_=self.request('/api/login',{'password':self.state.password})
        self.assertEqual(status,200)
        return {'Cookie':headers['Set-Cookie'].split(';')[0]}
    def test_api_requires_auth(self): self.assertEqual(self.request('/api/config')[0],401)
    def test_config_does_not_expose_secret(self):
        self.state.config['cd2_token']='sensitive'
        self.state.config['openlist_password']='openlist-sensitive'
        status,_,body=self.request('/api/config',headers=self.login())
        self.assertEqual(status,200);self.assertNotIn(b'sensitive',body)
    def test_save_independent_settings_without_cd2_or_mapping(self):
        status,_,body=self.request('/api/config',{'provider':'none','default_mode':'path','cd2_url':'','mappings':[]},self.login())
        self.assertEqual(status,200,body)
        self.assertEqual(self.state.config['mappings'],[])
    def test_csrf_rejected(self):
        h=dict(self.login(),Origin='http://evil.invalid')
        self.assertEqual(self.request('/api/config',{},h)[0],403)
    def test_stable_capability_url_and_tamper_rejection(self):
        path=Path(self.temp.name)/'video.mkv'
        self.assertEqual(self.state.playback(path), self.state.playback(path))
        self.assertEqual(self.request('/play/id/invalid/video.mkv')[0],403)
    def test_http_302_location_is_dynamic_and_no_body(self):
        self.state.config['source_roots']=[self.temp.name]
        video=Path(self.temp.name)/'movie.mkv';video.write_bytes(b'v')
        from urllib.parse import urlsplit
        url=urlsplit(self.state.playback(video)).path
        with patch('bridge.server.resolve',return_value='https://cdn.example/fresh?expiry=123'):
            status,headers,body=self.request(url)
        self.assertEqual(status,302);self.assertEqual(headers['Location'],'https://cdn.example/fresh?expiry=123');self.assertEqual(body,b'')
    def test_restore_preserves_existing_original(self):
        src=Path(self.temp.name)/'cloud';dst=Path(self.temp.name)/'library';src.mkdir();dst.mkdir()
        self.state.config.update(source_roots=[str(src)],target_roots=[str(dst)],mappings=[{'local':str(src),'cloud':'/115'}])
        (src/'movie.mkv').write_bytes(b'video');(src/'other.txt').write_bytes(b'old')
        p=make_plan({'source':str(src),'target':str(dst),'kind':'jav','cleanup':True},self.state.config)
        self.state.put_job({'id':'j1','status':'running','plan':p})
        execute(p,self.state.config,self.state.playback,journal=lambda k,v:self.state.journal('j1',k,v))
        (src/'other.txt').write_bytes(b'new')
        r=self.state.restore('j1');self.assertEqual(r['restored'],0);self.assertTrue(r['errors'])
        (src/'other.txt').unlink()
        self.assertEqual(self.state.restore('j1')['restored'],1)
        self.assertEqual((src/'other.txt').read_bytes(),b'old')

if __name__=='__main__': unittest.main()
