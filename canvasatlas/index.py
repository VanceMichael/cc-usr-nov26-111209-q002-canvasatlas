"""采集结果索引：页数规则、引用链追溯、素材检索。

页数只由 Canvas 身份与 Manifest.items 页序决定；Range 是可重叠的阅读分组，
painting 的 Choice 是同一目标的候选图，三者都不会产生额外页。
"""
import json
from collections import deque

from .graph import display_label
from .ledger import BLOCKED, DONE, FAILED, PENDING, CLAIMED, BAD, Ledger

INCOMPLETE_STATES = {PENDING, CLAIMED, FAILED}


class Index:
    def __init__(self, db_path):
        self.ledger = Ledger(db_path)

    def close(self):
        self.ledger.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def overview(self, job_id, language=None):
        job = self.ledger.job(job_id)
        counts = self.ledger.state_counts(job_id)
        complete = all(count == 0 for state, count in counts.items() if state != DONE)
        manifests = [
            self.manifest_view(job_id, document['uri'], language=language)
            for document in self.ledger.observed_documents(job_id)
            if document['type'] == 'Manifest'
        ]
        manifests = [view for view in manifests if view['available']]
        return {
            'job_id': job_id,
            'root_uri': job['root_uri'],
            'allowed_origins': job['allowed_origins'],
            'complete': complete,
            'counts': counts,
            'manifests': manifests,
            'blocked': [
                {'uri': row['uri'], 'reason': row['note']}
                for row in self.ledger.queue_rows(job_id)
                if row['state'] == BLOCKED
            ],
            'bad': [
                {'uri': row['uri'], 'reason': row['note']}
                for row in self.ledger.queue_rows(job_id)
                if row['state'] == BAD
            ],
        }

    def _digest(self, job_id, uri):
        return self.ledger.observed_digest(job_id, uri) or self.ledger.known_digest(uri)

    def manifest_view(self, job_id, manifest_uri, language=None):
        digest = self._digest(job_id, manifest_uri)
        document = self.ledger.document(manifest_uri, digest) if digest else None
        if document is None:
            return {
                'manifest_uri': manifest_uri,
                'available': False,
                'state': self.ledger.state_of(job_id, manifest_uri),
                'pages': [],
            }
        edges = self.ledger.refs(manifest_uri, digest)
        pages, seen_canvases = [], set()
        page_refs = [edge for edge in edges if edge['relation'] == 'item'
                     and edge['target_type'] == 'Canvas']
        for order, edge in enumerate(page_refs):
            canvas_uri = edge['target_uri']
            if canvas_uri in seen_canvases:  # 同一 Canvas 身份只占一页
                continue
            seen_canvases.add(canvas_uri)
            pages.append(self._page_view(job_id, canvas_uri, order, language))
        ranges = self._range_view(job_id, manifest_uri, digest, language)
        incomplete_refs = self._incomplete_refs(job_id, manifest_uri, digest)
        return {
            'manifest_uri': manifest_uri,
            'available': True,
            'state': DONE,
            'label': display_label(document['label'], language),
            'page_count': len(pages),
            'pages': pages,
            'ranges': ranges,
            'incomplete_references': incomplete_refs,
            'complete': not incomplete_refs and all(
                page['state'] == 'complete' or page['state'] == 'no_image'
                for page in pages
            ),
        }

    def _canvas_annotation_edges(self, job_id, canvas_uri, canvas_digest):
        """汇总 Canvas 直接内嵌与其 AnnotationPage 文档上的 painting 候选。"""
        edges = []
        for edge in self.ledger.refs(canvas_uri, canvas_digest):
            if edge['relation'] == 'annotation':
                edges.append(edge)
            elif edge['relation'] == 'annotation_page':
                page_digest = self._digest(job_id, edge['target_uri'])
                if page_digest:
                    edges.extend(self.ledger.refs(edge['target_uri'], page_digest))
        return edges

    def _page_view(self, job_id, canvas_uri, order, language):
        canvas_state = self.ledger.state_of(job_id, canvas_uri)
        label = None
        digest = self._digest(job_id, canvas_uri)
        if digest:
            canvas_doc = self.ledger.document(canvas_uri, digest)
            if canvas_doc:
                label = display_label(canvas_doc['label'], language)
        annotation_pages, candidates = [], []
        if digest:
            for edge in self.ledger.refs(canvas_uri, digest):
                if edge['relation'] == 'annotation_page':
                    page_uri = edge['target_uri']
                    page_state = self.ledger.state_of(job_id, page_uri)
                    if edge['detail'].get('embedded') and page_state == DONE:
                        page_state = 'embedded'
                    annotation_pages.append({'uri': page_uri, 'state': page_state})
            for annotation_edge in self._canvas_annotation_edges(job_id, canvas_uri, digest):
                if annotation_edge['relation'] == 'annotation':
                    candidates.extend(annotation_edge['detail'].get('candidates', []))
        if canvas_state in INCOMPLETE_STATES:
            state = 'references_pending'
        elif any(page['state'] in INCOMPLETE_STATES for page in annotation_pages):
            state = 'references_pending'
        elif candidates:
            state = 'complete'
        else:
            state = 'no_image'  # 引用都采完了，但这一页确实没有 painting 图
        return {
            'canvas_uri': canvas_uri,
            'order': order,
            'label': label,
            'state': state,
            'annotation_pages': annotation_pages,
            'image_candidates': candidates,
        }

    def _range_view(self, job_id, manifest_uri, digest, language):
        grouped = {}
        for edge in self.ledger.refs(manifest_uri, digest):
            if edge['relation'] != 'range':
                continue
            range_uri = edge['detail']['range']
            grouped.setdefault(range_uri, {
                'range_uri': range_uri,
                'label': display_label(edge['detail'].get('label', {}), language),
                'members': [],
            })
            grouped[range_uri]['members'].append({
                'uri': edge['target_uri'], 'type': edge['target_type'],
            })
        return list(grouped.values())

    def _incomplete_refs(self, job_id, manifest_uri, digest):
        """沿引用图收集仍未采完的具体引用，说明缺在哪。"""
        notes = {row['uri']: row['note'] for row in self.ledger.queue_rows(job_id)}
        missing = []
        queue = deque([(manifest_uri, digest)])
        visited = set()
        while queue:
            source_uri, source_digest = queue.popleft()
            key = (source_uri, source_digest)
            if key in visited:  # 循环引用安全
                continue
            visited.add(key)
            for edge in self.ledger.refs(source_uri, source_digest):
                target = edge['target_uri']
                if not target or edge['target_type'] not in {
                    'Collection', 'Manifest', 'Canvas', 'AnnotationPage'
                }:
                    continue
                state = self.ledger.state_of(job_id, target)
                if state in INCOMPLETE_STATES:
                    missing.append({
                        'uri': target, 'type': edge['target_type'],
                        'state': state, 'via': source_uri,
                        'reason': notes.get(target),
                    })
                target_digest = self._digest(job_id, target)
                if target_digest:
                    queue.append((target, target_digest))
        return missing

    def chain(self, job_id, uri):
        """从 Collection 根到任一资源的完整引用链与片段（含循环保护）。"""
        job = self.ledger.job(job_id)
        root = job['root_uri']
        root_digest = self.ledger.observed_digest(job_id, root)
        if root_digest is None:
            return []
        if uri == root:
            return [{'uri': root, 'relation': 'root', 'detail': {}}]
        paths = deque([[(root, root_digest, {'uri': root, 'relation': 'root', 'detail': {}})]])
        while paths:
            path = paths.popleft()
            current_uri, current_digest, _ = path[-1]
            visited = {node[0] for node in path}
            for edge in self.ledger.refs(current_uri, current_digest):
                target = edge['target_uri']
                # 图像/服务不是采集节点：命中 annotation 候选即结束，携带裁切片段。
                image_step = self._image_step(edge, uri)
                if image_step is not None:
                    return [node[2] for node in path] + [image_step]
                if not target or target in visited:  # 循环引用不回绕
                    continue
                step = {
                    'uri': target, 'relation': edge['relation'],
                    'target_type': edge['target_type'], 'detail': edge['detail'],
                }
                if target == uri:
                    return [node[2] for node in path] + [step]
                target_digest = self._digest(job_id, target)
                if target_digest:
                    paths.append(path + [(target, target_digest, step)])
        return []

    @staticmethod
    def _image_step(edge, uri):
        if edge['relation'] != 'annotation':
            return None
        for candidate in edge['detail'].get('candidates', []):
            if candidate.get('image_uri') == uri:
                return {
                    'uri': uri, 'relation': 'painting_image',
                    'target_type': 'Image',
                    'detail': {
                        'selector': candidate.get('selector'),
                        'specific_resource': candidate.get('specific_resource'),
                        'services': candidate.get('services'),
                        'canvas': edge['target_uri'],
                    },
                }
        return None

    def search(self, job_id, text, language=None):
        """素材检索：标签原文与图像/服务标识都参与匹配，返回所在页与候选。"""
        text = (text or '').lower()
        hits = []
        for document in self.ledger.observed_documents(job_id):
            if document['type'] != 'Canvas':
                continue
            label = display_label(document['label'], language)
            label_matched = text and any(
                text in value.lower() for values in document['label'].values() for value in values
            )
            candidates = []
            for edge in self._canvas_annotation_edges(job_id, document['uri'], document['digest']):
                for candidate in edge['detail'].get('candidates', []):
                    haystack = json.dumps(candidate, ensure_ascii=False).lower()
                    if not text or label_matched or text in haystack:
                        candidates.append(candidate)
            if candidates or label_matched:
                hits.append({
                    'canvas_uri': document['uri'],
                    'label': label,
                    'image_candidates': candidates,
                })
        return hits

    def stages(self, job_id):
        """实际采集阶段记录：每次抓取的状态、最终地址、尝试次序。"""
        return self.ledger.harvest_rows(job_id)
