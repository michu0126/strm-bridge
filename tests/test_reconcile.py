import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge.server import State
from bridge.cloud import uri
from bridge.cloud_providers import item, CloudError


class Fake115:
    def __init__(self):
        self.tree={'0':[item('source','Movies',True)],'source':[],'other':[]}
    def list(self,parent):
        return iter(copy.deepcopy(self.tree[parent]))
    def directory_path(self,parent):
        if parent not in self.tree:raise CloudError('source missing')
        return []
    def add(self,identifier,name,parent='source'):
        obj=item(identifier,name,False,150000000,time.time()-120)
        self.tree[parent].append(obj)
        return obj


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.target=self.root/'library';self.target.mkdir()
        self.state=State(self.root/'data')
        self.state.config.update(target_roots=[str(self.target)],source_roots=[])
        self.account=self.state.clouds.save({'name':'115','provider':'115','credentials':{'access_token':'test'}})
        self.fake=Fake115();self.state.clouds.clients[self.account['id']]=self.fake
        self.state.clouds.browse(self.account['source'])
        self.options=dict(source=uri(self.account['id'],'source'),target=str(self.target),kind='jav',mode='redirect',reconcile=True)
        self.fake.add('a','a.mkv');self.fake.add('b','b.mkv')
        self.run_plan(self.plan())
    def tearDown(self):
        self.state.watcher.close();self.state.uploads.close();self.state.pool.shutdown(wait=True);self.state.db.close();self.temp.cleanup()
    def plan(self):
        return self.state.make_plan(self.options,self.state.config)
    def run_plan(self,plan):
        self.state.put_job({'id':plan['id'],'status':'preview','options':plan['options'],'plan':plan})
        return self.state.execute(plan,self.state.config,self.state.playback,journal=lambda k,v:self.state.journal(plan['id'],k,v))
    def test_deleted_source_recycles_only_recorded_strm_and_can_restore(self):
        self.fake.tree['source']=[o for o in self.fake.tree['source'] if o['id']!='a']
        (self.target/'unmanaged.strm').write_text('keep');(self.target/'poster.jpg').write_bytes(b'poster')
        plan=self.plan();self.assertEqual(plan['counts']['retire'],1)
        result=self.run_plan(plan);self.assertEqual(result['retired'],1);self.assertFalse((self.target/'a.strm').exists())
        self.assertTrue((self.target/'b.strm').exists());self.assertTrue((self.target/'unmanaged.strm').exists());self.assertTrue((self.target/'poster.jpg').exists())
        self.assertEqual(self.state.restore(plan['id'])['restored'],1);self.assertTrue((self.target/'a.strm').exists())
        self.assertEqual(self.state.restore(plan['id'])['restored'],0)
    def test_rename_creates_new_before_retiring_old(self):
        self.fake.tree['source'][0]['name']='renamed.mkv'
        plan=self.plan();self.assertEqual(plan['counts']['strm'],1);self.assertEqual(plan['reconcile_summary']['renamed'],1)
        result=self.run_plan(plan);self.assertEqual(result['errors'],[])
        self.assertTrue((self.target/'renamed.strm').exists());self.assertFalse((self.target/'a.strm').exists())
        self.assertEqual(self.plan()['counts']['strm'],0);self.assertEqual(self.plan()['counts']['retire'],0)
    def test_content_hash_changes_with_same_size_and_time_are_updated(self):
        self.fake.tree['source'][0]['sha1']='A'*40
        self.run_plan(self.plan())
        self.fake.tree['source'][0]['sha1']='B'*40
        plan=self.plan();self.assertEqual(plan['counts']['strm'],1)
        self.fake.tree['source'][0]['sha1']='C'*40
        self.assertTrue(self.run_plan(plan)['errors'])
        self.assertEqual(self.run_plan(self.plan())['errors'],[])
    def test_moved_file_and_deleted_directory_follow_paths(self):
        self.fake.tree['source'].append(item('nested','Nested',True));self.fake.tree['nested']=[self.fake.tree['source'].pop(0)]
        result=self.run_plan(self.plan());self.assertEqual(result['retired'],1)
        self.assertTrue((self.target/'Nested/a.strm').exists());self.assertFalse((self.target/'a.strm').exists())
        self.fake.tree['source']=[o for o in self.fake.tree['source'] if o['id']!='nested']
        result=self.run_plan(self.plan());self.assertEqual(result['retired'],1);self.assertFalse((self.target/'Nested').exists())
    def test_same_name_replacement_and_swap_keep_new_output(self):
        old=self.fake.tree['source'].pop(0);self.fake.add('new','a.mkv')
        result=self.run_plan(self.plan());self.assertEqual(result['errors'],[])
        self.assertEqual(self.state.generated.list()['total'],2)
        self.fake.tree['source'][0]['name']='a.mkv';self.fake.tree['source'][1]['name']='b.mkv'
        result=self.run_plan(self.plan());self.assertEqual(result['errors'],[])
        self.assertTrue((self.target/'a.strm').exists());self.assertTrue((self.target/'b.strm').exists())
    def test_modified_user_output_and_failed_replacement_are_preserved(self):
        self.fake.tree['source'][0]['name']='new.mkv';(self.target/'new.strm').write_text('unmanaged')
        result=self.run_plan(self.plan());self.assertTrue(result['errors']);self.assertEqual(result['retired'],0)
        self.assertTrue((self.target/'a.strm').exists())
        self.fake.tree['source']=[];(self.target/'a.strm').write_text('user changed')
        result=self.run_plan(self.plan());self.assertTrue(result['errors']);self.assertTrue((self.target/'a.strm').exists())
    def test_reappearing_file_old_preview_and_failed_scan_do_not_remove(self):
        old=self.fake.tree['source'].pop(0);plan=self.plan();self.fake.tree['source'].append(old)
        result=self.run_plan(plan);self.assertTrue(result['errors']);self.assertTrue((self.target/'a.strm').exists())
        with patch.object(self.fake,'list',side_effect=CloudError('offline')):
            with self.assertRaises(CloudError):self.plan()
        self.assertTrue((self.target/'b.strm').exists())
    def test_empty_cloud_directory_is_valid_and_cancel_preserves(self):
        self.fake.tree['source']=[];plan=self.plan();self.assertEqual(plan['counts']['retire'],2)
        result=self.state.execute(plan,self.state.config,self.state.playback,cancel=lambda:True)
        self.assertTrue(result['cancelled']);self.assertTrue((self.target/'a.strm').exists())
        self.assertEqual(self.run_plan(plan)['retired'],2)
    def test_monitor_opt_in_persists_and_reconciles(self):
        watch=self.state.watcher.save({'options':dict(self.options,reconcile=False)})
        self.fake.tree['source'].pop(0)
        self.state.watcher.run(watch,'watch-before',dict(self.state.config));self.assertTrue((self.target/'a.strm').exists())
        watch=self.state.watcher.save({'id':watch['id'],'reconcile':True})
        self.state.watcher.run(watch,'watch-after',dict(self.state.config));self.assertFalse((self.target/'a.strm').exists())
        self.assertEqual(self.state.watcher.list()[0]['last_result']['retired'],1)
