import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from bridge.cloud import Clouds, is_cloud, parse_uri, uri
from bridge.cloud_providers import Cloud115, Cloud123, Aliyun, Baidu, Tianyi, CloudError, PROVIDERS, item
from bridge.server import State


class FakeCloud:
    """In-memory remote filesystem: no locally mounted source directory exists."""
    def __init__(self):
        self.tree = {'0': [item('dir', '电影', True)], 'dir': []}
        self.links = []
        self.moves = []
    def add(self, name, size=120_000_000, parent='dir', identifier=None):
        obj = item(identifier or str(sum(map(len, self.tree.values())) + 100), name, False, size, time.time()-120)
        self.tree[parent].append(obj)
        return obj
    def list(self, parent):
        return iter(copy.deepcopy(self.tree[parent]))
    def mkdir(self, parent, name):
        identifier = 'folder' + str(len(self.tree))
        self.tree[parent].append(item(identifier, name, True))
        self.tree[identifier] = []
        return identifier
    def move(self, obj, destination):
        self.tree[obj['parent']] = [r for r in self.tree[obj['parent']] if r['id'] != obj['id']]
        moved = dict(obj, parent=destination)
        self.tree[destination].append(moved)
        self.moves.append((obj['id'], destination))
        return moved
    def link(self, obj, agent):
        self.links.append(obj['id'])
        return 'https://media.example/' + obj['id'], {}


class NativeCloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / 'library'; self.target.mkdir()
        self.state = State(self.root / 'data')
        self.state.config.update(source_roots=[], target_roots=[str(self.target)], provider='none', public_url='http://nas:18642')
        self.account = self.state.clouds.save({'name': '我的115', 'provider': '115', 'credentials': {'access_token': 'SECRET-TOKEN'}})
        self.fake = FakeCloud()
        self.state.clouds.clients[self.account['id']] = self.fake
        self.state.clouds.browse(self.account['source'])
        self.options = dict(source=uri(self.account['id'], 'dir'), target=str(self.target), kind='tv', mode='redirect', subtitle_mode='copy')
    def tearDown(self):
        self.state.watcher.close(); self.state.pool.shutdown(wait=True); self.state.db.close(); self.tmp.cleanup()
    def plan(self, **kwargs):
        return self.state.make_plan(dict(self.options, **kwargs), self.state.config)
    def run_plan(self, plan):
        return self.state.execute(plan, self.state.config, self.state.playback, journal=lambda kind, value: self.state.journal('test', kind, value))
    def test_accounts_survive_restart_and_redact_credentials(self):
        self.assertNotIn('SECRET-TOKEN', json.dumps(self.state.clouds.list_accounts()))
        self.assertEqual(Clouds(self.state).accounts[self.account['id']]['credentials']['access_token'], 'SECRET-TOKEN')
        self.assertEqual(self.account['configured'], ['access_token'])
    def test_browse_native_without_any_source_mount(self):
        d = self.state.clouds.browse(self.account['source'])
        self.assertEqual(d['directories'][0]['name'], '电影')
        self.assertEqual(parse_uri(d['directories'][0]['path']), (self.account['id'], 'dir'))
        self.assertEqual(self.state.clouds.browse(d['directories'][0]['path'])['parent'], self.account['source'])
    def test_scan_and_generation_never_fetch_media_links(self):
        self.fake.add('Show.S01E01.mkv')
        plan = self.plan(); result = self.run_plan(plan)
        self.assertEqual(result['created'], 1); self.assertEqual(self.fake.links, [])
        text = (self.target / 'Show.S01E01.strm').read_text()
        self.assertIn('http://nas:18642/play/', text)
        self.assertEqual(self.run_plan(plan)['skipped'], 1)
        row = self.state.db.execute('SELECT path FROM media').fetchone()[0]
        self.assertEqual(self.state.clouds.resolve(json.loads(row[6:]), 'player'), 'https://media.example/101')
    def test_all_small_types_including_videos_are_quarantined_and_restorable(self):
        self.fake.add('main.mkv', 100_000_000)
        small = ['advert.mp4', 'advert.exe', 'README', 'ad.rar', 'ad.strm', 'ad.srt']
        for name in small: self.fake.add(name, 12)
        plan = self.plan(kind='jav', cleanup=True)
        self.assertEqual(plan['counts']['strm'], 1); self.assertEqual(plan['counts']['trash'], 6)
        result = self.run_plan(plan)
        self.assertEqual(result['errors'], []); self.assertEqual(result['trashed'], 6)
        self.assertEqual([r['name'] for r in self.fake.tree['dir'] if not r['folder']], ['main.mkv'])
        self.state.put_job({'id': 'test', 'status': 'completed', 'plan': plan})
        self.assertEqual(self.state.restore('test')['restored'], 6)
        self.assertEqual(self.state.restore('test')['restored'], 0)
    def test_cleanup_defaults_off(self):
        self.fake.add('ad.mp4', 5)
        plan = self.plan(kind='jav')
        self.assertEqual(plan['counts']['strm'], 1); self.assertEqual(plan['counts']['trash'], 0)
    def test_changed_cloud_file_is_not_cleaned(self):
        self.fake.add('main.mkv'); ad = self.fake.add('ad.mp4', 10)
        plan = self.plan(kind='jav', cleanup=True); ad['size'] = 200_000_000
        result = self.run_plan(plan)
        self.assertEqual(result['trashed'], 0); self.assertTrue(result['errors'])
        self.assertFalse(self.fake.moves)
    def test_generation_failure_stops_remote_cleanup(self):
        self.fake.add('main.mkv'); self.fake.add('ad.exe', 10)
        plan = self.plan(kind='jav', cleanup=True)
        (self.target / 'main.strm').write_text('user content')
        result = self.run_plan(plan)
        self.assertEqual(result['trashed'], 0); self.assertFalse(self.fake.moves)
    def test_subtitles_match_episodes_and_language(self):
        self.fake.add('Series.S01E01.mkv'); self.fake.add('Series.S01E02.mkv')
        self.fake.add('Release.S01E02.chs.srt', 3)
        plan = self.plan()
        action = next(a for a in plan['actions'] if a['type'] == 'copy')
        self.assertEqual(Path(action['target']).name, 'Series.S01E02.chs.srt')
        with patch.object(self.state.clouds, 'download', side_effect=lambda c,o,p,cancel: p.write_bytes(b'sub')):
            result = self.run_plan(plan)
        self.assertEqual(result['subtitles'], 1); self.assertFalse(self.fake.moves)
    def test_existing_subtitle_never_causes_source_removal(self):
        self.fake.add('movie.mkv'); self.fake.add('movie.srt', 3)
        (self.target / 'movie.srt').write_bytes(b'bad')
        result = self.run_plan(self.plan(subtitle_mode='move'))
        self.assertTrue(result['errors']); self.assertFalse(self.fake.moves)
    def test_scan_error_never_creates_partial_plan(self):
        with patch.object(self.fake, 'list', side_effect=CloudError('分页失败')):
            with self.assertRaises(CloudError): self.plan()
        self.assertEqual(list(self.target.iterdir()), [])
    def test_deleted_video_before_execution_is_not_generated(self):
        self.fake.add('movie.mkv'); plan = self.plan()
        self.fake.tree['dir'] = []
        result = self.run_plan(plan)
        self.assertTrue(result['errors']); self.assertEqual(result['created'], 0)
    def test_subtitle_failed_download_never_moves_source(self):
        self.fake.add('movie.mkv'); self.fake.add('movie.srt', 3)
        with patch.object(self.state.clouds, 'download', side_effect=CloudError('下载失败')):
            result = self.run_plan(self.plan(subtitle_mode='move'))
        self.assertTrue(result['errors']); self.assertFalse(self.fake.moves)
    def test_cloud_subtitle_move_uses_recoverable_folder(self):
        self.fake.add('movie.mkv'); self.fake.add('movie.srt', 3)
        plan = self.plan(subtitle_mode='move')
        with patch.object(self.state.clouds, 'download', side_effect=lambda c,o,p,cancel: p.write_bytes(b'sub')):
            result = self.run_plan(plan)
        self.assertEqual(result['subtitles'], 1); self.assertEqual(len(self.fake.moves), 1)
        self.state.put_job({'id':'test','status':'completed','plan':plan})
        self.assertEqual(self.state.restore('test')['restored'], 1)
    def test_cancel_before_scan(self):
        with self.assertRaises(CloudError):
            self.state.make_plan(self.options, self.state.config, lambda: True)
    def test_cloud_paths_cannot_escape_local_target(self):
        self.fake.add('../../outside.mkv')
        with self.assertRaises(CloudError): self.plan()
    def test_account_change_invalidates_preview(self):
        self.fake.add('movie.mkv'); plan = self.plan()
        self.state.clouds.save(dict(self.account, name='renamed', credentials={}))
        with self.assertRaises(CloudError): self.run_plan(plan)
    def test_cloud_monitor_adds_without_mount_and_never_cleans(self):
        self.fake.add('movie.mkv'); self.fake.add('ad.exe', 12)
        w = self.state.watcher.save({'options': dict(self.options, kind='jav', cleanup=True, subtitle_mode='move', mode='path')})
        self.assertFalse(w['options']['cleanup']); self.assertEqual(w['options']['mode'], 'redirect')
        self.state.watcher.tick(); self.state.pool.submit(lambda: None).result(timeout=10)
        self.assertTrue((self.target/'movie.strm').exists()); self.assertFalse(self.fake.moves)
        self.assertEqual(self.state.watcher.list()[0]['last_status'], 'completed')
    def test_removing_account_pauses_its_watch(self):
        self.state.watcher.save({'options': self.options})
        self.state.clouds.delete(self.account['id'])
        self.assertFalse(self.state.watcher.list()[0]['enabled'])
    def test_refresh_token_replacement_drops_stale_access(self):
        self.state.clouds.save({'id': self.account['id'], 'credentials': {'refresh_token': 'new'}})
        self.assertNotIn('access_token', self.state.clouds.accounts[self.account['id']]['credentials'])
    def test_duplicate_names_keep_both_with_stable_distinct_targets(self):
        self.fake.add('same.mkv'); self.fake.add('same.mkv')
        plan = self.plan()
        targets = [a['target'] for a in plan['actions']]
        self.assertEqual(len(set(targets)), 2)
        self.fake.tree['dir'].reverse()
        self.assertEqual(set(targets), {a['target'] for a in self.plan()['actions']})
        result = self.run_plan(plan)
        self.assertEqual(result['created'], 2)
        self.assertEqual(result['errors'], [])

    def test_duplicate_directory_names_do_not_merge_their_contents(self):
        self.fake.tree['dir'] = [item('a', 'same', True), item('b', 'same', True)]
        self.fake.tree['a'] = []; self.fake.tree['b'] = []
        self.fake.add('movie.mkv', parent='a'); self.fake.add('movie.mkv', parent='b')
        plan = self.plan()
        self.assertEqual(len({Path(a['target']).parent for a in plan['actions']}), 2)

    def test_interrupted_scan_resumes_but_completed_scan_is_fresh(self):
        self.fake.tree['dir'] = [item('a', 'A', True), item('b', 'B', True)]
        self.fake.tree['a'] = []; self.fake.tree['b'] = []
        self.fake.add('movie.mkv', parent='b')
        original = self.fake.list
        def broken(parent):
            if parent == 'a': raise CloudError('network interrupted')
            return original(parent)
        with patch.object(self.fake, 'list', side_effect=broken):
            with self.assertRaises(CloudError): self.plan()
        with patch.object(self.fake, 'list', wraps=original) as calls:
            plan = self.plan()
            self.assertEqual([c.args[0] for c in calls.call_args_list], ['a'])
        self.assertEqual(plan['counts']['strm'], 1)
        self.fake.add('new.mkv', parent='b')
        self.assertEqual(self.plan()['counts']['strm'], 2)

    def test_scan_releases_account_lock_between_directories(self):
        import threading
        acquired = []
        def progress(info):
            def other_request():
                with self.state.clouds.client(self.account['id']): acquired.append(True)
            t = threading.Thread(target=other_request)
            t.start(); t.join(timeout=1)
            self.assertFalse(t.is_alive(), 'scan held the playback account lock during progress')
        self.state.make_plan(self.options, self.state.config, progress=progress)
        self.assertTrue(acquired)


