import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from canvasatlas.harvester import Harvester
from canvasatlas.index import Index
from canvasatlas.source_server import FixtureServer
from canvasatlas.transport import origin_of


class HarvestCase(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.origin = origin_of(self.base)
        self.directory = tempfile.mkdtemp()
        self.db = Path(self.directory) / 'atlas.sqlite3'
        self.objects = Path(self.directory) / 'objects'

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def phase(self, value):
        urllib.request.urlopen(self.base + '/control/phase?value=' + value).read()

    def open_index(self):
        return Index(self.db)

    def harvest(self, url=None, job='job', workers=1, max_attempts=1, allowed=None):
        harvester = Harvester.open(
            job, url or self.base + '/collection', self.db, self.objects,
            allowed or {self.origin}, workers=workers, max_attempts=max_attempts,
        )
        return harvester.run()


class HarvestStageTests(HarvestCase):
    def test_initial_run_is_incomplete_with_concrete_missing_reference(self):
        report = self.harvest()
        # 阶段判断：不是数请求次数，而是检查队列工作账与失败原因。
        self.assertFalse(report['complete'])
        failed = [row for row in report['queue'] if row['state'] == 'failed']
        self.assertEqual({row['uri'] for row in failed}, {self.base + '/annotations/1'})
        self.assertTrue(all(row['note'].startswith('HTTP 503') for row in failed))
        with self.open_index() as index:
            manifest = index.manifest_view('job', self.base + '/book')
            self.assertEqual(manifest['page_count'], 1)
            self.assertFalse(manifest['complete'])
            page = manifest['pages'][0]
            self.assertEqual(page['state'], 'references_pending')
            self.assertEqual(page['annotation_pages'][0]['state'], 'failed')
            self.assertEqual(
                {ref['uri'] for ref in manifest['incomplete_references']},
                {self.base + '/annotations/1'},
            )
            # 实际采集阶段记录：503 与 200 都入档，503 不被当作“资源不存在”。
            stages = index.stages('job')
            statuses = {row['http_status'] for row in stages}
            self.assertEqual(statuses, {200, 503})
            self.assertTrue(all(row['final_url'].startswith(self.base) for row in stages))

    def test_late_annotation_page_completes_the_volume(self):
        self.harvest()
        self.phase('complete')
        # 新进程视角：重新打开账本续行，而不是依赖内存状态。
        report = Harvester('job', self.db, self.objects, {self.origin}, max_attempts=1).run()
        self.assertTrue(report['complete'])
        self.assertEqual(report['counts']['failed'], 0)
        with self.open_index() as index:
            manifest = index.manifest_view('job', self.base + '/book')
            page = manifest['pages'][0]
            self.assertEqual(page['state'], 'complete')
            self.assertTrue(manifest['complete'])
            candidates = page['image_candidates']
            self.assertEqual(len(candidates), 2)  # Choice 两个候选都保留
            front, back = candidates
            self.assertFalse(front['specific_resource'])
            self.assertTrue(back['specific_resource'])
            self.assertEqual(back['selector']['value'], 'xywh=10,20,50,70')
            self.assertEqual(front['services'][0]['type'], 'ImageService3')
            # 图像像素从不入采集队列。
            queued = {row['uri'] for row in index.ledger.queue_rows('job')}
            self.assertNotIn(self.base + '/image/front', queued)
            self.assertNotIn(self.base + '/image/back', queued)

    def test_ranges_share_canvas_without_extra_pages(self):
        self.phase('complete')
        self.harvest()
        with self.open_index() as index:
            manifest = index.manifest_view('job', self.base + '/book')
            self.assertEqual(manifest['page_count'], 1)  # 两个 Range 引用同一 Canvas
            range_uris = {range_['range_uri'] for range_ in manifest['ranges']}
            self.assertEqual(range_uris, {self.base + '/range/a', self.base + '/range/b'})
            for range_ in manifest['ranges']:
                self.assertEqual([m['uri'] for m in range_['members']],
                                 [self.base + '/canvas/1'])

    def test_collection_loop_does_not_duplicate_or_hang(self):
        report = self.harvest()
        uris = [row['uri'] for row in report['queue']]
        self.assertEqual(len(uris), len(set(uris)))
        # 循环自引用的 Collection 只登记一次。
        self.assertEqual(uris.count(self.base + '/collection'), 1)


class LanguageAndChainTests(HarvestCase):
    def test_language_preference_keeps_all_originals_and_none_is_not_default(self):
        self.phase('complete')
        self.harvest()
        with self.open_index() as index:
            page = index.manifest_view('job', self.base + '/book')['pages'][0]
            english = page['label']
            self.assertEqual(english['display'], 'Cover')
            self.assertEqual(english['language'], 'en')
            self.assertEqual(set(english['all']), {'zh', 'en', 'none'})
            chinese = index.manifest_view('job', self.base + '/book', language='zh')
            self.assertEqual(chinese['pages'][0]['label']['display'], '封面')
            # none 标签存在但不抢占自然语言显示。
            self.assertNotEqual(english['language'], 'none')

    def test_chain_traces_full_reference_path_and_fragment(self):
        self.phase('complete')
        self.harvest()
        with self.open_index() as index:
            chain = index.chain('job', self.base + '/image/back')
            relations = [hop['relation'] for hop in chain]
            self.assertEqual(relations[0], 'root')
            self.assertEqual(relations[-1], 'painting_image')
            self.assertIn('annotation_page', relations)
            self.assertIn('item', relations)
            final_hop = chain[-1]
            self.assertEqual(final_hop['detail']['selector']['value'], 'xywh=10,20,50,70')
            self.assertEqual(final_hop['detail']['canvas'], self.base + '/canvas/1')
            # 循环引用下追溯 AnnotationPage 仍能回到根。
            page_chain = index.chain('job', self.base + '/annotations/1')
            self.assertEqual(page_chain[0]['uri'], self.base + '/collection')


class BoundaryTests(HarvestCase):
    def test_foreign_origin_is_blocked_not_fetched(self):
        report = self.harvest(url=self.base + '/linked/book')
        self.assertFalse(report['complete'])
        blocked = [row for row in report['queue'] if row['state'] == 'blocked']
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]['uri'], 'http://other-origin.test/foreign/book')
        with self.open_index() as index:
            self.assertEqual(index.overview('job')['blocked'][0]['reason'],
                             '来源不在允许清单')

    def test_bad_document_is_marked_bad_and_keeps_volume_incomplete(self):
        report = self.harvest(url=self.base + '/linked/book')
        bad = [row for row in report['queue'] if row['state'] == 'bad']
        self.assertEqual([row['uri'] for row in bad], [self.base + '/broken/doc'])
        self.assertFalse(report['complete'])
        with self.open_index() as index:
            self.assertTrue(index.overview('job')['bad'][0]['reason'])

    def test_embedded_annotation_page_needs_no_separate_fetch(self):
        report = self.harvest(url=self.base + '/embedded/book')
        self.assertTrue(report['complete'])
        with self.open_index() as index:
            manifest = index.manifest_view('job', self.base + '/embedded/book')
            page = manifest['pages'][0]
            self.assertEqual(page['state'], 'complete')
            self.assertEqual(page['annotation_pages'][0]['state'], 'embedded')
            self.assertEqual(page['image_candidates'][0]['image_uri'],
                             self.base + '/image/embedded')

    def test_no_image_page_is_distinct_from_pending_reference(self):
        # updated 阶段第二页 Canvas 没有任何 painting 内容。
        self.phase('updated')
        self.harvest()
        with self.open_index() as index:
            manifest = index.manifest_view('job', self.base + '/book')
            states = {page['canvas_uri']: page['state'] for page in manifest['pages']}
            self.assertEqual(states[self.base + '/canvas/2'], 'no_image')
            self.assertEqual(states[self.base + '/canvas/1'], 'complete')
            self.assertTrue(manifest['complete'])


