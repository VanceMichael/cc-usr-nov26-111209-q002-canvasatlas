import unittest
from pathlib import Path
from canvasatlas.parser import inspect_document

class ParserTests(unittest.TestCase):
    def test_manifest_structure_is_not_extra_canvas(self):
        result = inspect_document(Path('canvasatlas/fixtures/manifest.json').read_bytes())
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['structure_count'], 2)

    def test_languages(self):
        result = inspect_document(Path('canvasatlas/fixtures/canvas.json').read_bytes())
        self.assertEqual(result['label']['none'], ['I'])

    def test_unknown_document(self):
        with self.assertRaises(ValueError):
            inspect_document(b'{"type":"Image","id":"a"}')

    def test_invalid_language_value(self):
        with self.assertRaises(ValueError):
            inspect_document(b'{"type":"Manifest","id":"a","label":{"en":"wrong"}}')
