DESCRIPTION = '数字样册 IIIF Presentation 3 文档查看器、采集索引与原文档案。'
DEMO_PATH = '/book'

import argparse
import json
import tempfile
import threading
from pathlib import Path

from .archive import Archive
from .harvester import Harvester
from .index import Index
from .parser import inspect_document
from .source_server import FixtureServer
from .transport import fetch, origin_of


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)

    inspect = commands.add_parser('inspect', help='查看单个 IIIF 文档结构')
    inspect.add_argument('path')

    capture = commands.add_parser('capture', help='对本机来源发起一次原文抓取')
    capture.add_argument('url')
    capture.add_argument('--db', default='captures.sqlite3')
    capture.add_argument('--objects', default='objects')

    show = commands.add_parser('show', help='列出原文接收记录')
    show.add_argument('--db', default='captures.sqlite3')
    show.add_argument('--objects', default='objects')

    harvest = commands.add_parser('harvest', help='面向授权 Collection 的采集索引')
    harvest.add_argument('url', help='Collection 或 Manifest 入口 URL')
    harvest.add_argument('--job', required=True, help='采集批次标识（修订隔离，跨进程续行）')
    harvest.add_argument('--db', default='canvasatlas.sqlite3')
    harvest.add_argument('--objects', default='objects')
    harvest.add_argument('--allow-origin', action='append', default=[],
                         help='允许的来源 origin，可重复；默认取入口 URL 的 origin')
    harvest.add_argument('--workers', type=int, default=1)
    harvest.add_argument('--max-attempts', type=int, default=2)

    status = commands.add_parser('status', help='查看批次完整度与逐引用状态')
    status.add_argument('--job', required=True)
    status.add_argument('--db', default='canvasatlas.sqlite3')
    status.add_argument('--language', default=None)

    pages = commands.add_parser('pages', help='按 Canvas 身份与页序列出某 Manifest 的页')
    pages.add_argument('--job', required=True)
    pages.add_argument('--db', default='canvasatlas.sqlite3')
    pages.add_argument('manifest_url')
    pages.add_argument('--language', default=None)

    chain = commands.add_parser('chain', help='追溯从入口到某资源的完整引用链与片段')
    chain.add_argument('--job', required=True)
    chain.add_argument('--db', default='canvasatlas.sqlite3')
    chain.add_argument('uri')

    search = commands.add_parser('search', help='素材检索：标签原文与图像/服务标识')
    search.add_argument('--job', required=True)
    search.add_argument('--db', default='canvasatlas.sqlite3')
    search.add_argument('text')
    search.add_argument('--language', default=None)

    stages = commands.add_parser('stages', help='列出实际采集阶段（每次抓取的状态与最终地址）')
    stages.add_argument('--job', required=True)
    stages.add_argument('--db', default='canvasatlas.sqlite3')

    commands.add_parser('demo')
    return parser


def main():
    arguments = build_parser().parse_args()
    command = arguments.command
    if command == 'inspect':
        result = inspect_document(Path(arguments.path).read_bytes())
    elif command in {'capture', 'show'}:
        archive = Archive(arguments.db, arguments.objects)
        try:
            if command == 'capture':
                status, content_type, body = fetch(arguments.url)
                result = {'capture_id': archive.save(arguments.url, status, content_type, body),
                          'http_status': status}
            else:
                result = archive.rows()
        finally:
            archive.close()
    elif command == 'harvest':
        allowed = arguments.allow_origin or [origin_of(arguments.url)]
        harvester = Harvester.open(
            arguments.job, arguments.url, arguments.db, arguments.objects,
            allowed, workers=arguments.workers, max_attempts=arguments.max_attempts,
        )
        result = harvester.run()
    elif command == 'status':
        with Index(arguments.db) as index:
            result = index.overview(arguments.job, language=arguments.language)
    elif command == 'pages':
        with Index(arguments.db) as index:
            result = index.manifest_view(arguments.job, arguments.manifest_url,
                                         language=arguments.language)
    elif command == 'chain':
        with Index(arguments.db) as index:
            result = index.chain(arguments.job, arguments.uri)
    elif command == 'search':
        with Index(arguments.db) as index:
            result = index.search(arguments.job, arguments.text, language=arguments.language)
    elif command == 'stages':
        with Index(arguments.db) as index:
            result = index.stages(arguments.job)
    else:
        with FixtureServer() as server, tempfile.TemporaryDirectory() as directory:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = 'http://127.0.0.1:' + str(server.server_address[1]) + DEMO_PATH
                status, content_type, body = fetch(url)
                archive = Archive(Path(directory) / 'demo.sqlite3', Path(directory) / 'objects')
                try:
                    capture_id = archive.save(url, status, content_type, body)
                    result = {'capture_id': capture_id, 'http_status': status,
                              'observation': inspect_document(archive.read(capture_id))}
                finally:
                    archive.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)
    print_json(result)


if __name__ == '__main__':
    main()
