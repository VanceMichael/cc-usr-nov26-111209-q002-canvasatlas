import base64
import json
import threading
import unittest
import urllib.parse
import urllib.request

from canvasatlas.source_server import FixtureServer
from canvasatlas.transport import fetch


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def get(self, path):
        return fetch(self.base + path)

    def phase(self, value):
        self.assertEqual(self.get('/control/phase?value=' + value)[0], 200)

    def test_annotation_late_and_loop(self):
        collection = json.loads(self.get('/collection')[2])
        self.assertEqual(collection['items'][1]['id'], 'collection')
        self.assertEqual(self.get('/annotations/1')[0], 503)
        self.phase('complete')
        body = json.loads(self.get('/annotations/1')[2])['items'][0]['body']
        self.assertEqual(body['type'], 'Choice')
        self.assertEqual(body['items'][1]['selector']['value'], 'xywh=10,20,50,70')

    def test_manifest_revision(self):
        self.assertEqual(len(json.loads(self.get('/book')[2])['items']), 1)
        self.phase('updated')
        self.assertEqual(len(json.loads(self.get('/book')[2])['items']), 2)