class ProviderProtocolTests(unittest.TestCase):
    def test_five_native_providers(self):
        self.assertEqual(set(PROVIDERS), {'115','123','baidu','aliyun','189'})
    def test_115_refresh_then_paginated_list_and_user_agent_link(self):
        c = Cloud115({'refresh_token':'refresh'}, Mock())
        responses=[{'code':0,'data':{'access_token':'access','refresh_token':'rotated','expires_in':3600}},
                   {'state':True,'count':2,'data':[{'fid':'1','fn':'film.mkv','fc':'1','fs':50,'pc':'pick'}]},
                   {'state':True,'count':2,'data':[{'fid':'2','fn':'Folder','fc':'0'}]},
                   {'state':True,'data':{'1':{'url':{'url':'https://video.example/film'}}}}]
        with patch.object(c,'raw',side_effect=responses) as call:
            rows=list(c.list('0')); location,headers=c.link(rows[0],'Emby')
        self.assertEqual(len(rows),2); self.assertTrue(rows[1]['folder'])
        self.assertEqual(call.call_args_list[2].kwargs['query']['offset'],1)
        self.assertEqual(c.c['refresh_token'],'rotated'); c.save.assert_called_once()
        self.assertEqual(headers['User-Agent'],'Emby'); self.assertEqual(location,'https://video.example/film')
    def test_115_expired_token_refreshes_only_once(self):
        c=Cloud115({'access_token':'old','refresh_token':'r'})
        with patch.object(c,'raw',side_effect=[{'state':False,'code':40140101},{'code':0,'data':{'access_token':'new'}},{'state':False,'code':40140101}]) as call:
            with self.assertRaises(CloudError): list(c.list('0'))
        self.assertEqual(call.call_count,3)
    def test_115_incomplete_pagination_errors(self):
        c=Cloud115({'access_token':'x'})
        with patch.object(c,'raw',return_value={'state':True,'count':5,'data':[]}):
            with self.assertRaises(CloudError): list(c.list('0'))
    def test_123_client_credentials_and_cursor(self):
        c=Cloud123({'client_id':'client','client_secret':'secret'},Mock())
        with patch.object(c,'raw',side_effect=[{'code':0,'data':{'accessToken':'token','expiredAt':'2099-01-01T00:00:00Z'}}, {'code':0,'data':{'fileList':[{'fileId':1,'filename':'A.mkv','type':0,'size':12}],'lastFileId':1}}, {'code':0,'data':{'fileList':[],'lastFileId':-1}}]) as call:
            self.assertEqual(len(list(c.list('0'))),1)
        self.assertEqual(call.call_args_list[0].kwargs['body']['clientID'],'client')
        self.assertEqual(call.call_args_list[2].kwargs['query']['lastFileId'],1)
    def test_123_repeated_cursor_fails(self):
        c=Cloud123({'access_token':'x'})
        with patch.object(c,'raw',return_value={'code':0,'data':{'fileList':[],'lastFileId':0}}):
            with self.assertRaises(CloudError): list(c.list('0'))
    def test_ali_discovers_drive_and_paginates(self):
        c=Aliyun({'access_token':'x'},Mock())
        with patch.object(c,'raw',side_effect=[{'resource_drive_id':'drive'}, {'items':[{'file_id':'file','name':'A.mkv','type':'file','size':20}],'next_marker':'next'}, {'items':[],'next_marker':''}]) as call:
            self.assertEqual(len(list(c.list('root'))),1)
        self.assertEqual(call.call_args_list[2].kwargs['body']['drive_id'],'drive')
        self.assertEqual(call.call_args_list[2].kwargs['body']['marker'],'next')
    def test_baidu_uses_oauth_not_mount_and_resolves_ticket(self):
        c=Baidu({'access_token':'x'})
        with patch.object(c,'raw',side_effect=[{'errno':0,'list':[{'path':'/a.mkv','fs_id':123,'server_filename':'a.mkv','isdir':0,'size':12}]},{'errno':0,'list':[{'dlink':'https://d.pcs.baidu.com/ticket?x=1'}]}]) as call, patch('bridge.cloud_providers.final_redirect',return_value='https://cdn.example/a') as redirect:
            rows=list(c.list('/')); link,headers=c.link(rows[0],'player')
        self.assertEqual(call.call_args_list[0].kwargs['query']['access_token'],'x')
        self.assertIn('access_token=x',redirect.call_args.args[0]); self.assertEqual(headers['User-Agent'],'pan.baidu.com')
    def test_tianyi_cookie_lists_files_and_folders(self):
        c=Tianyi({'cookie':'session=secret'})
        with patch.object(c,'raw',side_effect=[{'res_code':0,'fileListAO':{'folderList':[{'id':1,'name':'Folder'}],'fileList':[{'id':2,'name':'film.mkv','size':100}]}},{'res_code':0,'fileListAO':{'folderList':[],'fileList':[]}}]) as call:
            rows=list(c.list('-11'))
        self.assertTrue(rows[0]['folder']); self.assertEqual(rows[1]['size'],100)
        self.assertEqual(call.call_args_list[0].kwargs['headers']['Cookie'],'session=secret')
    def test_provider_errors_are_not_empty_success(self):
        for cls,data in [(Cloud115,{'state':False,'code':9}),(Cloud123,{'code':42}), (Aliyun,{'code':'Forbidden'}),(Baidu,{'errno':31066}),(Tianyi,{'res_code':1})]:
            with self.subTest(provider=cls.__name__):
                c=cls({'access_token':'x','cookie':'x'})
                with patch.object(c,'raw',return_value=data):
                    with self.assertRaises(CloudError): list(c.list(c.root))


if __name__ == '__main__':
    unittest.main()
