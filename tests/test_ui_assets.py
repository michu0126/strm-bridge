from html.parser import HTMLParser
import unittest
from unittest.mock import Mock

from bridge.server import Handler, STATIC


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        path = attrs.get('src') if tag == 'script' else attrs.get('href') if tag == 'link' else None
        if path and path.startswith('/'):
            self.paths.append(path)


class UIAssetTests(unittest.TestCase):
    def test_every_entry_page_asset_is_served_with_the_correct_content(self):
        page = Assets()
        page.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        self.assertIn('/confirmation.js', page.paths)
        for path in page.paths:
            with self.subTest(path=path):
                handler = object.__new__(Handler)
                handler.path = path
                handler.respond = Mock()
                handler.get_route()
                args, kwargs = handler.respond.call_args
                self.assertEqual(args[0], (STATIC / path[1:]).read_bytes())
                self.assertEqual(kwargs.get('code', 200), 200)
                self.assertNotIn('json', kwargs['content_type'])
