"""Crash-safe immutable run identity, evidence checkpoints and A→Z queue."""
import fcntl
import hashlib
import json
import os
import shutil
import time
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from row_stream import RowStream


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identity(item):
    from scan_contract import canonical_url
    from urllib.parse import urlsplit
    product = item.get('product_id') or item.get('platform_product_id')
    variant = item.get('variant_id') or item.get('platform_variant_id')
    return digest([urlsplit(item['url']).hostname, item.get('market'), item.get('locale'),
                   str(product) if product else canonical_url(item['url']), str(variant or item.get('sku') or '')])


class DatabaseSchemaError(ValueError):
    """The selected file cannot safely be opened as an authority checkpoint."""


def check_schema(db):
    # CREATE TABLE IF NOT EXISTS does not validate an existing table. Check the
    # original authority columns before *any* pragma, DDL or data migration.
    required = {
        'runs': {'id', 'brand_id', 'data'},
        'products': {'run_id', 'identity', 'item', 'result', 'state', 'retryable', 'attempts'},
        'pages': {'run_id', 'url', 'data'},
        'queue': {'brand_id', 'brand', 'data', 'run_id'},
        'events': {'sequence', 'run_id', 'at', 'data'},
        'candidates': {'run_id', 'ordinal', 'identity', 'reason'},
        'changes': {'run_id', 'identity', 'state', 'old_url', 'new_url'},
        'refresh_requests': {'htt_id', 'requested_at', 'reason'},
    }
    for table, expected in required.items():
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if columns and expected - columns:
            raise DatabaseSchemaError(
                f'SCANNER DATABASE SCHEMA MISMATCH: {table} is missing '
                f'{", ".join(sorted(expected - columns))}. '
                f'Existing columns: {", ".join(sorted(columns))}. '
                'Database left unchanged. Select the original authority checkpoint '
                'or inspect its schema before migration; do not delete or reset it.')


