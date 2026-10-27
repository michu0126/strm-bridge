import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge.server import State
from bridge.cloud import uri
from bridge.cloud_providers import item, CloudError
from bridge.upload_watcher import UploadWatcher


class Fake115:
    def __init__(self):
        self.tree = {'0': []}
        self.calls = []
        self.fail = False
        self.on_upload = None
    def list(self, parent):
        return iter(copy.deepcopy(self.tree[parent]))
    def mkdir(self, parent, name):
        identifier = str(len(self.tree))
        self.tree[parent].append(item(identifier, name, True))
        self.tree[identifier] = []
        return identifier
    def upload(self, path, parent, sha1, session, checkpoint, progress, cancel):
        self.calls.append((str(path), parent, session))
        checkpoint({'upload_id': 'persisted-test-session', 'sha1': sha1})
        if self.on_upload:
            self.on_upload(path)
        progress('uploading', path.stat().st_size, path.stat().st_size)
        if self.fail:
            raise CloudError('网络中断')
        self.tree[parent].append(item('file-' + str(len(self.calls)), path.name,
            size=path.stat().st_size, sha1=sha1))


class UploadWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.state = State(self.root / 'data')
        self.state.config.update(source_roots=[], target_roots=[str(self.source)], upload_roots=[])
        self.account = self.state.clouds.save({'provider': '115', 'credentials': {
            'auth_mode': 'cookie', 'cookie': 'UID=test; CID=test; SEID=test'}})
        self.target = self.account['source']
        self.fake = Fake115()
        self.factory = patch('bridge.upload_watcher.Cookie115', return_value=self.fake)
        self.factory.start()
    def tearDown(self):
        self.factory.stop()
        self.state.uploads.close(); self.state.watcher.close()
        self.state.pool.shutdown(wait=True); self.state.db.close(); self.temp.cleanup()
    def save(self, **kwargs):
        return self.state.uploads.save(dict(source=str(self.source), target=self.target,
            interval=60, stable_seconds=10, **kwargs))
    def due(self):
        w = self.state.uploads.list()[0]; w['next_run'] = 0; self.state.uploads.put(w)
    def run_watch(self, stable=True):
        self.due()
        future = time.time() + (120 if stable else 0)
        with patch('bridge.upload_watcher.time.time', return_value=future):
            return self.state.uploads.tick()
    def write(self, name, data=b'new local data'):
        p = self.source / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
        return p
    def observe(self):
        self.run_watch(False)

    def test_existing_files_are_a_baseline_and_only_new_files_upload(self):
        old = self.write('already-there.mp4')
        self.save(); self.run_watch()
        self.assertEqual(self.fake.calls, [])
        fresh = self.write('nested/new.mp4')
        self.observe(); self.run_watch()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertTrue(old.exists()); self.assertEqual(fresh.read_bytes(), b'new local data')
        folder = self.fake.tree['0'][0]
        self.assertEqual(folder['name'], 'nested')
        self.assertEqual(self.fake.tree[folder['id']][0]['name'], 'new.mp4')
        self.run_watch(); self.assertEqual(len(self.fake.calls), 1)
        self.assertIsNone(self.state.busy)
        self.assertEqual(self.state.jobs()[0]['type'], 'upload')

    def test_optional_initial_upload_includes_all_types_but_not_temporary_downloads(self):
        for name in ['film.mkv', 'film.srt', 'poster.jpg', 'metadata.nfo', 'README']:
            self.write(name)
        for name in ['unfinished.part', 'browser.crdownload', 'qbit.!qB']:
            self.write(name)
        self.save(include_existing=True); self.run_watch()
        self.assertEqual(len(self.fake.calls), 5)
        self.assertEqual(len(list(self.source.iterdir())), 8)

    def test_new_and_changed_files_wait_for_a_stable_signature(self):
        self.save(); p = self.write('still-writing.mp4')
        self.observe(); self.assertEqual(self.fake.calls, [])
        p.write_bytes(b'longer data')
        self.observe(); self.assertEqual(self.fake.calls, [])
        self.run_watch(); self.assertEqual(len(self.fake.calls), 1)

    def test_same_name_conflicts_never_overwrite_and_identical_content_is_skipped(self):
        self.write('conflict.mp4', b'local'); self.write('same.srt', b'same')
        self.fake.tree['0'] = [item('old', 'conflict.mp4', size=5, sha1=hashlib.sha1(b'other').hexdigest()),
                               item('same', 'same.srt', size=4, sha1=hashlib.sha1(b'same').hexdigest())]
        self.save(include_existing=True); self.run_watch()
        self.assertEqual(self.fake.calls, [])
        result = self.state.uploads.list()[0]['last_result']
        self.assertEqual((result['conflicts'], result['skipped']), (1, 1))
        self.assertEqual(self.fake.tree['0'][0]['id'], 'old')

    def test_failure_checkpoint_survives_restart_and_retry_does_not_repeat_completed_files(self):
        self.write('retry.mp4'); watch = self.save(include_existing=True)
        self.fake.fail = True; self.run_watch()
        self.assertEqual(self.state.uploads.files(watch['id'])['retry.mp4']['session']['upload_id'], 'persisted-test-session')
        self.state.uploads = UploadWatcher(self.state)
        self.fake.fail = False
        self.state.uploads.control(watch['id'], retry=True); self.run_watch()
        self.assertEqual(self.fake.calls[-1][2]['upload_id'], 'persisted-test-session')
        self.run_watch(); self.assertEqual(len(self.fake.calls), 2)

    def test_pause_during_transfer_retains_pending_file_and_removal_keeps_source(self):
        p = self.write('pause.mp4'); watch = self.save(include_existing=True)
        self.fake.on_upload = lambda path: self.state.uploads.save({'id': watch['id'], 'enabled': False})
        self.run_watch()
        self.assertFalse(self.state.uploads.list()[0]['enabled'])
        self.assertEqual(self.state.uploads.files(watch['id'])['pause.mp4']['status'], 'pending')
        self.state.uploads.control(watch['id'], delete=True)
        self.assertTrue(p.exists()); self.assertEqual(self.state.uploads.files(watch['id']), {})
        self.state.uploads.put_file(watch['id'], 'pause.mp4', {'should': 'not reappear'})
        self.assertEqual(self.state.uploads.files(watch['id']), {})

    def test_source_changes_during_upload_are_not_marked_successful(self):
        self.write('changing.mp4'); watch = self.save(include_existing=True)
        self.fake.on_upload = lambda path: path.write_bytes(b'changed during upload')
        self.run_watch()
        self.assertEqual(self.state.uploads.files(watch['id'])['changing.mp4']['status'], 'failed')
        self.assertEqual(self.state.uploads.list()[0]['uploaded_total'], 0)

    def test_upload_is_not_completed_until_cloud_listing_verifies_hash(self):
        self.write('verify.mp4'); watch = self.save(include_existing=True)
        self.fake.upload = lambda *args, **kwargs: {'rapid': False}
        with patch.object(self.state.uploads.stop, 'wait', return_value=False):
            self.run_watch()
        self.assertEqual(self.state.uploads.files(watch['id'])['verify.mp4']['status'], 'failed')
        self.assertEqual(self.state.uploads.list()[0]['uploaded_total'], 0)

    def test_invalid_roots_duplicate_rules_and_non_cookie_accounts_are_rejected(self):
        self.save()
        with self.assertRaises(ValueError): self.save()
        with self.assertRaises(ValueError): self.state.uploads.save({'source': str(self.root), 'target': self.target})
        other = self.state.clouds.save({'provider': '115', 'credentials': {'access_token': 'example'}})
        with self.assertRaises(ValueError): self.state.uploads.save({'source': str(self.source), 'target': other['source']})

    def test_removing_account_pauses_upload_watch(self):
        watch = self.save()
        self.state.clouds.delete(self.account['id'])
        self.assertFalse(self.state.uploads.get(watch['id'])['enabled'])
    def test_delete_local_is_opt_in_and_only_after_cloud_verification(self):
        p = self.write('delete-after-verified.mp4'); watch = self.save(include_existing=True, delete_local=True)
        self.run_watch()
        self.assertFalse(p.exists())
        self.assertEqual(self.state.uploads.get(watch['id'])['last_result']['deleted'], 1)
        self.assertEqual(len(self.fake.tree['0']), 1)
    def test_delete_local_keeps_failed_or_conflicting_files(self):
        failed = self.write('failed.mp4'); conflict = self.write('conflict.mp4')
        self.fake.fail = True
        self.fake.tree['0'] = [item('old', 'conflict.mp4', size=99, sha1='OTHER')]
        self.save(include_existing=True, delete_local=True); self.run_watch()
        self.assertTrue(failed.exists()); self.assertTrue(conflict.exists())
    def test_delete_local_keeps_unverified_callback_and_retries_unlink_errors(self):
        p = self.write('keep.mp4'); watch = self.save(include_existing=True, delete_local=True)
        with patch.object(Path, 'unlink', side_effect=PermissionError('Read-only mount')):
            self.run_watch()
        self.assertTrue(p.exists())
        self.state.uploads.control(watch['id'], retry=True); self.run_watch()
        self.assertFalse(p.exists()); self.assertEqual(len(self.fake.calls), 1)
    def test_delete_local_rechecks_existing_cloud_file_before_unlink(self):
        p = self.write('remote-removed.mp4', b'same'); self.save(include_existing=True, delete_local=True)
        matching = item('same', p.name, size=4, sha1=hashlib.sha1(b'same').hexdigest())
        calls = []
        def listing(parent):
            calls.append(parent)
            return iter([matching] if len(calls) == 1 else [])
        self.fake.list = listing
        self.run_watch()
        self.assertTrue(p.exists()); self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.state.uploads.list()[0]['last_status'], 'partial')


if __name__ == '__main__': unittest.main()
