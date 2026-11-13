"""采集引用工作账本（SQLite，支持跨进程续行）。

队列状态：
- pending  已登记、等待抓取
- claimed  已被某个工作者领取（进程崩溃后由 reset_claims 回收）
- done     已取回并通过文档解析
- failed   本次运行抓取/解析失败，下一次运行重新排队（单资源重试）
- blocked  来源不在允许清单，永久不抓但保留受限原因
- bad      取回内容不是受支持的文档，登记为坏引用
整册完整的唯一判定：该 job 队列中所有引用均为 done。
"""
import json
import sqlite3

SCHEMA = """
create table if not exists jobs (
    id text primary key,
    root_uri text not null,
    allowed_origins text not null,
    note text,
    created_at text not null default current_timestamp
);
create table if not exists documents (
    uri text not null,
    digest text not null,
    doc_type text not null,
    label text not null,
    captured_at text not null default current_timestamp,
    primary key (uri, digest)
);
create table if not exists refs (
    source_uri text not null,
    source_digest text,
    relation text not null,
    target_uri text,
    target_type text,
    ord integer,
    detail text not null default '{}'
);
create index if not exists ref_source on refs(source_uri, source_digest);
create index if not exists ref_target on refs(target_uri);
create table if not exists queue (
    job_id text not null,
    uri text not null,
    state text not null,
    attempts integer not null default 0,
    failures integer not null default 0,
    note text,
    updated_at text not null default current_timestamp,
    primary key (job_id, uri)
) without rowid;
create table if not exists harvests (
    rowid integer primary key autoincrement,
    job_id text not null,
    uri text not null,
    capture_id integer not null,
    digest text not null,
    http_status integer not null,
    final_url text not null,
    attempt integer not null,
    at text not null default current_timestamp
);
create index if not exists harvest_job on harvests(job_id, uri);
"""

PENDING, CLAIMED, DONE, FAILED, BLOCKED, BAD = 'pending', 'claimed', 'done', 'failed', 'blocked', 'bad'
TERMINAL = {DONE, BLOCKED, BAD}


