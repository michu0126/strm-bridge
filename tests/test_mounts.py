import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.mounts import media_mounts, apply_mount_roots
from bridge.server import State


class MountTests(unittest.TestCase):
    def test_mountinfo_includes_unicode_spaces_and_excludes_app_system_files(self):
        paths = ['/', '/app/bridge', '/app/static', '/data', '/proc', '/dev',
                 '/etc/hosts', '/library/特色', r'/downloads/TV\040Shows', '/cloud/115']
        text = '\n'.join('10 1 0:40 /host '+p+' rw - btrfs /dev/disk rw' for p in paths)
        text += '\n11 1 0:41 / /tmp rw - tmpfs tmpfs rw'
        self.assertEqual(media_mounts(text, '/data'), ['/library/特色', '/downloads/TV Shows', '/cloud/115'])

    def test_keeps_configured_roots_and_adds_new_mounts_to_all_selectors(self):
        config = {'source_roots': ['/cloud'], 'target_roots': ['/library/old'], 'upload_roots': []}
        with patch('bridge.mounts.discover_media_mounts', return_value=['/library/特色', '/cloud/115']):
            apply_mount_roots(config, '/data')
            apply_mount_roots(config, '/data')
        self.assertEqual(config['source_roots'], ['/cloud', '/library/特色'])
        self.assertEqual(config['target_roots'], ['/library/old', '/library/特色', '/cloud/115'])
        self.assertIn('/cloud', config['upload_roots'])
        self.assertEqual(config['upload_roots'].count('/library/特色'), 1)

    def test_saved_config_does_not_hide_new_mount_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'source_roots': [], 'target_roots': ['/library/old'],
                      'public_url': 'http://localhost', 'mappings': []}
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            with patch('bridge.mounts.discover_media_mounts', return_value=['/library/特色']):
                state = State(folder)
            try:
                for key in ('source_roots', 'target_roots', 'upload_roots'):
                    self.assertIn('/library/特色', state.config[key])
                self.assertEqual(json.loads(path.read_text())['target_roots'], ['/library/old'])
            finally:
                state.pool.shutdown(wait=True)
                state.db.close()
