import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from bridge.appearance import Wallpapers, image_type, MAX_IMAGE

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1kAAAAASUVORK5CYII=')

class AppearanceTests(unittest.TestCase):
    def test_uploaded_background_survives_restart(self):
        with tempfile.TemporaryDirectory() as t:
            first=Wallpapers(t)
            self.assertEqual(first.status()['custom_url'],'')
            self.assertTrue(first.upload(PNG)['url'].startswith('/api/appearance/image/custom'))
            restored=Wallpapers(t)
            self.assertTrue(restored.status()['custom_url'])
            self.assertEqual(restored.image('custom'),(PNG,'image/png'))

    def test_rejects_non_image_and_oversized_upload(self):
        with tempfile.TemporaryDirectory() as t:
            w=Wallpapers(t)
            with self.assertRaises(ValueError):w.upload(b'<svg onload="alert(1)"></svg>')
            with self.assertRaises(ValueError):w.upload(PNG+b'0'*MAX_IMAGE)

    def test_wallpaper_path_cannot_escape_data_folder(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):Wallpapers(t).image('../config.json')

    def test_bing_wallpaper_is_cached_without_repeat_network_requests(self):
        class Response:
            def __init__(self,body):self.body=body
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return self.body
        with tempfile.TemporaryDirectory() as t:
            with patch('bridge.appearance.build_opener') as build:
                build.return_value.open.side_effect=[Response(json.dumps({'images':[{'url':'/th?id=wallpaper.jpg','copyright':'Daily photo'}]}).encode()),Response(PNG)]
                first=Wallpapers(t).bing()
                self.assertEqual(Wallpapers(t).bing(),first)
                self.assertEqual(build.return_value.open.call_count,2)
                self.assertEqual(Wallpapers(t).image(first['url'].rsplit('/',1)[-1])[1],'image/png')

if __name__=='__main__':unittest.main()
