"""IIIF Presentation 3 引用图解析。

只做结构观察，不做抓取决策。输出三类信息：
- links：可继续采集的引用（Collection/Manifest/Canvas/AnnotationPage/Range）
- embedded：内嵌对象的类型，用于区分“已随文档到达”与“仍需另取”
- records：业务结构（页、painting 候选、Range 分组、语言标签、service 登记）
"""
import json
import urllib.parse

DOCUMENT_TYPES = {'Collection', 'Manifest', 'Canvas', 'AnnotationPage', 'Range'}
# 这些类型的引用才需要进入采集工作账；Range 是内嵌结构分组，Image 只登记不抓取。
FETCH_TYPES = {'Collection', 'Manifest', 'Canvas', 'AnnotationPage'}
LANGUAGE_NONE = 'none'


def resolve_uri(reference, base_url):
    """按所在文档的最终响应地址解析相对标识；去掉片段部分。"""
    resolved = urllib.parse.urljoin(base_url, reference)
    without_fragment = urllib.parse.urlsplit(resolved)._replace(fragment='').geturl()
    return without_fragment


def parse_document(body):
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('Presentation 文档必须是 JSON 对象')
    document_type = value.get('type')
    if document_type not in DOCUMENT_TYPES:
        raise ValueError('不支持的 Presentation 文档类型')
    document_id = value.get('id')
    if not isinstance(document_id, str) or not document_id:
        raise ValueError('文档缺少 id')
    label = _language_map(value.get('label'))
    return ParsedDocument(document_id, document_type, label, value)


def _language_map(value):
    value = value or {}
    if not isinstance(value, dict) or any(
        not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries)
        for entries in value.values()
    ):
        raise ValueError('label 必须使用语言映射，且每个语言值为字符串数组')
    return value


def _is_dict(value):
    return isinstance(value, dict)


