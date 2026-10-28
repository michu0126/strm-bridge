import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from bridge.server import State
from bridge.cloud import uri
from bridge.cloud_cookie import Cookie115
from bridge.cloud_providers import item,CloudError
from bridge.videos import below


class VideoClient:
    def __init__(self):
        self.tree={'0':[item('source','Source',True),item('dest','Destination',True)],'source':[],'dest':[]}
        self.dimensions={};self.calls=[];self.operations=[]
        for key,name,dimensions in [('low','360.mkv',(640,360)),('hd','1080.mkv',(1920,1080)),('crop','crop.mkv',(1920,800)),('unknown','unknown.mkv',(0,0))]:
            self.tree['source'].append(item(key,name,False,150000000,time.time()-120,pickcode='pick-'+key));self.dimensions[key]=dimensions
    def list(self,parent):return iter(copy.deepcopy(self.tree[parent]))
    def directory_path(self,parent):
        if parent not in self.tree:raise CloudError('missing directory')
        return []
    def recursive_pages(self,parent):yield [dict(o,parent=parent) for o in copy.deepcopy(self.tree[parent]) if not o['folder']]
    def video_info(self,obj):
        self.calls.append(obj['id']);w,h=self.dimensions[obj['id']];return {'width':w,'height':h}
    def move(self,obj,destination):
        self.tree[obj['parent']]=[o for o in self.tree[obj['parent']] if o['id']!=obj['id']]
        self.tree[destination].append(dict(obj,parent=destination));self.operations.append(('move',obj['id']))
    def delete(self,obj):
        self.tree[obj['parent']]=[o for o in self.tree[obj['parent']] if o['id']!=obj['id']];self.operations.append(('delete',obj['id']))


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();self.state=State(self.root/'data')
        self.account=self.state.clouds.save({'name':'115','provider':'115','credentials':{'auth_mode':'cookie','cookie':'UID=1; CID=2; SEID=3'}})
        self.client=VideoClient();self.state.clouds.clients[self.account['id']]=self.client;self.state.clouds.browse(self.account['source'])
        self.options={'source':uri(self.account['id'],'source'),'height':1080,'rule':'tier'}
    def tearDown(self):
        self.state.videos.close();self.state.watcher.close();self.state.uploads.close();self.state.pool.shutdown(wait=True);self.state.db.close();self.temp.cleanup()
    def scan(self,**kwargs):
        s=self.state.videos.start(dict(self.options,**kwargs));self.state.videos.pool.submit(lambda:None).result(timeout=10);return self.state.videos.get(s['id'])
    def operation(self,scan,operation,ids=['low']):
        task=self.state.videos.batch({'scan_id':scan['id'],'operation':operation,'ids':ids,'target':uri(self.account['id'],'dest')})
        self.state.videos.pool.submit(lambda:None).result(timeout=10);return self.state.videos.get(task['id'])
    def test_thresholds_crops_portrait_and_unknown(self):
        self.assertTrue(below(640,360));self.assertTrue(below(720,1280));self.assertFalse(below(1920,800));self.assertTrue(below(1920,800,rule='height'));self.assertFalse(below(1920,1080));self.assertFalse(below(0,720))
    def test_actual_dimensions_filter_persist_and_cache(self):
        scan=self.scan();self.assertEqual(scan['status'],'completed');self.assertEqual(scan['matched'],1);self.assertEqual(scan['unknown'],1)
        result=self.state.videos.rows(scan['id']);self.assertEqual(result['rows'][0]['id'],'low')
        self.assertNotIn('remote',result['rows'][0]);self.assertEqual(self.state.videos.rows(scan['id'],filter_='unknown')['total'],1)
        second=self.scan();self.assertEqual(second['cached'],4);self.assertEqual(len(self.client.calls),4)
        self.client.tree['source'][0]['size']+=1;self.scan();self.assertEqual(len(self.client.calls),5)
    def test_strict_height_mode_detects_cropped_video(self):
        scan=self.scan(rule='height');self.assertEqual(scan['matched'],2)
    def test_move_is_verified_and_existing_target_is_not_overwritten(self):
        scan=self.scan();task=self.operation(scan,'move');self.assertEqual(task['result']['moved'],1);self.assertEqual(task['status'],'completed')
        self.assertEqual(self.client.tree['dest'][0]['id'],'low');self.assertIsNone(self.state.busy)
        self.assertEqual(self.state.videos.rows(scan['id'])['rows'][0]['operation'],'move')
    def test_delete_only_selected_confirmed_low_file_and_schedule_sync(self):
        library=self.root/'library';library.mkdir();self.state.config['target_roots']=[str(library)]
        watch=self.state.watcher.save({'options':dict(source=self.options['source'],target=str(library),kind='jav',mode='redirect',reconcile=True)})
        watch['next_run']=time.time()+10000;self.state.watcher.put(watch)
        scan=self.scan();task=self.operation(scan,'delete');self.assertEqual(task['result']['deleted'],1)
        self.assertEqual(self.client.operations,[('delete','low')]);self.assertEqual(len(self.client.tree['source']),3)
        self.assertEqual(self.state.watcher.list()[0]['next_run'],0)
    def test_unknown_or_full_hd_cannot_be_submitted_to_batch(self):
        scan=self.scan()
        for key in ('unknown','hd','crop','does-not-exist'):
            with self.assertRaises(ValueError):self.operation(scan,'delete',[key])
        self.assertEqual(self.client.operations,[])
    def test_changed_source_and_destination_conflict_preserve_files(self):
        scan=self.scan();self.client.tree['source'][0]['size']+=100
        task=self.operation(scan,'delete');self.assertEqual(task['result']['deleted'],0);self.assertTrue(task['result']['errors'])
        scan=self.scan();self.client.tree['dest'].append(item('conflict','360.mkv',False,10))
        task=self.operation(scan,'move');self.assertEqual(task['result']['moved'],0);self.assertTrue(task['result']['errors']);self.assertEqual(self.client.operations,[])
    def test_scan_cancel_and_busy_generation_prevent_mutation(self):
        scan=self.scan();self.state.busy='generation'
        with self.assertRaises(ValueError):self.operation(scan,'delete')
        self.state.busy=None
        with patch.object(self.state.videos.pool,'submit'):
            job=self.state.videos.start(self.options);self.state.cancelled.add(job['id']);self.state.videos.scan(job,False)
        self.assertEqual(self.state.videos.get(job['id'])['status'],'cancelled');self.assertEqual(self.client.operations,[])
    def test_failed_video_info_remains_unknown_and_cannot_be_deleted(self):
        with patch.object(self.client,'video_info',side_effect=CloudError('not available')):scan=self.scan()
        self.assertEqual(scan['matched'],0);self.assertEqual(scan['unknown'],4);self.assertEqual(self.state.videos.rows(scan['id'])['total'],0)


class VideoProtocolTests(unittest.TestCase):
    def test_original_dimensions_are_used_not_transcode_list(self):
        c=Cookie115({'cookie':'test'});obj={'id':'1','pickcode':'pc','parent':'folder'}
        with patch.object(c,'request',return_value={'width':'640','height':'360','file_id':'1','definition_list':{'1080':'url'}}):self.assertEqual(c.video_info(obj),{'width':640,'height':360})
        with patch.object(c,'request',return_value={'width':'1920','height':'1080','file_id':'other'}):
            with self.assertRaises(CloudError):c.video_info(obj)
        with patch.object(c,'request') as request:c.delete(obj)
        self.assertEqual(request.call_args.args[0],'/rb/delete');self.assertEqual(request.call_args.kwargs['form']['fid[0]'],'1')
