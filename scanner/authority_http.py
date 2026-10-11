"""Durable authority throttling. One synchronous request per domain, no scoring."""
import random
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from catalogue_discovery import FetchError


def retry_after_seconds(value, at):
    try:
        seconds = float(value)
        return max(0, seconds) if seconds >= 0 and seconds < float('inf') else None
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - at)
        except (TypeError, ValueError, OverflowError):
            return None


class AuthorityHTTP:
    def __init__(self, http, store, run_id, retry_limit=12, probe_retry_limit=1,
                 backoff_base=5, backoff_cap=300,
                 clock=time.time, sleep=time.sleep, jitter=random.uniform, budget=None):
        if (retry_limit < 0 or probe_retry_limit < 0 or probe_retry_limit > retry_limit
                or backoff_base <= 0 or backoff_cap < backoff_base):
            raise ValueError('Invalid authority retry policy')
        self.http, self.store, self.run_id = http, store, run_id
        self.base, self.validate = http.base, http.validate
        self.domain = urlsplit(http.base).hostname.removeprefix('www.')
        self.limit, self.probe_limit = retry_limit, probe_retry_limit
        self.backoff_base, self.cap = backoff_base, backoff_cap
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.budget = budget
        store.db.executescript('''
          CREATE TABLE IF NOT EXISTS authority_retries(
            run_id TEXT,url TEXT,attempts INTEGER,retry_at REAL,state TEXT,
            total_429 INTEGER NOT NULL,PRIMARY KEY(run_id,url));
          CREATE TABLE IF NOT EXISTS authority_domains(
            domain TEXT PRIMARY KEY,next_at REAL,interval REAL,concurrency INTEGER);
        ''')
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO authority_domains VALUES(?,?,?,1)',
                             (self.domain, 0, http.interval))

    def wait_until(self, deadline):
        if self.budget is not None:
            return self.budget.wait_until(deadline)
        while (remaining := deadline - self.clock()) > 0:
            self.sleep(min(30, remaining))

    def get(self, url):
        return self._get(url, self.limit)

    def probe(self, url):
        """Bound an initial adapter probe so one throttled endpoint cannot hold a brand.

        Discovery may fall through only before that adapter emits catalogue rows.
        Normal catalogue/product requests retain the full durable retry budget.
        """
        return self._get(url, self.probe_limit)

    def reserve_request(self, retry_at=0):
        """Share the durable domain deadline across static and browser channels."""
        if self.budget is not None:
            self.budget.check()
        db = self.store.db
        next_at, interval = db.execute(
            'SELECT next_at,interval FROM authority_domains WHERE domain=?',
            (self.domain,)).fetchone()
        deadline = max(next_at, retry_at)
        if self.budget is None and deadline - self.clock() > max(60, self.cap + 5):
            self.store.update(self.run_id, status='DEFERRED', worker_retry_at=deadline,
                              defer_reason='domain_backoff', error='domain_backoff')
            raise FetchError('domain_backoff', True)
        self.wait_until(deadline)
        # The worker's domain lock owns this slot for both evidence channels.
        with db:
            db.execute('UPDATE authority_domains SET next_at=? WHERE domain=?',
                       (self.clock() + interval, self.domain))
        return interval

    def record_429(self, url, exc, attempts, total, interval, retry_limit):
        attempts += 1
        total += 1
        # Retry-After is a minimum, never shortened by our cap or jitter.
        delay = retry_after_seconds(exc.retry_after, self.clock())
        if delay is None:
            delay = min(self.cap, self.backoff_base * 2 ** min(attempts - 1, 30))
        delay += self.jitter(0, min(5, delay * .1))
        retry_at = self.clock() + delay
        state = 'EXHAUSTED' if attempts > retry_limit else 'BACKOFF'
        db = self.store.db
        with db:
            db.execute('INSERT OR REPLACE INTO authority_retries VALUES(?,?,?,?,?,?)',
                       (self.run_id, url, attempts, retry_at, state, total))
            db.execute('UPDATE authority_domains SET next_at=MAX(next_at,?),interval=?,concurrency=1 WHERE domain=?',
                       (retry_at, min(60, max(interval, self.http.interval) * 2), self.domain))
        count = db.execute('SELECT SUM(total_429) FROM authority_retries WHERE run_id=?',
                           (self.run_id,)).fetchone()[0]
        self.store.update(self.run_id, status='RETRY' if state == 'BACKOFF' else 'FAILED',
                          authority_retry={'url': url, 'attempts': attempts,
                                           'retry_at': retry_at, 'state': state},
                          http_429_count=count,
                          error='http_429' if state == 'BACKOFF' else 'authority_retry_exhausted')
        return attempts, retry_at, state, total

    def render(self, url, renderer):
        """A browser is an alternate evidence channel, never a throttle bypass.

        Browser 429s are persisted separately from exhausted static requests.
        Defer after each failed browser attempt; an explicit resume observes the
        deadline and bounded retry ceiling without reusing failed HTML.
        """
        self.validate(url)
        key = 'rendered:' + url
        saved = self.store.db.execute(
            'SELECT attempts,retry_at,state,total_429 FROM authority_retries WHERE run_id=? AND url=?',
            (self.run_id, key)).fetchone()
        attempts, retry_at, state, total = saved or (0, 0, 'PENDING', 0)
        if state == 'EXHAUSTED':
            raise FetchError('render_retry_exhausted')
        if state == 'SUCCESS':
            attempts, retry_at = 0, 0
        interval = self.reserve_request(retry_at)
        try:
            html = renderer(url)
        except FetchError as exc:
            if exc.state != 'http_429':
                raise
            attempts, retry_at, state, total = self.record_429(
                key, exc, attempts, total, interval, self.probe_limit)
            if state == 'EXHAUSTED':
                raise FetchError('render_retry_exhausted') from None
            self.store.update(self.run_id, status='DEFERRED', worker_retry_at=retry_at,
                              defer_reason='domain_backoff', error='domain_backoff')
            raise FetchError('domain_backoff', True) from None
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO authority_retries VALUES(?,?,?,?,?,?)',
                                 (self.run_id, key, attempts, 0, 'SUCCESS', total))
            self.store.db.execute(
                'UPDATE authority_domains SET interval=MAX(?,interval/2.0) WHERE domain=?',
                (self.http.interval, self.domain))
        # Static exhaustion is recovered only after the caller validates the
        # rendered product evidence; do not clear that diagnostic prematurely.
        if (self.store.run(self.run_id).get('authority_retry') or {}).get('url') == key:
            self.store.update(self.run_id, status='RUNNING', authority_retry=None, error=None)
        return html

    def _get(self, url, retry_limit):
        if self.budget is not None:
            self.budget.check()
        db = self.store.db
        saved = db.execute('SELECT attempts,retry_at,state,total_429 FROM authority_retries WHERE run_id=? AND url=?',
                           (self.run_id, url)).fetchone()
        attempts, retry_at, state, total = saved or (0, 0, 'PENDING', 0)
        if state == 'EXHAUSTED':
            raise FetchError('authority_retry_exhausted')
        if state == 'SUCCESS':
            attempts, retry_at = 0, 0
        while True:
            interval = self.reserve_request(retry_at)
            try:
                response = self.http.get(url)
            except FetchError as exc:
                if exc.state != 'http_429':
                    raise
                attempts, retry_at, state, total = self.record_429(
                    url, exc, attempts, total, interval, retry_limit)
                if state == 'EXHAUSTED':
                    raise FetchError('authority_retry_exhausted') from None
                continue
            with db:
                db.execute('INSERT OR REPLACE INTO authority_retries VALUES(?,?,?,?,?,?)',
                           (self.run_id, url, attempts, 0, 'SUCCESS', total))
                db.execute(
                    'UPDATE authority_domains SET interval=MAX(?,interval/2.0) WHERE domain=?',
                    (self.http.interval, self.domain))
            if (self.store.run(self.run_id).get('authority_retry') or {}).get('url') == url:
                self.store.update(self.run_id, status='RUNNING', authority_retry=None, error=None)
            return response