def _object_list(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('items 必须是数组')
    return [entry for entry in value if _is_dict(entry)]


class ParsedDocument:
    def __init__(self, document_id, document_type, label, raw):
        self.document_id = document_id
        self.document_type = document_type
        self.label = label
        self.raw = raw

    def describe(self, base_url):
        """把一份已取回的文档展开为引用边与内嵌记录。"""
        edges = []
        embedded = []
        records = []
        if self.document_type in {'Collection', 'Manifest'}:
            for index, child in enumerate(_object_list(self.raw.get('items'))):
                edges.extend(_reference_edges(child, base_url, index, relation='item'))
                # 只有内嵌 AnnotationPage 可免另取；Canvas/Collection 仍按身份引用采集。
                if child.get('type') == 'AnnotationPage' and _is_embedded(child):
                    embedded.append({
                        'uri': resolve_uri(child['id'], base_url),
                        'type': child['type'],
                        'embedded': True,
                    })
            for structure in _object_list(self.raw.get('structures')):
                edges.extend(_reference_edges(structure, base_url, None, relation='structure'))
                if structure.get('type') == 'Range':
                    records.append(_range_record(structure, base_url))
        elif self.document_type == 'Canvas':
            for page in _object_list(self.raw.get('items')):
                edges.extend(_annotation_page_edges(page, base_url))
                # 内嵌 AnnotationPage 的 painting 记录直接归到本 Canvas 文档。
                if page.get('type') == 'AnnotationPage' and isinstance(page.get('items'), list):
                    records.extend(_annotation_records(page, base_url))
        elif self.document_type == 'AnnotationPage':
            records.extend(_annotation_records(self.raw, base_url))
        elif self.document_type == 'Range':
            records.append(_range_record(self.raw, base_url))
            for child in _object_list(self.raw.get('items')):
                edges.extend(_reference_edges(child, base_url, None, relation='structure'))
        return {'edges': edges, 'embedded': embedded, 'records': records}


def _is_embedded(node):
    """携带自身 items 的节点属于内嵌对象（可另取但不必）。"""
    return bool(node.get('items'))


def _canvas_self_contained(node):
    """Canvas 自身是否完整：内嵌 AnnotationPage 已带 annotation，无需外部补齐。"""
    for page in _object_list(node.get('items')):
        if page.get('type') == 'AnnotationPage' and isinstance(page.get('items'), list) and page['items']:
            return True
    return False


def _reference_edges(node, base_url, order, relation='item'):
    """Collection/Manifest/Range 的直接子项引用，关系由调用方按所在位置给定。"""
    node_type = node.get('type')
    node_id = node.get('id')
    if not isinstance(node_id, str) or not node_id:
        return []
    uri = resolve_uri(node_id, base_url)
    detail = {}
    # 仅当 Canvas 内嵌的 AnnotationPage 已携带 annotation 内容时，才视为无需另取的完整内嵌页；
    # 仅引用外部 AnnotationPage 的 Canvas 仍按身份独立采集。
    if node_type == 'Canvas' and _canvas_self_contained(node):
        detail['embedded_canvas'] = True
    edges = [{
        'relation': relation,
        'target_uri': uri,
        'target_type': node_type,
        'order': order,
        'detail': detail,
    }]
    # 内嵌 Canvas 仍要观察它携带的 AnnotationPage 引用。
    if node_type == 'Canvas':
        for page in _object_list(node.get('items')):
            edges.extend(_annotation_page_edges(page, base_url))
    if node_type == 'Range':
        for child in _object_list(node.get('items')):
            edges.extend(_reference_edges(child, base_url, None, relation='structure'))
    return edges


def _annotation_page_edges(page, base_url):
    page_id = page.get('id')
    if not isinstance(page_id, str) or not page_id:
        return []
    uri = resolve_uri(page_id, base_url)
    edges = [{
        'relation': 'annotation_page',
        'target_uri': uri,
        'target_type': 'AnnotationPage',
        'order': None,
        'detail': {},
    }]
    if _is_dict(page) and isinstance(page.get('items'), list) and page['items']:
        # 内嵌 AnnotationPage：内容已在本文档内，标记为不需要另取。
        edges[-1]['detail'] = {'embedded': True}
    return edges


def _range_record(node, base_url):
    members = []
    for child in _object_list(node.get('items')):
        child_id = child.get('id')
        if isinstance(child_id, str) and child_id:
            members.append({'uri': resolve_uri(child_id, base_url), 'type': child.get('type')})
    return {
        'kind': 'range',
        'uri': resolve_uri(node['id'], base_url),
        'label': _language_map(node.get('label')),
        'members': members,
    }


def _annotation_records(page, base_url):
    records = []
    for annotation in _object_list(page.get('items')):
        target = annotation.get('target')
        if not isinstance(target, str) or not target:
            continue
        record = {
            'kind': 'annotation',
            'uri': resolve_uri(annotation['id'], base_url) if isinstance(annotation.get('id'), str) else None,
            'motivation': annotation.get('motivation'),
            'target': _target_description(target, base_url),
            'candidates': [],
        }
        if annotation.get('motivation') == 'painting':
            record['candidates'] = _painting_candidates(annotation.get('body'), base_url)
        records.append(record)
    return records


def _target_description(target, base_url):
    """target 可以是 Canvas 标识，也可以是带 #xywh 的片段。"""
    parsed = urllib.parse.urlsplit(target)
    return {
        'canvas': resolve_uri(target, base_url),
        'fragment': parsed.fragment or None,
    }


def _painting_candidates(body, base_url):
    if not _is_dict(body):
        return []
    body_type = body.get('type')
    if body_type == 'Choice':
        return [_candidate(item, base_url) for item in _object_list(body.get('items'))]
    return [_candidate(body, base_url)]


def _candidate(node, base_url):
    """处理 Image 与 SpecificResource；service 只登记描述，不产生抓取引用。"""
    if node.get('type') == 'SpecificResource':
        source = node.get('source') if _is_dict(node.get('source')) else {}
        selector = node.get('selector') if _is_dict(node.get('selector')) else {}
        image_uri = source.get('id') if isinstance(source.get('id'), str) else None
        return {
            'image_uri': resolve_uri(image_uri, base_url) if image_uri else None,
            'specific_resource': True,
            'selector': {
                'type': selector.get('type'),
                'value': selector.get('value'),
            } if selector else None,
            'services': _service_descriptions(source.get('service'), base_url),
        }
    image_uri = node.get('id') if isinstance(node.get('id'), str) else None
    return {
        'image_uri': resolve_uri(image_uri, base_url) if image_uri else None,
        'specific_resource': False,
        'selector': None,
        'services': _service_descriptions(node.get('service'), base_url),
    }


def _service_descriptions(services, base_url):
    descriptions = []
    for service in _object_list(services):
        description = {'type': service.get('type'), 'profile': service.get('profile')}
        if isinstance(service.get('id'), str):
            description['id'] = resolve_uri(service['id'], base_url)
        descriptions.append(description)
    return descriptions


def display_label(label, preferred_language):
    """选定显示语言，但保留全部原文；none 不参与默认语言优先级。"""
    if not label:
        return {'display': None, 'language': None, 'all': {}}
    chosen_language = None
    if preferred_language in label:
        chosen_language = preferred_language
    elif label:
        # 明确不把 none 当默认语言：有其他自然语言时优先取排序后的第一个。
        natural = sorted(language for language in label if language != LANGUAGE_NONE)
        chosen_language = natural[0] if natural else LANGUAGE_NONE
    values = label.get(chosen_language, [])
    return {
        'display': values[0] if values else None,
        'language': chosen_language,
        'all': label,
    }
