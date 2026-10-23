import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge.server import State


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.target = self.root / 'library'; self.target.mkdir()
        self.state = State(self.root / 'data')
        self.state.config.update(source_roots=[str(self.source)], target_roots=[str(self.target)],
                                 provider='none', default_mode='path', mappings=[])
        self.options = {'source': str(self.source), 'target': str(self.target), 'kind': 'tv', 'mode': 'path'}

    def tearDown(self):
        self.state.watcher.close()
        self.state.pool.shutdown(wait=True)
        self.state.db.close()
        self.temp.cleanup()

    def file(self, name, recent=False):
        p = self.source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'file')
        if not recent:
            os.utime(p, (time.time() - 120, time.time() - 120))
        return p

    def run_watch(self):
        watch = self.state.watcher.list()[0]
        watch['next_run'] = 0
        self.state.watcher.put(watch)
        self.assertTrue(self.state.watcher.tick())
        self.state.pool.submit(lambda: None).result(timeout=10)

    def test_new_video_and_late_subtitle_are_added_without_overwrite(self):
        self.file('Show.S01E01.mkv')
        self.state.watcher.save({'options': self.options})
        with patch('bridge.resolver.rpc', side_effect=AssertionError('Folder watch must not call CD2')):
            self.run_watch()
            first = self.target / 'Show.S01E01.strm'
            original = first.stat().st_mtime_ns
            self.file('Show.S01E02.mkv')
            self.file('Other.S01E01.chs.srt')
            self.run_watch()
        self.assertEqual(first.stat().st_mtime_ns, original)
        self.assertTrue((self.target / 'Show.S01E02.strm').exists())
        self.assertTrue((self.target / 'Show.S01E01.chs.srt').exists())
        self.run_watch()
        self.assertEqual(self.state.watcher.list()[0]['last_status'], 'idle')

    def test_settings_survive_service_restart(self):
        watch = self.state.watcher.save({'options': self.options, 'interval': 600})
        self.state.pool.shutdown(wait=True); self.state.db.close()
        self.state = State(self.root / 'data')
        restored = self.state.watcher.list()[0]
        self.assertEqual(restored['id'], watch['id'])
        self.assertEqual(restored['interval'], 600)
        self.assertTrue(restored['enabled'])

    def test_recent_file_is_deferred_until_stable(self):
        video = self.file('new.mkv', recent=True)
        self.state.watcher.save({'options': self.options})
        self.run_watch()
        self.assertFalse((self.target / 'new.strm').exists())
        os.utime(video, (time.time() - 120, time.time() - 120))
        self.run_watch()
        self.assertTrue((self.target / 'new.strm').exists())

    def test_auto_watch_never_cleans_or_moves_source_files(self):
        self.file('movie.mkv'); sub = self.file('movie.srt'); junk = self.file('other.url')
        options = dict(self.options, kind='jav', cleanup=True, subtitle_mode='move')
        watch = self.state.watcher.save({'options': options})
        self.assertFalse(watch['options']['cleanup'])
        self.assertEqual(watch['options']['subtitle_mode'], 'copy')
        self.run_watch()
        self.assertTrue(sub.exists()); self.assertTrue(junk.exists())

    def test_busy_service_skips_and_paused_watch_stays_paused(self):
        watch = self.state.watcher.save({'options': self.options})
        self.state.busy = 'manual-job'
        self.assertFalse(self.state.watcher.tick())
        self.state.busy = None
        self.state.watcher.save({'id': watch['id'], 'enabled': False})
        self.assertFalse(self.state.watcher.tick())

    def test_missing_mount_records_error_without_deleting_library(self):
        self.file('movie.mkv')
        self.state.watcher.save({'options': self.options})
        self.source.rename(self.root / 'temporarily-offline')
        existing = self.target / 'existing.strm'; existing.write_text('preserve')
        self.run_watch()
        self.assertEqual(existing.read_text(), 'preserve')
        self.assertEqual(self.state.watcher.list()[0]['last_status'], 'failed')
        self.assertIsNone(self.state.busy)

    def test_duplicate_watch_and_nested_target_are_rejected(self):
        self.state.watcher.save({'options': self.options})
        with self.assertRaisesRegex(ValueError, '已有监控'):
            self.state.watcher.save({'options': self.options})


if __name__ == '__main__':
    unittest.main()