class RevisionTests(HarvestCase):
    def test_revisions_are_isolated_jobs_and_old_job_stays_readable(self):
        self.phase('complete')
        first = self.harvest(job='v1')
        self.assertTrue(first['complete'])
        self.phase('updated')
        second = self.harvest(job='v2')
        self.assertTrue(second['complete'])
        with self.open_index() as index:
            v1 = index.manifest_view('v1', self.base + '/book')
            v2 = index.manifest_view('v2', self.base + '/book')
            self.assertEqual(v1['page_count'], 1)  # 旧版页序不被新版候选图污染
            self.assertEqual(v2['page_count'], 2)
            self.assertEqual(v1['pages'][0]['image_candidates'][0]['image_uri'],
                             self.base + '/image/front')

    def test_partial_new_revision_reports_where_references_are_missing(self):
        # v1：initial 阶段旧版 1 页，AnnotationPage 未采完，旧批次保持自身可读状态。
        first = self.harvest(job='v1')
        self.assertFalse(first['complete'])
        # v2：来源修订到 updated（2 页且 AnnotationPage 可返回），但用受限允许清单
        # 使第二个 Canvas 所在来源被阻断，模拟“新版已部分收到但引用仍缺”。
        self.phase('updated')
        # 先正常采集 v2，再人为制造一个未采完引用并核验缺口定位。
        report = self.harvest(job='v2')
        self.assertTrue(report['complete'])
        with self.open_index() as index:
            ledger = index.ledger
            with ledger.connection:
                ledger.connection.execute(
                    "update queue set state='failed', note='模拟引用未采完' "
                    "where job_id='v2' and uri=?",
                    (self.base + '/annotations/1',),
                )
            v2 = index.manifest_view('v2', self.base + '/book')
            self.assertEqual(v2['page_count'], 2)
            pending = {ref['uri'] for ref in v2['incomplete_references']}
            self.assertEqual(pending, {self.base + '/annotations/1'})
            self.assertFalse(v2['complete'])
            # 旧批次与新批次各自独立，页序不串。
            v1 = index.manifest_view('v1', self.base + '/book')
            self.assertEqual(v1['page_count'], 1)
            self.assertFalse(v1['complete'])


