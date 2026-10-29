import unittest
from unittest.mock import patch
from bridge.cloud_providers import Cloud115,Cloud123,Aliyun,Baidu,Tianyi
from bridge.cloud_cookie import Cookie115,Web123
from tests import test_cloud as fixtures

class DeleteProtocolTests(unittest.TestCase):
    def test_native_delete_contracts(self):
        obj={'id':'123','parent':'456','name':'ad.txt','folder':False}
        cases=[(Cloud115,'/open/ufile/delete','form','file_ids'),(Cloud123,'/api/v1/file/trash','body','fileIDs'),
          (Aliyun,'/adrive/v1.0/openFile/recyclebin/trash','body','file_id'),(Baidu,'/xpan/file','form','filelist'),
          (Tianyi,'/api/open/batch/createBatchTask.action','form','type'),(Cookie115,'/rb/delete','form','fid[0]'),
          (Web123,'/file/trash','body','fileTrashInfoList')]
        for cls,endpoint,encoding,key in cases:
            with self.subTest(provider=cls.__name__):
                client=cls({'drive_id':'d'})
                with patch.object(client,'request',return_value={}) as request:client.delete(obj)
                self.assertEqual(request.call_args.args[:2],(endpoint,'POST'));self.assertIn(key,request.call_args.kwargs[encoding])
                self.assertNotIn('purge',endpoint);self.assertNotEqual('/open/rb/del',endpoint)

class CleanupGuardTests(unittest.TestCase):
    setUp=fixtures.NativeCloudTests.setUp
    tearDown=fixtures.NativeCloudTests.tearDown
    plan=fixtures.NativeCloudTests.plan
    run_plan=fixtures.NativeCloudTests.run_plan
    # Reuse the native-cloud fixture without rerunning its base test methods.
    def test_delete_noop_is_not_reported_as_success(self):
        self.fake.add('main.mkv',100_000_000);ad=self.fake.add('ad.exe',12)
        plan=self.plan(kind='jav',cleanup=True)
        with patch.object(self.fake,'delete'),patch('bridge.cloud.time.sleep'):result=self.run_plan(plan)
        self.assertEqual(result['deleted'],0);self.assertTrue(result['errors']);self.assertTrue(any(x['id']==ad['id'] for x in self.fake.tree['dir']))
        self.assertEqual(set(self.fake.tree),{'0','dir'})
    def test_changed_hash_preserves_file_even_when_size_and_time_match(self):
        ad=self.fake.add('ad.mp4',12);ad['sha1']='A'*40;plan=self.plan(kind='jav',cleanup=True);ad['sha1']='B'*40
        result=self.run_plan(plan);self.assertEqual(result['deleted'],0);self.assertFalse(self.fake.deletes);self.assertTrue(result['errors'])
    def test_legacy_cleanup_preview_must_be_refreshed(self):
        self.fake.add('ad.mp4',12);plan=self.plan(kind='jav',cleanup=True)
        for action in plan['actions']:action['type']='trash'
        with self.assertRaisesRegex(ValueError,'重新扫描'):self.run_plan(plan)
        self.assertFalse(self.fake.deletes);self.assertFalse(self.fake.moves)

    def test_cloud_watch_deletes_only_after_preview_opt_in(self):
        self.fake.add('main.mkv',100_000_000);ad=self.fake.add('ad.exe',12)
        options=dict(self.options,kind='jav',cleanup=True)
        plan=self.plan(kind='jav',cleanup=True)
        self.state.put_job({'id':'preview','status':'preview','plan':plan,'options':options})
        watch=self.state.watcher.save({'options':options,'cleanup_preview':'preview'})
        self.assertTrue(watch['options']['cleanup']);self.state.watcher.tick();self.state.pool.submit(lambda:None).result(timeout=10)
        self.assertEqual(self.fake.deletes,[ad['id']]);self.assertFalse(self.fake.moves)
        self.assertEqual(self.state.watcher.list()[0]['last_result']['deleted'],1)