class Ledger:
    def __init__(self, database, timeout=30):
        self.connection = sqlite3.connect(database, timeout=timeout)
        self.connection.execute('pragma foreign_keys=on')
        self.connection.execute('pragma journal_mode=wal')
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def create_job(self, job_id, root_uri, allowed_origins, note=None):
        with self.connection:
            self.connection.execute(
                'insert or ignore into jobs(id, root_uri, allowed_origins, note) values(?,?,?,?)',
                (job_id, root_uri, json.dumps(sorted(allowed_origins)), note),
            )
        self.enqueue(job_id, root_uri)

    def job(self, job_id):
        row = self.connection.execute('select * from jobs where id=?', (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return {
            'id': row[0], 'root_uri': row[1], 'allowed_origins': json.loads(row[2]),
            'note': row[3], 'created_at': row[4],
        }

    def list_jobs(self):
        self.connection.row_factory = sqlite3.Row
        rows = self.connection.execute('select * from jobs order by created_at, id').fetchall()
        return [dict(row) for row in rows]

    def enqueue(self, job_id, uri, state=PENDING, note=None):
        """登记新引用；已存在的引用（含 done）不被覆盖。"""
        with self.connection:
            self.connection.execute(
                'insert or ignore into queue(job_id, uri, state, note) values(?,?,?,?)',
                (job_id, uri, state, note),
            )

    def mark_embedded_done(self, job_id, uri):
        """内嵌对象无需另取：占住 done 位置，保持完整度统计一致。"""
        with self.connection:
            self.connection.execute(
                'insert or ignore into queue(job_id, uri, state, note) values(?,?,?,?)',
                (job_id, uri, DONE, 'embedded'),
            )

    def reset_for_run(self, job_id):
        """新运行开始：回收崩溃遗留的 claimed，并重排失败引用。"""
        with self.connection:
            self.connection.execute(
                "update queue set state=? where job_id=? and state=?",
                (PENDING, job_id, CLAIMED),
            )
            self.connection.execute(
                "update queue set state=? where job_id=? and state=?",
                (PENDING, job_id, FAILED),
            )

    def claim(self, job_id):
        """原子领取一条 pending 引用；并行工作者不会领到同一条。"""
        with self.connection:
            row = self.connection.execute(
                "select uri from queue where job_id=? and state=? limit 1",
                (job_id, PENDING),
            ).fetchone()
            if row is None:
                return None
            uri = row[0]
            updated = self.connection.execute(
                "update queue set state=?, attempts=attempts+1, updated_at=current_timestamp "
                "where job_id=? and uri=? and state=?",
                (CLAIMED, job_id, uri, PENDING),
            )
            if updated.rowcount != 1:
                return None
            attempts = self.connection.execute(
                'select attempts from queue where job_id=? and uri=?', (job_id, uri)
            ).fetchone()[0]
            return uri, attempts

    def _set_state(self, job_id, uri, state, note=None):
        with self.connection:
            self.connection.execute(
                "update queue set state=?, note=?, updated_at=current_timestamp "
                "where job_id=? and uri=?",
                (state, note, job_id, uri),
            )

    def mark_done(self, job_id, uri):
        self._set_state(job_id, uri, DONE)

    def mark_failed(self, job_id, uri, reason):
        with self.connection:
            self.connection.execute(
                "update queue set state=?, failures=failures+1, note=?, updated_at=current_timestamp "
                "where job_id=? and uri=?",
                (FAILED, reason, job_id, uri),
            )

    def mark_blocked(self, job_id, uri, reason):
        self._set_state(job_id, uri, BLOCKED, reason)

    def mark_bad(self, job_id, uri, reason):
        self._set_state(job_id, uri, BAD, reason)

    def state_of(self, job_id, uri):
        row = self.connection.execute(
            'select state from queue where job_id=? and uri=?', (job_id, uri)
        ).fetchone()
        return row[0] if row else None

    def state_counts(self, job_id):
        counts = {state: 0 for state in (PENDING, CLAIMED, DONE, FAILED, BLOCKED, BAD)}
        for state, count in self.connection.execute(
            'select state, count(*) from queue where job_id=? group by state', (job_id,)
        ):
            counts[state] = count
        return counts

    def queue_rows(self, job_id):
        self.connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in self.connection.execute(
                'select * from queue where job_id=? order by uri', (job_id,)
            ).fetchall()
        ]

    def record_harvest(self, job_id, uri, capture_id, digest, http_status, final_url, attempt):
        with self.connection:
            cursor = self.connection.execute(
                'insert into harvests(job_id, uri, capture_id, digest, http_status, final_url, attempt) '
                'values(?,?,?,?,?,?,?)',
                (job_id, uri, capture_id, digest, http_status, final_url, attempt),
            )
            return cursor.lastrowid

    def observed_digest(self, job_id, uri):
        """该 job 中此 URI 最近一次成功响应所对应的原文摘要。"""
        row = self.connection.execute(
            'select digest from harvests where job_id=? and uri=? order by rowid desc limit 1',
            (job_id, uri),
        ).fetchone()
        return row[0] if row else None

    def known_digest(self, uri):
        """已登记的任意文档版本摘要（内嵌对象没有抓取历史时使用）。"""
        row = self.connection.execute(
            'select digest from documents where uri=? order by rowid desc limit 1', (uri,)
        ).fetchone()
        return row[0] if row else None

    def record_document(self, uri, digest, doc_type, label, edges):
        with self.connection:
            self.connection.execute(
                'insert or ignore into documents(uri, digest, doc_type, label) values(?,?,?,?)',
                (uri, digest, doc_type, json.dumps(label, ensure_ascii=False)),
            )
            self.connection.execute(
                'delete from refs where source_uri=? and source_digest=?', (uri, digest)
            )
            self.connection.executemany(
                'insert into refs(source_uri, source_digest, relation, target_uri, target_type, ord, detail) '
                'values(?,?,?,?,?,?,?)',
                [
                    (
                        uri, digest, edge['relation'], edge.get('target_uri'),
                        edge.get('target_type'), edge.get('order'),
                        json.dumps(edge.get('detail', {}), ensure_ascii=False),
                    )
                    for edge in edges
                ],
            )

    def document(self, uri, digest):
        row = self.connection.execute(
            'select uri, digest, doc_type, label from documents where uri=? and digest=?',
            (uri, digest),
        ).fetchone()
        if row is None:
            return None
        return {'uri': row[0], 'digest': row[1], 'type': row[2], 'label': json.loads(row[3])}

    def refs(self, source_uri, source_digest):
        self.connection.row_factory = sqlite3.Row
        rows = self.connection.execute(
            'select relation, target_uri, target_type, ord, detail from refs '
            'where source_uri=? and source_digest=? order by ord, rowid',
            (source_uri, source_digest),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['detail'] = json.loads(item['detail'])
            result.append(item)
        return result

    def reachable_uris(self, job_id):
        return [
            row[0]
            for row in self.connection.execute(
                'select uri from queue where job_id=? order by uri', (job_id,)
            ).fetchall()
        ]

    def harvest_rows(self, job_id):
        self.connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in self.connection.execute(
                'select rowid, uri, capture_id, digest, http_status, final_url, attempt, at '
                'from harvests where job_id=? order by rowid', (job_id,)
            ).fetchall()
        ]

    def observed_documents(self, job_id):
        """该 job 中每个 URI 最近一次成功响应对应的文档版本。"""
        result = []
        for uri, in self.connection.execute(
            'select distinct uri from harvests where job_id=? order by uri', (job_id,)
        ):
            digest = self.observed_digest(job_id, uri)
            document = self.document(uri, digest)
            if document is not None:
                result.append(document)
        return result
