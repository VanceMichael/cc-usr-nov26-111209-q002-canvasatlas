"""面向授权 Collection 的采集器。

每个工作线程持有自己的账本/归档连接（SQLite 连接不跨线程共享），
通过 queue 表的原子 claim 协调并行。抓取全部落在 job 配置的允许 origin 内，
图像服务只登记描述，从不下载图像像素。
"""
import json
import urllib.error
from pathlib import Path

from . import graph
from .archive import Archive
from .ledger import BLOCKED, DONE, Ledger
from .transport import OriginRestricted, origin_of, retrieve


class Harvester:
    def __init__(self, job_id, db_path, objects_path, allowed_origins, workers=1,
                 max_attempts=None, response_limit=4 * 1024 * 1024):
        self.job_id = job_id
        self.db_path = str(db_path)
        self.objects_path = str(objects_path)
        self.captures_path = str(Path(db_path).parent / (Path(db_path).stem + '-captures.sqlite3'))
        self.allowed_origins = set(allowed_origins)
        self.workers = max(1, workers)
        # 单资源尝试预算由工作账 attempts 累计跨运行记录；保留参数用于调用方配置意图。
        self.max_attempts = max_attempts
        self.response_limit = response_limit
        self._setup()

    def _setup(self):
        ledger = Ledger(self.db_path)
        try:
            job = ledger.job(self.job_id)
            self.root_uri = job['root_uri']
            self.allowed_origins = set(job['allowed_origins'])
        finally:
            ledger.close()

    @classmethod
    def open(cls, job_id, root_uri, db_path, objects_path, allowed_origins, **kwargs):
        ledger = Ledger(db_path)
        try:
            ledger.create_job(job_id, root_uri, allowed_origins)
        finally:
            ledger.close()
        return cls(job_id, db_path, objects_path, allowed_origins, **kwargs)

    def run(self):
        """执行一轮采集。可反复调用，失败引用在新一轮重新排队，账本跨进程续行。"""
        import threading

        ledger = Ledger(self.db_path)
        try:
            ledger.reset_for_run(self.job_id)
        finally:
            ledger.close()

        errors = []
        def worker():
            local_ledger = Ledger(self.db_path)
            local_archive = Archive(self.captures_path, self.objects_path)
            try:
                while True:
                    claimed = local_ledger.claim(self.job_id)
                    if claimed is None:
                        return
                    uri, attempt = claimed
                    try:
                        self._process(local_ledger, local_archive, uri, attempt)
                    except Exception as error:  # 工作者异常不能拖垮整轮
                        local_ledger.mark_failed(self.job_id, uri, f'{type(error).__name__}: {error}')
                        errors.append({'uri': uri, 'error': str(error)})
                        # 单资源重试由下一轮 run 承接（reset_for_run 重排），
                        # 本轮队列保留 failed，避免提前宣布整册完整。
            finally:
                local_ledger.close()
                local_archive.close()

        threads = [threading.Thread(target=worker, name=f'harvest-{i}') for i in range(self.workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return self.report()

    def _process(self, ledger, archive, uri, attempt):
        if origin_of(uri) not in self.allowed_origins:
            ledger.mark_blocked(self.job_id, uri, '来源不在允许清单')
            return
        try:
            response = retrieve(uri, self.allowed_origins, limit=self.response_limit)
        except OriginRestricted as error:
            ledger.mark_blocked(self.job_id, uri, str(error))
            return
        except (urllib.error.URLError, ValueError, OSError) as error:
            ledger.mark_failed(self.job_id, uri, f'{type(error).__name__}: {error}')
            return

        capture_id = archive.save(uri, response['status'], response['content_type'], response['body'])
        final_url = response['final_url']
        digest = archive.digest_for(capture_id)
        ledger.record_harvest(
            self.job_id, uri, capture_id, digest, response['status'], final_url, attempt
        )

        if response['status'] != 200:
            # HTTP 失败不证明资源不存在：保持可重试，绝不宣布缺失或完整。
            ledger.mark_failed(self.job_id, uri, f'HTTP {response["status"]}')
            return

        try:
            parsed = graph.parse_document(response['body'])
        except (ValueError, json.JSONDecodeError) as error:
            ledger.mark_bad(self.job_id, uri, str(error))
            return

        # 相对标识按所在文档的最终响应地址解析。
        description = parsed.describe(final_url)
        edges = list(description['edges'])
        edges.extend(self._record_edges(parsed, description['records'], final_url))
        ledger.record_document(uri, digest, parsed.document_type, parsed.label, edges)

        self._record_embedded_documents(ledger, parsed.raw, final_url, digest)

        for edge in edges:
            target = edge.get('target_uri')
            target_type = edge.get('target_type')
            if not target or target_type not in graph.FETCH_TYPES:
                continue
            if origin_of(target) not in self.allowed_origins:
                ledger.enqueue(self.job_id, target, state=BLOCKED, note='来源不在允许清单')
                continue
            if edge.get('detail', {}).get('embedded') or edge.get('detail', {}).get('embedded_canvas'):
                ledger.mark_embedded_done(self.job_id, target)
            else:
                ledger.enqueue(self.job_id, target)
        for embedded in description['embedded']:
            if embedded['embedded']:
                ledger.mark_embedded_done(self.job_id, embedded['uri'])
        ledger.mark_done(self.job_id, uri)

    def _record_embedded_documents(self, ledger, raw, base_url, parent_digest):
        """内嵌完整 Canvas 作为独立文档版本登记，来源摘要指向承载文档。"""
        for node in self._iter_embedded(raw):
            if node.get('type') != 'Canvas' or not isinstance(node.get('id'), str):
                continue
            parsed_canvas = graph.parse_document(json.dumps(node).encode())
            canvas_uri = graph.resolve_uri(node['id'], base_url)
            description = parsed_canvas.describe(base_url)
            ledger.record_document(
                canvas_uri, parent_digest, 'Canvas', parsed_canvas.label,
                description['edges'] + self._record_edges(parsed_canvas, description['records'], base_url),
            )
            for embedded in description['embedded']:
                if embedded['embedded']:
                    ledger.mark_embedded_done(self.job_id, embedded['uri'])
            for edge in description['edges']:
                target = edge.get('target_uri')
                if target and edge.get('detail', {}).get('embedded'):
                    ledger.mark_embedded_done(self.job_id, target)

    @staticmethod
    def _iter_embedded(raw):
        for child in raw.get('items', []) if isinstance(raw, dict) else []:
            if (isinstance(child, dict) and child.get('type') == 'Canvas'
                    and graph._canvas_self_contained(child)):
                yield child

    def _record_edges(self, parsed, records, base_url):
        edges = []
        for record in records:
            if record['kind'] == 'range':
                for member in record['members']:
                    edges.append({
                        'relation': 'range',
                        'target_uri': member['uri'],
                        'target_type': member['type'],
                        'order': None,
                        'detail': {'range': record['uri'],
                                   'label': record['label']},
                    })
            elif record['kind'] == 'annotation':
                edges.append({
                    'relation': 'annotation',
                    'target_uri': record['target']['canvas'],
                    'target_type': 'Canvas',
                    'order': None,
                    'detail': {
                        'annotation': record['uri'],
                        'motivation': record['motivation'],
                        'fragment': record['target']['fragment'],
                        'candidates': record['candidates'],
                    },
                })
        return edges

    def report(self):
        ledger = Ledger(self.db_path)
        try:
            counts = ledger.state_counts(self.job_id)
            complete = all(count == 0 for state, count in counts.items() if state != DONE)
            return {
                'job_id': self.job_id,
                'complete': complete,
                'counts': counts,
                'queue': ledger.queue_rows(self.job_id),
            }
        finally:
            ledger.close()
