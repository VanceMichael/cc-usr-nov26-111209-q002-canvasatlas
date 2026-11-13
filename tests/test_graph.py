import unittest

from canvasatlas.graph import (
    display_label,
    parse_document,
    resolve_uri,
)


class ResolveTests(unittest.TestCase):
    def test_relative_uri_uses_final_response_url_and_drops_fragment(self):
        self.assertEqual(resolve_uri('canvas/1', 'http://127.0.0.1:9/book'),
                         'http://127.0.0.1:9/canvas/1')
        self.assertEqual(resolve_uri('http://h.test/c#xywh=1,2,3,4', 'http://h.test/base/'),
                         'http://h.test/c')

    def test_none_is_not_used_as_default_language(self):
        label = {'none': ['X'], 'en': ['Y']}
        self.assertEqual(display_label(label, None)['language'], 'en')
        only_none = display_label({'none': ['X']}, None)
        self.assertEqual(only_none['language'], 'none')
        preferred = display_label(label, 'zh')
        self.assertEqual(preferred['display'], 'Y')
        self.assertEqual(set(preferred['all']), {'none', 'en'})


class ChoiceAndSpecificResourceTests(unittest.TestCase):
    def test_choice_keeps_both_candidates_and_service_is_described_only(self):
        body = b'''{
          "id": "http://h.test/annotations/1",
          "type": "AnnotationPage",
          "items": [{
            "id": "http://h.test/painting/1",
            "type": "Annotation",
            "motivation": "painting",
            "target": "http://h.test/canvas/1#xywh=0,0,10,10",
            "body": {
              "type": "Choice",
              "items": [
                {"id": "http://h.test/image/front", "type": "Image",
                 "service": [{"id": "http://h.test/image/svc", "type": "ImageService3"}]},
                {"type": "SpecificResource",
                 "source": {"id": "http://h.test/image/back", "type": "Image"},
                 "selector": {"type": "FragmentSelector", "value": "xywh=10,20,50,70"}}
              ]
            }
          }]
        }'''
        parsed = parse_document(body)
        description = parsed.describe('http://h.test/annotations/1')
        record = next(record for record in description['records']
                      if record['kind'] == 'annotation')
        detail_target = record['target']
        self.assertEqual(detail_target['fragment'], 'xywh=0,0,10,10')
        candidates = record['candidates']
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]['services'][0]['id'], 'http://h.test/image/svc')
        self.assertTrue(candidates[1]['specific_resource'])
        self.assertEqual(candidates[1]['selector']['value'], 'xywh=10,20,50,70')
        # 服务与图像都不产生可抓取引用边。
        self.assertTrue(all(edge['target_type'] != 'Image' for edge in description['edges']))


class ManifestPageRuleTests(unittest.TestCase):
    def test_ranges_and_shared_canvas_do_not_add_pages(self):
        body = b'''{
          "id": "http://h.test/book",
          "type": "Manifest",
          "items": [
            {"id": "http://h.test/canvas/1", "type": "Canvas", "items": []},
            {"id": "http://h.test/canvas/1", "type": "Canvas", "items": []}
          ],
          "structures": [
            {"id": "http://h.test/range/a", "type": "Range",
             "items": [{"id": "http://h.test/canvas/1", "type": "Canvas"}]},
            {"id": "http://h.test/range/b", "type": "Range",
             "items": [{"id": "http://h.test/canvas/1", "type": "Canvas"}]}
          ]
        }'''
        description = parse_document(body).describe('http://h.test/book')
        canvas_items = [edge for edge in description['edges']
                        if edge['relation'] == 'item' and edge['target_type'] == 'Canvas']
        self.assertEqual([edge['order'] for edge in canvas_items], [0, 1])
        # Range 作为结构记录输出，成员都是同一个 Canvas 身份。
        range_records = [record for record in description['records']
                         if record['kind'] == 'range']
        self.assertEqual({record['uri'] for record in range_records},
                         {'http://h.test/range/a', 'http://h.test/range/b'})
        for record in range_records:
            self.assertEqual([member['uri'] for member in record['members']],
                             ['http://h.test/canvas/1'])


class CollectionLoopTests(unittest.TestCase):
    def test_self_referencing_collection_emits_single_edge(self):
        body = b'''{
          "id": "http://h.test/collection",
          "type": "Collection",
          "items": [
            {"id": "book", "type": "Manifest"},
            {"id": "collection", "type": "Collection"}
          ]
        }'''
        description = parse_document(body).describe('http://h.test/collection')
        targets = [(edge['target_uri'], edge['target_type']) for edge in description['edges']]
        self.assertEqual(targets, [
            ('http://h.test/book', 'Manifest'),
            ('http://h.test/collection', 'Collection'),
        ])


if __name__ == '__main__':
    unittest.main()
