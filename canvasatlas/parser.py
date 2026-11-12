import json


def inspect_document(body):
    value = json.loads(body)
    if not isinstance(value, dict) or value.get('type') not in {'Collection', 'Manifest', 'Canvas', 'AnnotationPage', 'Range'}:
        raise ValueError('不支持的 Presentation 文档类型')
    if not isinstance(value.get('id'), str) or not value['id']:
        raise ValueError('文档缺少 id')
    children = value.get('items', [])
    if not isinstance(children, list):
        raise ValueError('items 必须是数组')
    label = value.get('label', {})
    if not isinstance(label, dict) or any(not isinstance(entries, list) for entries in label.values()):
        raise ValueError('label 必须使用语言映射')
    return {'id': value['id'], 'type': value['type'], 'label': label,
            'items': [{'id': child.get('id'), 'type': child.get('type')} for child in children if isinstance(child, dict)],
            'structure_count': len(value.get('structures', []))}
