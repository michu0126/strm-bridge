import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from bridge.server import State
from bridge.core import make_plan
from bridge.cloud import uri
from bridge.cloud_cookie import Cookie115
from bridge.cloud_providers import item, CloudError


class GeneratedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.source=self.root/'source';self.source.mkdir()
        self.target=self.root/'target';self.target.mkdir()
        self.state=State(self.root/'data')
        self.state.config.update(source_roots=[str(self.source)],target_roots=[str(self.target)])
        self.options=dict(source=str(self.source),target=str(self.target),kind='movie',mode='path')
        (self.source/'a.mkv').write_bytes(b'video-a');(self.source/'b.mkv').write_bytes(b'video-b')
    def tearDown(self):
        self.state.watcher.close();self.state.uploads.close();self.state.pool.shutdown(wait=True);self.state.db.close();self.temp.cleanup()
    def plan(self):
        return self.state.make_plan(self.options,self.state.config)
    def execute(self,plan):
        return self.state.execute(plan,self.state.config,self.state.playback)
    def test_successful_records_skip_actions_but_new_subtitles_still_match(self):
        self.assertEqual(self.execute(self.plan())['created'],2)
        (self.source/'a.chs.srt').write_text('subtitle')
        plan=self.plan();self.assertEqual(plan['counts']['strm'],0);self.assertEqual(plan['counts']['recorded'],2)
        self.assertEqual(plan['counts']['copy'],1);self.assertEqual(Path(plan['actions'][0]['target']).name,'a.chs.srt')
    def test_delete_one_record_preserves_files_and_rewrites_only_one(self):
        self.execute(self.plan());records=self.state.generated.list()['records']
        record=next(r for r in records if r['source'].endswith('a.mkv'))
        old=(self.target/'a.strm').read_bytes()
        self.assertEqual(self.state.generated.delete([record['id']])['deleted'],1)
        self.assertEqual((self.target/'a.strm').read_bytes(),old)
        plan=self.plan();self.assertEqual(plan['counts']['strm'],1);self.assertEqual(plan['counts']['recorded'],1)
        self.assertEqual(self.execute(plan)['created'],1);self.assertEqual(self.state.generated.list()['total'],2)
        self.assertEqual(len(list((self.state.data/'strm-backups').iterdir())),1)
    def test_missing_or_changed_source_is_eligible(self):
        self.execute(self.plan());(self.target/'a.strm').unlink()
        (self.source/'b.mkv').write_bytes(b'changed')
        self.assertEqual(self.plan()['counts']['strm'],2)
        self.assertEqual(self.execute(self.plan())['errors'],[])
    def test_changed_user_output_is_not_overwritten(self):
        self.execute(self.plan());(self.target/'a.strm').write_text('user edited')
        result=self.execute(self.plan());self.assertTrue(result['errors'])
        self.assertEqual((self.target/'a.strm').read_text(),'user edited')
    def test_changed_config_updates_managed_output(self):
        self.execute(self.plan());self.state.config['public_url']='http://changed:18642'
        plan=self.plan();self.assertEqual(plan['counts']['strm'],2)
        self.assertEqual(self.execute(plan)['created'],2)
    def test_direct_single_plan_does_not_walk_source(self):
        self.execute(self.plan());record=self.state.generated.list()['records'][0]
        with patch('bridge.core.walk',side_effect=AssertionError('full scan')):
            plan=self.state.generated.single_plan(record['id'],self.state.config)
        self.assertEqual(len(plan['actions']),1);self.assertEqual(self.execute(plan)['created'],1)
    def test_async_single_regeneration_and_busy_delete(self):
        self.execute(self.plan());record=self.state.generated.list()['records'][0]
        self.state.busy='other'
        with self.assertRaises(ValueError):self.state.generated.delete([record['id']])
        self.state.busy=None
        job=self.state.start_scan(self.options,regenerate=record['id'])
        self.state.pool.shutdown(wait=True)
        self.assertEqual(self.state.job(job['id'])['status'],'completed')
        self.assertEqual(self.state.job(job['id'])['result']['created'],1)
    def test_legacy_backfill_checks_actual_output_and_tombstones(self):
        plan=make_plan(self.options,self.state.config)
        (self.target/'a.strm').write_bytes((str(self.source/'a.mkv')+'\n').encode())
        (self.target/'b.strm').write_text('wrong content')
        self.state.put_job({'id':'legacy','status':'completed','plan':plan})
        self.state.generated.backfill()
        records=self.state.generated.list()['records'];self.assertEqual(len(records),1)
        self.state.generated.delete([records[0]['id']]);self.state.generated.backfill()
        self.assertEqual(self.state.generated.list()['total'],0)
    def test_pagination_search_and_persistence(self):
        self.execute(self.plan());self.assertEqual(self.state.generated.list(search='a.mkv')['total'],1)
        self.assertEqual(self.state.generated.list(offset=1)['records'][0]['source'],str(self.source/'a.mkv'))
        from bridge.generated import GeneratedRecords
        self.assertEqual(GeneratedRecords(self.state).list()['total'],2)


class Recursive115Tests(unittest.TestCase):
    def test_recursive_protocol_pages_all_formats_and_rejects_incomplete(self):
        client=Cookie115({'cookie':'test'},Mock())
        rows=[{'fid':'1','cid':'folder','n':'film.mkv','s':120000000},{'fid':'2','cid':'folder','n':'film.srt','s':3}]
        with patch.object(client,'request',side_effect=[{'count':2,'data':rows[:1]},{'count':2,'data':rows[1:]}]) as request:
            pages=list(client.recursive_pages('root'))
        self.assertEqual([r['name'] for page in pages for r in page],['film.mkv','film.srt'])
        self.assertEqual(request.call_args_list[0].kwargs['query']['cur'],0)
        with patch.object(client,'request',side_effect=[{'count':2,'data':rows[:1]},{'count':2,'data':[]}]):
            with self.assertRaises(CloudError):list(client.recursive_pages('root'))
        with patch.object(client,'request',side_effect=[{'count':2,'data':rows[:1]},{'count':2,'data':rows[:1]}]):
            with self.assertRaises(CloudError):list(client.recursive_pages('root'))
    def test_fast_scan_skips_leaf_directory_requests_and_preserves_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();target=root/'library';target.mkdir();state=State(root/'data')
            try:
                state.config.update(target_roots=[str(target)])
                account=state.clouds.save({'name':'115','provider':'115','credentials':{'cookie':'UID=1; CID=2; SEID=3','auth_mode':'cookie'}})
                client=Cookie115({'cookie':'test'},Mock());state.clouds.clients[account['id']]=client
                folders=[item(str(i),'Film'+str(i),True) for i in range(1000)]
                files=[dict(item('file'+str(i),'film.mkv',False,120000000),parent=str(i)) for i in range(1000)]
                with patch.object(client,'list',return_value=iter(copy.deepcopy(folders))) as listing,patch.object(client,'recursive_pages',return_value=iter([copy.deepcopy(files)])),patch.object(client,'directory_path',side_effect=AssertionError('leaf request')):
                    plan=state.make_plan(dict(source=account['source'],target=str(target),kind='jav'),state.config)
                self.assertEqual(listing.call_count,1);self.assertEqual(plan['counts']['strm'],1000)
                self.assertEqual(Path(plan['actions'][0]['target']).relative_to(target).as_posix(),'Film0/film.strm')
            finally:
                state.watcher.close();state.uploads.close();state.pool.shutdown(wait=True);state.db.close()