class Store:
    def __init__(self, path, minimum_free_bytes=None):
        selected = os.environ.get('HTT_MIN_FREE_BYTES', '0') if minimum_free_bytes is None else minimum_free_bytes
        if isinstance(selected, bool) or not str(selected).isdigit():
            raise ValueError('Invalid persistent disk reserve')
        self.minimum_free_bytes = int(selected)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        try:
            check_schema(self.db)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            schema = '''
              CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS products(run_id TEXT, identity TEXT, item TEXT NOT NULL,
                result TEXT, state TEXT NOT NULL, retryable INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id,identity));
              CREATE TABLE IF NOT EXISTS pages(run_id TEXT, url TEXT, data TEXT NOT NULL, PRIMARY KEY(run_id,url));
              CREATE TABLE IF NOT EXISTS queue(brand_id TEXT PRIMARY KEY, brand TEXT NOT NULL, data TEXT NOT NULL, run_id TEXT);
              CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY, run_id TEXT, at TEXT, data TEXT);
            '''
            self.db.execute('BEGIN IMMEDIATE')
            for statement in schema.split(';'):
                if statement.strip():
                    self.db.execute(statement)

            columns = {r[1] for r in self.db.execute('PRAGMA table_info(products)')}
            for name, definition in [('discovery', 'TEXT'), ('discovery_hash', 'TEXT'),
                                     ('delta', "TEXT DEFAULT 'NEW'"), ('reused_from', 'TEXT')]:
                if name not in columns:
                    self.db.execute(f'ALTER TABLE products ADD COLUMN {name} {definition}')
            self.db.execute('UPDATE products SET discovery=item WHERE discovery IS NULL')
            self.db.execute("CREATE INDEX IF NOT EXISTS products_sku ON products(run_id,json_extract(discovery,'$.sku'))")
            self.db.execute('CREATE TABLE IF NOT EXISTS candidates(run_id TEXT, ordinal INTEGER, identity TEXT, reason TEXT, PRIMARY KEY(run_id,ordinal))')
            self.db.execute('CREATE TABLE IF NOT EXISTS changes(run_id TEXT, identity TEXT, state TEXT, old_url TEXT, new_url TEXT, PRIMARY KEY(run_id,identity))')
            self.db.execute('CREATE TABLE IF NOT EXISTS refresh_requests(htt_id TEXT PRIMARY KEY, requested_at TEXT NOT NULL, reason TEXT NOT NULL)')
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def iter_products(self, run_id):
        cursor = self.db.execute('SELECT identity,item,result,state,retryable,attempts,discovery,discovery_hash,delta,reused_from FROM products WHERE run_id=? ORDER BY identity', (run_id,))
        while batch := cursor.fetchmany(300):
            for key, item, result, state, retryable, attempts, discovery, fingerprint, delta, reused in batch:
                yield {'identity': key, 'item': json.loads(item), 'result': json.loads(result) if result else None,
                       'state': state, 'retryable': bool(retryable), 'attempts': attempts,
                       'discovery': json.loads(discovery), 'discovery_hash': fingerprint, 'delta': delta, 'reused_from': reused}

    def product_count(self, run_id):
        return self.db.execute('SELECT COUNT(*) FROM products WHERE run_id=?', (run_id,)).fetchone()[0]

    def result_rows(self, run_id):
        count = self.db.execute('SELECT COUNT(*) FROM products WHERE run_id=? AND result IS NOT NULL', (run_id,)).fetchone()[0]
        def iterate():
            cursor = self.db.execute('SELECT result FROM products WHERE run_id=? AND result IS NOT NULL ORDER BY identity', (run_id,))
            while batch := cursor.fetchmany(300):
                for (value,) in batch:
                    yield json.loads(value)
        return RowStream(iterate, count)

    def previous_snapshot(self, run):
        for (data,) in self.db.execute('SELECT data FROM runs WHERE brand_id=? AND id!=? ORDER BY rowid DESC', (run['brand_id'], run['scan_run_id'])):
            previous = json.loads(data)
            if (previous.get('scanner_status') == 'SCANNER_COMPLETE' and previous.get('publish_eligible')
                    and previous.get('mode') == 'AUTHORITY_CENSUS'
                    and all(previous.get(k) == run.get(k) for k in ('htt_id','domain','cpt','department','census_unit'))):
                return previous
        return None

    def discovery_digest(self, run_id):
        checksum = hashlib.sha256()
        for key, fingerprint in self.db.execute('SELECT identity,discovery_hash FROM products WHERE run_id=? ORDER BY identity', (run_id,)):
            checksum.update(encode([key, fingerprint]).encode())
        return checksum.hexdigest()

    def enqueue_refresh(self, htt_id, reason):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO refresh_requests VALUES(?,?,?)', (htt_id, now(), reason))

    def close(self):
        self.db.close()

    @contextmanager
    def lock(self):
        with self.path.with_suffix(self.path.suffix + '.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another worker owns this checkpoint') from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def enqueue(self, brands):
        seen = set()
        for brand in brands:
            for key in ('brand_id', 'htt_id', 'brand', 'domain', 'cpt'):
                if not brand.get(key):
                    raise ValueError('Queue requires ' + key)
            if str(brand['brand_id']) in seen:
                raise ValueError('Duplicate queue identity')
            seen.add(str(brand['brand_id']))
        with self.db:
            for brand in brands:
                self.db.execute('INSERT INTO queue VALUES(?,?,?,NULL) ON CONFLICT(brand_id) DO UPDATE SET brand=excluded.brand,data=excluded.data',
                                (str(brand['brand_id']), brand['brand'], encode(brand)))

    def queue(self):
        rows = [(json.loads(data), run_id) for data, run_id in self.db.execute('SELECT data,run_id FROM queue')]
        return sorted(rows, key=lambda row: (row[0]['brand'].casefold(), str(row[0]['brand_id'])))

    def create(self, brand, fingerprint, scanner_version, reader_version, standard_version):
        run_id = str(uuid.uuid4())
        value = {**brand, 'brand_id': str(brand['brand_id']), 'htt_id': str(brand['htt_id']), 'scan_run_id': run_id,
                 'started_at': now(), 'completed_at': None, 'scanner_fingerprint': fingerprint,
                 'scanner_version': scanner_version, 'reader_version': reader_version, 'standard_version': standard_version,
                 'mode': 'AUTHORITY_CENSUS', 'scanner_status': 'PENDING', 'publication_status': 'NOT AUTHORIZED',
                 'ecosystem_status': 'NOT TESTED', 'fully_complete': False, 'catalogue_snapshot_id': None, 'status': 'RUNNING', 'integrity_status': 'PENDING',
                 'htt_publish_status': 'NOT ATTEMPTED', 'htt_readback_status': 'NOT TESTED',
                 'idealwrld_sync_status': 'NOT ATTEMPTED', 'idealwrld_readback_status': 'NOT TESTED',
                 'app_catalogue_status': 'NOT TESTED', 'app_report_status': 'NOT TESTED', 'parity_status': 'NOT TESTED',
                 'error': None, 'discovery_complete': False}
        with self.db:
            self.db.execute('INSERT INTO runs VALUES(?,?,?)', (run_id, value['brand_id'], encode(value)))
            self.db.execute('UPDATE queue SET run_id=? WHERE brand_id=?', (run_id, value['brand_id']))
        return value

    def run(self, run_id):
        row = self.db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown scan_run_id')
        return json.loads(row[0])

    def update(self, run_id, **changes):
        immutable = {'scan_run_id', 'brand_id', 'htt_id', 'brand', 'domain', 'started_at', 'scanner_fingerprint',
                     'scanner_version', 'reader_version', 'standard_version', 'mode', 'slug', 'cpt', 'department', 'census_unit',
                     'execution_policy', 'sitemap_audit_policy'}
        if immutable.intersection(changes):
            raise ValueError('Immutable run identity cannot change')
        run = self.run(run_id)
        run.update(changes)
        with self.db:
            self.db.execute('UPDATE runs SET data=? WHERE id=?', (encode(run), run_id))
            self.db.execute('INSERT INTO events(run_id,at,data) VALUES(?,?,?)', (run_id, now(), encode(changes)))

    def add(self, run_id, item):
        self.check_capacity(len(encode(item).encode()))
        key = identity(item)
        previous = self.db.execute('SELECT item FROM products WHERE run_id=? AND identity=?', (run_id, key)).fetchone()
        with self.db:
            if previous:
                old = json.loads(previous[0])
                fields = ('composition', 'material', 'body_html', 'description', 'product_type', 'title')
                if any(old.get(f) != item.get(f) for f in fields):
                    old['_identity_conflict'] = True
                    self.db.execute("UPDATE products SET item=?,result=NULL,state='pending' WHERE run_id=? AND identity=?", (encode(old), run_id, key))
                return 'duplicate'
            # Same SKU attached to different platform products is an identity conflict.
            sku = item.get('sku')
            if sku:
                for other_key, raw in self.db.execute("SELECT identity,item FROM products WHERE run_id=? AND json_extract(discovery,'$.sku')=?", (run_id, sku)):
                    other = json.loads(raw)
                    if other.get('sku') == sku:
                        other['_identity_conflict'] = item['_identity_conflict'] = True
                        self.db.execute("UPDATE products SET item=?,result=NULL,state='pending' WHERE run_id=? AND identity=?", (encode(other), run_id, other_key))
            self.db.execute("INSERT INTO products(run_id,identity,item,state,discovery,discovery_hash) VALUES(?,?,?,'pending',?,?)", (run_id, key, encode(item), encode(item), discovery_hash(item)))
        return 'added'

    def products(self, run_id):
        # Compatibility helper for small manual callers; authority uses iter_products.
        return list(self.iter_products(run_id))

    def check_capacity(self, incoming_bytes=0):
        if self.minimum_free_bytes and shutil.disk_usage(self.path.parent).free < self.minimum_free_bytes + 2 * incoming_bytes:
            from scan_policy import ScanPaused
            raise ScanPaused('disk_pressure', time.time() + 300)

    def save_product(self, run_id, key, item, result, retryable=False):
        self.check_capacity(len(encode(item).encode()) + len(encode(result).encode()))
        with self.db:
            self.db.execute('UPDATE products SET item=?,result=?,state=?,retryable=?,attempts=attempts+1 WHERE run_id=? AND identity=?',
                            (encode(item), encode(result), result['terminal_state'], int(retryable), run_id, key))

    def save_page(self, run_id, url, response):
        self.check_capacity(len(response.text.encode()) + len(encode(response.headers).encode()))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO pages VALUES(?,?,?)',
                            (run_id, url, encode({'url': response.url, 'text': response.text, 'headers': response.headers})))

    def page(self, run_id, url):
        row = self.db.execute('SELECT data FROM pages WHERE run_id=? AND url=?', (run_id, url)).fetchone()
        return json.loads(row[0]) if row else None

    def summary(self):
        counts = Counter()
        for _, run_id in self.queue():
            status = self.run(run_id)['status'] if run_id else 'NOT_STARTED'
            category = {'HTT_PUBLISHED': 'SYNC_PENDING', 'HTT_VERIFIED': 'SYNC_PENDING', 'IW_SYNCED': 'SYNC_PENDING',
                        'SCANNER_COMPLETE': 'SYNC_PENDING', 'CALCULATED': 'SYNC_PENDING', 'REPAIR_REQUIRED': 'TECHNICAL_REPAIR'}.get(status, status)
            counts[category] += 1
        return {'TOTAL_BRANDS': len(self.queue()), **{key: counts[key] for key in
                ('COMPLETE', 'RUNNING', 'RETRY', 'TECHNICAL_REPAIR', 'SYNC_PENDING', 'FAILED', 'NOT_STARTED')}}

    def export(self, path):
        from rescore_catalogues import atomic_json
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic JSON manifest plus durable SQLite event history. No secrets/config copied.
        atomic_json(path, {'summary': self.summary(), 'runs': [json.loads(row[0]) for row in self.db.execute('SELECT data FROM runs')]})


def discovery_hash(item):
    # Transport position/capture timestamps are not product changes.
    return digest({k: v for k, v in item.items() if k not in {'source_endpoint', 'fetchedAt', 'discovery_source'}})
