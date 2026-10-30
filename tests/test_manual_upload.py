import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.server import State
from bridge.upload_watcher import UploadWatcher
from bridge.cloud_providers import item
from test_upload_watcher import Fake115


class ManualUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.state = State(self.root / 'data')
        self.state.config.update(source_roots=[], target_roots=[str(self.source)], upload_roots=[])
        self.account = self.state.clouds.save({'provider': '115', 'credentials': {
            'auth_mode': 'cookie', 'cookie': 'UID=test; CID=test; SEID=test'}})
        self.fake = Fake115()
        self.factory = patch('bridge.upload_watcher.Cookie115', return_value=self.fake)
        self.factory.start()

    def tearDown(self):
        self.factory.stop()
        self.state.uploads.close(); self.state.watcher.close()
        self.state.pool.shutdown(wait=True); self.state.db.close(); self.temp.cleanup()

    def file(self, name, content=b'upload contents'):
        p = self.source / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content)
        return p

    def create(self, paths, **options):
        return self.state.uploads.manual(dict(sources=[str(p) for p in paths], target=self.account['source'], **options))

    def test_selected_files_upload_immediately_once_and_leave_other_files(self):
        a, b, c = self.file('a.mp4'), self.file('b.srt'), self.file('other.nfo')
        task = self.create([a, b])
        self.assertEqual(self.state.job(task['job_id'])['status'], 'queued')
        self.assertTrue(self.state.uploads.tick())
        task = self.state.uploads.get(task['id'])
        self.assertEqual(task['last_status'], 'completed'); self.assertFalse(task['enabled'])
        self.assertEqual({r['name'] for r in self.fake.tree['0']}, {'a.mp4', 'b.srt'})
        self.assertTrue(all(p.is_file() for p in [a, b, c]))
        self.file('later.mp4')
        self.assertFalse(self.state.uploads.tick())

    def test_folder_upload_preserves_contents_and_skips_temporary_files(self):
        self.file('folder/nested/movie.mkv'); self.file('folder/nested/poster.jpg')
        self.file('folder/unfinished.part')
        task = self.create([self.source / 'folder'])
        self.state.uploads.tick()
        folder = self.fake.tree['0'][0]
        self.assertEqual(folder['name'], 'folder')
        nested = self.fake.tree[folder['id']][0]
        self.assertEqual(nested['name'], 'nested')
        self.assertEqual({r['name'] for r in self.fake.tree[nested['id']]}, {'movie.mkv', 'poster.jpg'})
        self.assertEqual(self.state.uploads.get(task['id'])['last_result']['uploaded'], 2)

    def test_selected_mount_root_keeps_its_name_without_accessing_parent(self):
        self.file('季 1/原片名 #01.mkv')
        task = self.create([self.source]); self.state.uploads.tick()
        folder = self.fake.tree['0'][0]
        self.assertEqual(folder['name'], self.source.name)
        season = self.fake.tree[folder['id']][0]
        self.assertEqual(season['name'], '季 1')
        self.assertEqual(self.fake.tree[season['id']][0]['name'], '原片名 #01.mkv')
        self.assertEqual(task['source'], str(self.source.resolve()))

    def test_single_and_multiple_folder_selection_use_the_same_remote_paths(self):
        self.file('影片甲/字幕/movie.srt'); self.file('影片乙/字幕/movie.srt')
        task = self.create([self.source/'影片甲', self.source/'影片乙'])
        self.state.uploads.tick()
        self.assertEqual({r['name'] for r in self.fake.tree['0']}, {'影片甲', '影片乙'})
        again = self.create([self.source/'影片甲']); self.state.uploads.tick()
        self.assertEqual(self.state.uploads.get(again['id'])['last_result']['skipped'], 1)
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(self.state.uploads.get(task['id'])['last_result']['uploaded'], 2)

    def test_empty_directory_tree_is_retained_and_original_directories_remain(self):
        source = self.source/'空目录'/'子目录'; source.mkdir(parents=True)
        task = self.create([source.parent]); self.state.uploads.tick()
        folder = self.fake.tree['0'][0]
        self.assertEqual(folder['name'], '空目录')
        child = self.fake.tree[folder['id']][0]
        self.assertEqual(child['name'], '子目录')
        self.assertEqual(self.fake.tree[child['id']], [])
        self.assertEqual(self.state.uploads.get(task['id'])['last_status'], 'completed')
        self.assertTrue(source.is_dir())

    def test_existing_folder_name_conflict_stops_instead_of_flattening(self):
        self.file('folder/movie.mkv')
        self.fake.tree['0'] = [item('existing-file', 'folder', size=5)]
        task = self.create([self.source/'folder']); self.state.uploads.tick()
        self.assertEqual(self.state.uploads.get(task['id'])['last_status'], 'failed')
        self.assertEqual(self.fake.calls, [])
        self.assertTrue((self.source/'folder/movie.mkv').exists())

    def test_legacy_pending_task_retains_its_original_destination_on_resume(self):
        self.file('folder/nested/movie.mkv')
        task = self.create([self.source/'folder'])
        task.pop('target_prefix'); self.state.uploads.put(task)
        self.state.uploads.tick()
        self.assertEqual(self.fake.tree['0'][0]['name'], 'nested')

    def test_large_task_keeps_one_job_and_a_frozen_manifest_across_batches(self):
        for i in range(205): self.file('folder/%03d.bin' % i)
        task = self.create([self.source / 'folder'])
        self.state.uploads.tick()
        job = self.state.job(task['job_id'])
        self.assertEqual((job['status'], job['done'], job['total']), ('queued', 200, 205))
        self.file('folder/new-after-start.bin')
        self.state.uploads.tick()
        job = self.state.job(task['job_id'])
        self.assertEqual((job['status'], job['done'], job['total']), ('completed', 205, 205))
        self.assertEqual(job['result']['uploaded'], 205)
        self.assertEqual(len(self.state.jobs()), 1)
        self.assertEqual(len(self.fake.calls), 205)

    def test_pause_resume_and_failure_retry_preserve_session_and_task_id(self):
        p = self.file('video.mp4'); task = self.create([p])
        self.fake.on_upload = lambda path: self.state.uploads.manual_control(task['id'], 'pause')
        self.state.uploads.tick()
        self.assertEqual(self.state.uploads.get(task['id'])['last_status'], 'paused')
        self.assertEqual(self.state.job(task['job_id'])['status'], 'cancelled')
        self.assertTrue(p.exists())
        self.fake.on_upload = None; self.fake.fail = True
        self.state.uploads.manual_control(task['id'], 'resume'); self.state.uploads.tick()
        self.assertEqual(self.state.job(task['job_id'])['status'], 'partial')
        self.assertFalse(self.state.uploads.tick(), 'manual failures must wait for an explicit retry')
        self.fake.fail = False
        self.state.uploads.manual_control(task['id'], 'retry'); self.state.uploads.tick()
        job = self.state.job(task['job_id'])
        self.assertEqual(job['status'], 'completed'); self.assertEqual(job['result']['uploaded'], 1)
        self.assertEqual(job['result']['errors'], [])
        self.assertEqual(self.fake.calls[-1][2]['upload_id'], 'persisted-test-session')

    def test_delete_local_remains_opt_in_and_remote_conflicts_keep_source(self):
        p = self.file('delete.bin', b'verified')
        task = self.create([p], delete_local=True); self.state.uploads.tick()
        self.assertFalse(p.exists())
        self.assertEqual(self.state.uploads.get(task['id'])['last_result']['deleted'], 1)
        p = self.file('delete.bin', b'different')
        task = self.create([p], delete_local=True); self.state.uploads.tick()
        self.assertTrue(p.exists())
        self.assertEqual(self.state.uploads.get(task['id'])['last_status'], 'partial')

    def test_paused_queue_can_be_removed_without_uploading_or_deleting_source(self):
        p = self.file('keep.bin'); task = self.create([p])
        self.state.uploads.manual_control(task['id'], 'pause')
        self.assertFalse(self.state.uploads.tick())
        self.state.uploads.manual_control(task['id'], 'delete')
        self.assertTrue(p.exists()); self.assertEqual(self.fake.calls, [])

    def test_completed_task_and_monitor_can_use_the_same_folder(self):
        self.file('existing.mp4'); task = self.create([self.source])
        self.state.uploads.tick()
        monitor = self.state.uploads.save({'source': str(self.source), 'target': self.account['source']})
        self.assertNotEqual(task['id'], monitor['id'])
        self.assertEqual(monitor.get('mode'), None)

    def test_invalid_sources_and_duplicate_active_requests_are_rejected(self):
        p = self.file('file.bin'); self.create([p])
        for paths in [[p], [self.root], [self.source/'missing'], [self.file('unfinished.part')]]:
            with self.assertRaises(ValueError): self.create(paths)
        for sources in [[], [None], [{'path': str(p)}]]:
            with self.assertRaises(ValueError): self.state.uploads.manual({'sources': sources, 'target': self.account['source']})

    def test_pending_manual_task_survives_worker_restart(self):
        p = self.file('restart.bin'); task = self.create([p])
        self.state.uploads = UploadWatcher(self.state)
        self.state.uploads.tick()
        self.assertEqual(self.state.job(task['job_id'])['status'], 'completed')

    def test_missing_pending_source_fails_without_marking_upload_success(self):
        p = self.file('missing.bin'); task = self.create([p]); p.unlink()
        self.state.uploads.tick()
        self.assertEqual(self.state.job(task['job_id'])['status'], 'failed')
        self.assertFalse(self.state.uploads.get(task['id'])['enabled'])
        self.assertEqual(self.fake.calls, [])

    def browser(self, files):
        return self.state.uploads.browser_begin({'target': self.account['source'], 'files': files})

    def test_browser_files_stream_to_nas_and_verified_cache_is_cleaned(self):
        task = self.browser([{'name': 'folder/file.bin', 'size': 6}, {'name': 'empty.txt', 'size': 0}])
        self.assertFalse(self.state.uploads.tick())
        self.state.uploads.browser_chunk(task['id'], 0, 0, 3, io.BytesIO(b'abc'))
        self.state.uploads.browser_chunk(task['id'], 0, 3, 3, io.BytesIO(b'def'))
        root = Path(task['source'])
        self.assertEqual((root/'folder/file.bin').read_bytes(), b'abcdef')
        self.state.uploads.browser_finish(task['id']); self.state.uploads.tick()
        job = self.state.job(task['job_id'])
        self.assertEqual((job['status'], job['result']['uploaded']), ('completed', 2))
        self.assertFalse(root.exists())
        self.assertEqual(len(self.fake.calls), 2)

    def test_incomplete_browser_chunk_can_resume_without_duplicate_bytes(self):
        task = self.browser([{'name': 'resume.bin', 'size': 6}])
        with self.assertRaises(ValueError):
            self.state.uploads.browser_chunk(task['id'], 0, 0, 3, io.BytesIO(b'a'))
        self.assertEqual(self.state.uploads.browser_task(task['id'])['received_bytes'], 0)
        with self.assertRaises(ValueError): self.state.uploads.browser_finish(task['id'])
        self.state.uploads.browser_chunk(task['id'], 0, 0, 6, io.BytesIO(b'abcdef'))
        self.assertEqual((Path(task['source'])/'resume.bin').read_bytes(), b'abcdef')
        self.state.uploads.browser_finish(task['id']); self.state.uploads.tick()
        self.assertEqual(self.state.job(task['job_id'])['status'], 'completed')

    def test_browser_failure_keeps_cached_file_for_manual_retry(self):
        task = self.browser([{'name': 'retry.bin', 'size': 3}])
        self.state.uploads.browser_chunk(task['id'], 0, 0, 3, io.BytesIO(b'abc'))
        self.state.uploads.browser_finish(task['id']); self.fake.fail = True; self.state.uploads.tick()
        self.assertTrue((Path(task['source'])/'retry.bin').exists())
        self.fake.fail = False
        self.state.uploads.manual_control(task['id'], 'retry'); self.state.uploads.tick()
        self.assertEqual(self.state.job(task['job_id'])['status'], 'completed')
        self.assertFalse(Path(task['source']).exists())

    def test_removing_browser_receiving_task_cleans_only_its_cache(self):
        original = self.file('original.bin')
        task = self.browser([{'name': 'cached.bin', 'size': 3}])
        other = self.browser([{'name': 'other.bin', 'size': 1}])
        self.state.uploads.browser_chunk(task['id'], 0, 0, 3, io.BytesIO(b'abc'))
        self.state.uploads.manual_control(task['id'], 'delete')
        self.assertFalse(Path(task['source']).exists())
        self.assertTrue(Path(other['source']).exists()); self.assertTrue(original.exists())

    def test_browser_paths_sizes_and_out_of_order_chunks_are_rejected(self):
        invalid = ['../outside', '/outside', 'a/../../b', 'a\\b', 'a//b']
        for name in invalid:
            with self.assertRaises(ValueError): self.browser([{'name': name, 'size': 3}])
        with self.assertRaises(ValueError): self.browser([{'name':'same','size':1},{'name':'same','size':2}])
        with self.assertRaises(ValueError): self.browser([{'name':'a','size':1},{'name':'a/b','size':2}])
        with self.assertRaises(ValueError): self.browser(['invalid'])
        task = self.browser([{'name': 'file.bin', 'size': 3}])
        with self.assertRaises(ValueError): self.state.uploads.manual_control(task['id'], 'resume')
        with self.assertRaises(ValueError): self.state.uploads.browser_chunk(task['id'], 0, 1, 2, io.BytesIO(b'ab'))
        with self.assertRaises(ValueError): self.state.uploads.browser_chunk(task['id'], 0, 0, 4, io.BytesIO(b'abcd'))

    def test_browser_explicit_hidden_folder_is_not_silently_excluded(self):
        task = self.browser([{'name': '.git/selected.txt', 'size': 3}])
        self.state.uploads.browser_chunk(task['id'], 0, 0, 3, io.BytesIO(b'abc'))
        self.state.uploads.browser_finish(task['id']); self.state.uploads.tick()
        self.assertEqual(self.state.job(task['job_id'])['status'], 'completed')
        self.assertEqual(self.state.uploads.get(task['id'])['uploaded_total'], 1)
        self.assertFalse(Path(task['source']).exists())

    def test_browser_disk_reservations_prevent_overcommitting_free_space(self):
        free = type('Disk', (), {'free': 33*1024*1024})()
        with patch('bridge.upload_watcher.shutil.disk_usage', return_value=free):
            self.browser([{'name': 'first.bin', 'size': 800_000}])
            with self.assertRaises(ValueError): self.browser([{'name': 'second.bin', 'size': 800_000}])