class ParallelAndResumeTests(HarvestCase):
    def test_parallel_workers_claim_distinct_references(self):
        self.phase('complete')
        report = self.harvest(workers=4)
        self.assertTrue(report['complete'])
        with self.open_index() as index:
            stages = index.stages('job')
            uris = [row['uri'] for row in stages if row['http_status'] == 200]
            # 并行返回下每个 URI 只被成功处理一次。
            self.assertEqual(sorted(uris), sorted(set(uris)))
            self.assertEqual(len(index.ledger.queue_rows('job')), 4)

    def test_resume_across_processes_via_subprocess(self):
        self.harvest()
        self.phase('complete')
        project_root = Path(__file__).resolve().parents[1]
        script = (
            "import sys; sys.path.insert(0, {root!r});\n"
            "from canvasatlas.harvester import Harvester\n"
            "h = Harvester('job', {db!r}, {objects!r}, {{{origin!r}}})\n"
            "print(h.run()['complete'])\n"
        ).format(root=str(project_root), db=str(self.db),
                 objects=str(self.objects), origin=self.origin)
        result = subprocess.run(
            [sys.executable, '-I', '-c', script],
            cwd=project_root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('True', result.stdout)
        with self.open_index() as index:
            self.assertTrue(index.overview('job')['complete'])

    def test_retry_budget_accumulates_attempts_without_false_completion(self):
        report = self.harvest(max_attempts=1)
        failed = [row for row in report['queue'] if row['state'] == 'failed']
        self.assertEqual(failed[0]['attempts'], 1)
        self.assertFalse(report['complete'])
        # 下一轮运行重排失败引用，attempts 累计。
        again = Harvester('job', self.db, self.objects, {self.origin}).run()
        self.assertFalse(again['complete'])
        row = next(row for row in again['queue'] if row['state'] == 'failed')
        self.assertEqual(row['attempts'], 2)


if __name__ == '__main__':
    unittest.main()
