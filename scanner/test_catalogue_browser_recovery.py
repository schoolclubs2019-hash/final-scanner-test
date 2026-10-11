"""Catalogue browser recovery preserves pagination evidence and durable ceilings."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from authority_http import AuthorityHTTP
from authority_batch import scan_one
from catalogue_discovery import FetchError, PublicHTTP, Response
from htt_publish import assert_publishable
from scanner_pipeline import pipeline_fingerprint, run_brand, SCANNER_VERSION
from scanner_store import Store
from throne_read import READER_VERSION
from throne_standard import VERSION


BASE = 'https://catalogue.test'
PAGE1 = BASE + '/products.json?limit=250&page=1'
PAGE2 = BASE + '/products.json?limit=250&page=2'


def products(count=22):
    return [{'id': number, 'handle': f'cotton-shirt-{number}',
             'title': 'Cotton shirt', 'sku': f'COT-{number}',
             'body_html': '<p>100% cotton</p>'}
            for number in range(1, count + 1)]


def response(url, rows):
    return Response(url, json.dumps({'products': rows}),
                    {'content-type': 'application/json; charset=utf-8'})


class Clock:
    def __init__(self):
        self.at = 1000
        self.sleeps = []

    def time(self):
        return self.at

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.at += seconds


class CatalogueHTTP:
    base = BASE
    interval = .01

    def __init__(self, clock, pages=None):
        self.clock, self.calls = clock, []
        self.pages = pages if pages is not None else {
            PAGE1: response(PAGE1, products()),
            PAGE2: FetchError('http_429', True, '7'),
        }

    def validate(self, url):
        if not url.startswith(BASE + '/'):
            raise FetchError('offsite_url')
        return url

    def get(self, url):
        self.validate(url)
        self.calls.append((url, self.clock.time()))
        if url in self.pages:
            value = self.pages[url]
            if isinstance(value, Exception):
                raise value
            return value
        if '/products/cotton-shirt-' in url:
            return Response(url, '<main><details><summary>Materials</summary>'
                            '<p>100% cotton</p></details></main>', {})
        raise FetchError('http_404')


class JSONRenderer:
    def __init__(self, clock, pages):
        self.clock, self.pages, self.calls = clock, pages, []

    def fetch_json(self, url):
        self.calls.append((url, self.clock.time()))
        value = self.pages[url]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def __call__(self, url):
        raise AssertionError('Catalogue recovery must never use a rendered DOM')


class OriginProbeHTTP(PublicHTTP):
    """Use real origin-resolution flow with offline transport responses."""
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.pages = {PAGE1: FetchError('blocked'), PAGE2: response(PAGE2, [])}
        super().__init__(BASE, allow_local=True, requests_per_second=100)

    def validate(self, url):
        return url if url == BASE else CatalogueHTTP.validate(self, url)

    def get(self, url):
        if url == BASE:
            self.calls.append((url, self.clock.time()))
            raise FetchError('http_429', True, '1')
        return CatalogueHTTP.get(self, url)


class CatalogueBrowserRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'canary.db')
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.brand = {'brand_id': 'browser-catalogue-proof', 'htt_id': 'htt-catalogue-proof',
                      'brand': 'Catalogue Proof', 'slug': 'catalogue-proof',
                      'domain': BASE, 'cpt': 'clothes', 'department': 'clothing',
                      'adapter': 'shopify', 'force_rescan': True}
        self.new_run()

    def new_run(self, **changes):
        self.brand = {**self.brand, **changes}
        self.store.enqueue([self.brand])
        self.rid = self.store.create(self.brand, pipeline_fingerprint(), SCANNER_VERSION,
                                     READER_VERSION, VERSION)['scan_run_id']

    def client(self, pages=None):
        raw = CatalogueHTTP(self.clock, pages)
        http = AuthorityHTTP(raw, self.store, self.rid, retry_limit=1, probe_retry_limit=1,
                             clock=self.clock.time, sleep=self.clock.sleep,
                             jitter=lambda a, b: 0)
        return raw, http

    def retry_row(self, url):
        return self.store.db.execute(
            'SELECT attempts,retry_at,state,total_429 FROM authority_retries '
            'WHERE run_id=? AND url=?', (self.rid, url)).fetchone()

    def test_selected_browser_transport_keeps_cooldown_and_complete_census(self):
        self.new_run(capture_transport='browser')
        raw, http = self.client()
        with self.store.db:
            self.store.db.execute('UPDATE authority_domains SET next_at=?', (1060,))
        class SelectedRenderer(JSONRenderer):
            def __call__(self, url):
                self.calls.append((url, self.clock.time()))
                return '<main><details><summary>Materials</summary><p>100% cotton</p></details></main>'
        renderer = SelectedRenderer(self.clock, {PAGE1: response(PAGE1, products(2)), PAGE2: response(PAGE2, [])})
        run = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assertEqual(raw.calls, [])
        self.assertTrue(all(at >= 1060 for _, at in renderer.calls))
        self.assertEqual((run['catalogue_confirmed'], run['processed']), (2, 2))
        self.assertEqual(run['canonical']['score'], 5)
        self.assertTrue(run['discovery_complete'])
        self.assertIsNotNone(self.store.page(self.rid, 'catalogue-json:' + PAGE2))
        self.assertIsNotNone(self.store.page(self.rid, 'rendered:' + BASE + '/products/cotton-shirt-1'))

    def test_selected_browser_transport_requires_renderer(self):
        self.new_run(capture_transport='browser')
        raw, http = self.client()
        with self.assertRaisesRegex(ValueError, 'requires the configured renderer'):
            run_brand(self.store, self.rid, http)
        self.assertEqual(raw.calls, [])

    def test_selected_browser_transport_resolves_origin_without_static_probe(self):
        self.new_run(capture_transport='browser')
        raw = OriginProbeHTTP(self.clock)
        class SelectedRenderer(JSONRenderer):
            def __call__(self, url):
                return '<main><details><summary>Materials</summary><p>100% cotton</p></details></main>'
        renderer = SelectedRenderer(self.clock, {PAGE1: response(PAGE1, products(1)), PAGE2: response(PAGE2, [])})
        run = run_brand(self.store, self.rid, raw, renderer=renderer)
        self.assertEqual(raw.calls, [])
        self.assertEqual(run['storefront_resolution']['crawl_origin'], BASE)
        self.assertEqual(run['canonical']['score'], 5)

    def test_selected_browser_transport_does_not_accept_partial_catalogue(self):
        self.new_run(capture_transport='browser')
        raw, http = self.client()
        renderer = JSONRenderer(self.clock, {PAGE1: response(PAGE1, products(2)), PAGE2: FetchError('http_429', True, '60')})
        run = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(run)
        self.assertEqual(raw.calls, [])

    def assert_held(self, run):
        self.assertFalse(run['discovery_complete'])
        self.assertFalse(run.get('publish_eligible'))
        self.assertFalse(run.get('fully_complete'))
        self.assertIsNone((run.get('canonical') or {}).get('score'))
        with self.assertRaises(ValueError):
            assert_publishable(run)

    def test_short_first_page_still_requires_raw_empty_second_page(self):
        raw, http = self.client()
        renderer = JSONRenderer(self.clock, {PAGE2: response(PAGE2, [])})
        run = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assertEqual(run['scanner_status'], 'SCANNER_COMPLETE', run.get('error'))
        self.assertTrue(run['discovery_complete'])
        self.assertEqual((run['catalogue_confirmed'], run['processed'], run['unaccounted']), (22, 22, 0))
        self.assertEqual(run['discovery_reconciliation'], 'PASS')
        self.assertEqual(run['canonical']['score'], 5)
        self.assertEqual([url for url, _ in raw.calls].count(PAGE2), 2)
        self.assertEqual([url for url, _ in renderer.calls], [PAGE2])
        saved = self.store.page(self.rid, 'catalogue-json:' + PAGE2)
        self.assertEqual(json.loads(saved['text']), {'products': []})
        self.assertEqual(saved['url'], PAGE2)
        self.assertEqual(self.retry_row(PAGE2)[2:], ('EXHAUSTED', 2))
        self.assertEqual(self.retry_row('rendered:' + PAGE2)[2], 'SUCCESS')
        self.assertGreaterEqual(renderer.calls[0][1], self.retry_row(PAGE2)[1])
        self.assertIsNone(run.get('authority_retry'))
        self.assertFalse(run['fully_complete'])

    def test_partial_catalogue_error_cannot_fall_through_to_other_adapter(self):
        self.new_run(adapter='auto')
        raw, http = self.client()
        renderer = JSONRenderer(self.clock, {PAGE2: Response(PAGE2, '{}', {})})
        run = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(run)
        self.assertEqual(self.store.product_count(self.rid), 22)
        self.assertTrue(all('/wp-json/' not in url and 'sitemap' not in url for url, _ in raw.calls))
        self.assertEqual(run['discovery_outcomes'][0]['emitted'], True)
        self.assertIsNone(self.store.page(self.rid, 'catalogue-json:' + PAGE2))
        self.assertIsNone(self.retry_row('rendered:' + PAGE2))

    def test_invalid_raw_payloads_never_cache_or_record_success(self):
        for text in ('0', '{}', '{"products":0}', '{"products":{}}', '[]',
                     '<html>{"products":[]}</html>', '{"products":[{}]}'):
            with self.subTest(text=text):
                self.new_run()
                _, http = self.client()
                renderer = JSONRenderer(self.clock, {PAGE2: Response(PAGE2, text, {})})
                run = run_brand(self.store, self.rid, http, renderer=renderer)
                self.assert_held(run)
                self.assertIsNone(self.store.page(self.rid, 'catalogue-json:' + PAGE2))
                self.assertIsNone(self.retry_row('rendered:' + PAGE2))
                self.assertEqual(self.retry_row(PAGE2)[2], 'EXHAUSTED')

    def test_browser_429_resume_honors_saved_deadline_and_static_exhaustion(self):
        raw, http = self.client()
        renderer = JSONRenderer(self.clock, {PAGE2: [FetchError('http_429', True, '1200'),
                                                   response(PAGE2, [])]})
        first = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(first)
        self.assertEqual(first['status'], 'DEFERRED')
        static_before = self.retry_row(PAGE2)
        browser_before = self.retry_row('rendered:' + PAGE2)
        self.assertEqual(browser_before[2:], ('BACKOFF', 1))
        call_count = len(raw.calls)
        waiting = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(waiting)
        self.assertEqual(len(renderer.calls), 1)
        self.assertEqual(len(raw.calls), call_count)
        self.clock.at = browser_before[1]
        final = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assertTrue(final['discovery_complete'], final.get('error'))
        self.assertEqual(final['processed'], 22)
        self.assertEqual([url for url, _ in raw.calls].count(PAGE1), 1)
        self.assertEqual([url for url, _ in raw.calls].count(PAGE2), 2)
        self.assertEqual(self.retry_row(PAGE2), static_before)
        self.assertEqual(self.retry_row('rendered:' + PAGE2)[2:], ('SUCCESS', 1))
        self.assertGreaterEqual(renderer.calls[-1][1], browser_before[1])

    def test_browser_retry_ceiling_persists_without_false_empty_success(self):
        _, http = self.client()
        renderer = JSONRenderer(self.clock, {PAGE2: FetchError('http_429', True, '7')})
        first = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(first)
        self.clock.at = self.retry_row('rendered:' + PAGE2)[1]
        second = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(second)
        self.assertEqual(self.retry_row('rendered:' + PAGE2)[2:], ('EXHAUSTED', 2))
        third = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(third)
        self.assertEqual(len(renderer.calls), 2)
        self.assertIsNone(self.store.page(self.rid, 'catalogue-json:' + PAGE2))

    def test_checkpointed_json_is_reused_after_interruption_before_products(self):
        raw, http = self.client()
        renderer = JSONRenderer(self.clock, {PAGE2: response(PAGE2, [])})
        with patch('scanner_pipeline.prepare_delta', side_effect=SystemExit('controlled interruption')):
            with self.assertRaises(SystemExit):
                run_brand(self.store, self.rid, http, renderer=renderer)
        self.assertTrue(self.store.run(self.rid)['discovery_complete'])
        static_before = self.retry_row(PAGE2)
        result = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assertEqual(result['processed'], 22)
        self.assertEqual([url for url, _ in renderer.calls], [PAGE2])
        self.assertEqual([url for url, _ in raw.calls].count(PAGE1), 1)
        self.assertEqual(self.retry_row(PAGE2), static_before)

    def test_interrupted_pagination_replays_verified_json_without_browser_refetch(self):
        raw, http = self.client({PAGE1: FetchError('blocked'), PAGE2: FetchError('blocked')})
        renderer = JSONRenderer(self.clock, {
            PAGE1: response(PAGE1, products()),
            PAGE2: [SystemExit('controlled interruption'), response(PAGE2, [])],
        })
        with self.assertRaises(SystemExit):
            run_brand(self.store, self.rid, http, renderer=renderer)
        self.assertFalse(self.store.run(self.rid)['discovery_complete'])
        self.assertEqual(self.store.product_count(self.rid), 22)
        cached_before = self.store.page(self.rid, 'catalogue-json:' + PAGE1)
        self.assertEqual(cached_before['provenance']['capture_source'], 'browser_http_response')
        self.assertEqual(cached_before['provenance']['requested_url'], PAGE1)
        run = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assertEqual(run['processed'], 22)
        self.assertEqual(run['duplicate_count'], 0)
        self.assertEqual(run['discovery_reconciliation'], 'PASS')
        self.assertEqual([url for url, _ in renderer.calls].count(PAGE1), 1)
        self.assertEqual([url for url, _ in raw.calls].count(PAGE1), 1)
        self.assertEqual(self.store.page(self.rid, 'catalogue-json:' + PAGE1), cached_before)

    def test_changed_json_checkpoint_is_held_before_resumed_network(self):
        raw, http = self.client({PAGE1: FetchError('blocked'), PAGE2: FetchError('blocked')})
        renderer = JSONRenderer(self.clock, {
            PAGE1: response(PAGE1, products()), PAGE2: SystemExit('controlled interruption'),
        })
        with self.assertRaises(SystemExit):
            run_brand(self.store, self.rid, http, renderer=renderer)
        key = 'catalogue-json:' + PAGE1
        cached = self.store.page(self.rid, key)
        cached['text'] = json.dumps({'products': []})
        with self.store.db:
            self.store.db.execute('UPDATE pages SET data=? WHERE run_id=? AND url=?',
                                  (json.dumps(cached), self.rid, key))
        calls_before = (len(raw.calls), len(renderer.calls))
        run = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(run)
        self.assertEqual(run['error'], 'catalogue_browser_checkpoint_mismatch')
        self.assertEqual((len(raw.calls), len(renderer.calls)), calls_before)
        self.assertEqual(self.store.product_count(self.rid), 22)

    def test_other_endpoint_response_cannot_supply_empty_termination(self):
        _, http = self.client()
        renderer = JSONRenderer(self.clock, {PAGE2: response(PAGE1, [])})
        run = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(run)
        self.assertEqual(run['error'], 'catalogue_response_identity_mismatch')
        self.assertIsNone(self.store.page(self.rid, 'catalogue-json:' + PAGE2))
        self.assertIsNone(self.retry_row('rendered:' + PAGE2))

    def test_production_entrypoint_resumes_interrupted_catalogue_browser_transition(self):
        for crash_at in ('static_exhausted', 'browser_request', 'cached_before_recovery'):
            with self.subTest(crash_at=crash_at):
                self.new_run()
                raw, http = self.client()
                values = ([SystemExit('controlled browser interruption'), response(PAGE2, [])]
                          if crash_at == 'browser_request' else response(PAGE2, []))
                renderer = JSONRenderer(self.clock, {PAGE2: values})
                original_update = self.store.update
                def update(run_id, **changes):
                    if (crash_at == 'cached_before_recovery' and
                            'catalogue_browser_recoveries' in changes):
                        raise SystemExit('controlled interruption after JSON checkpoint')
                    original_update(run_id, **changes)
                    if crash_at == 'static_exhausted' and changes.get('status') == 'FAILED':
                        raise SystemExit('controlled interruption after static exhaustion commit')
                with patch('authority_batch.PublicHTTP', return_value=http), \
                        patch('authority_batch.time.time', side_effect=self.clock.time), \
                        patch('authority_batch.time.sleep', side_effect=self.clock.sleep):
                    with patch.object(self.store, 'update', side_effect=update):
                        with self.assertRaises(SystemExit):
                            scan_one(self.store, self.brand, self.rid, renderer=renderer)
                    before = self.retry_row(PAGE2)
                    self.assertEqual(before[2:], ('EXHAUSTED', 2))
                    result = scan_one(self.store, self.brand, self.rid, renderer=renderer)
                self.assertEqual(result['scanner_status'], 'SCANNER_COMPLETE',
                                 (crash_at, result.get('status'), result.get('error')))
                self.assertEqual(result['processed'], 22)
                self.assertEqual(self.retry_row(PAGE2), before)
                self.assertEqual([url for url, _ in raw.calls].count(PAGE1), 1)
                self.assertEqual([url for url, _ in raw.calls].count(PAGE2), 2)
                expected_browser_calls = 2 if crash_at == 'browser_request' else 1
                self.assertEqual(len(renderer.calls), expected_browser_calls)

    def test_production_failed_guard_retains_unrelated_or_terminal_failures(self):
        for scenario in ('missing_marker', 'rendered_exhausted', 'no_json_renderer',
                         'completed_discovery', 'wrong_url'):
            with self.subTest(scenario=scenario):
                self.new_run()
                _, http = self.client()
                renderer = JSONRenderer(self.clock, {PAGE2: SystemExit('controlled interruption')})
                with self.assertRaises(SystemExit):
                    run_brand(self.store, self.rid, http, renderer=renderer)
                before = self.retry_row(PAGE2)
                if scenario == 'missing_marker':
                    self.store.update(self.rid, catalogue_recovery_pending=None)
                elif scenario == 'rendered_exhausted':
                    self.store.update(self.rid, authority_retry={
                        'url': 'rendered:' + PAGE2, 'state': 'EXHAUSTED'})
                elif scenario == 'no_json_renderer':
                    renderer = Mock()
                elif scenario == 'completed_discovery':
                    self.store.update(self.rid, discovery_complete=True)
                elif scenario == 'wrong_url':
                    self.store.update(self.rid, catalogue_recovery_pending={
                        'url': PAGE1, 'adapter': 'shopify'})
                with patch('authority_batch.PublicHTTP') as constructor:
                    run = scan_one(self.store, self.brand, self.rid, renderer=renderer)
                self.assertEqual(run['status'], 'FAILED')
                constructor.assert_not_called()
                self.assertEqual(self.retry_row(PAGE2), before)

    def test_second_interruption_replaying_cached_page_preserves_pending_recovery(self):
        raw, http = self.client()
        renderer = JSONRenderer(self.clock, {
            PAGE2: [SystemExit('controlled browser interruption'), response(PAGE2, [])],
        })
        with patch('authority_batch.PublicHTTP', return_value=http), \
                patch('authority_batch.time.time', side_effect=self.clock.time), \
                patch('authority_batch.time.sleep', side_effect=self.clock.sleep):
            with self.assertRaises(SystemExit):
                scan_one(self.store, self.brand, self.rid, renderer=renderer)
            before = self.retry_row(PAGE2)
            original_add = self.store.add
            def add(run_id, item):
                original_add(run_id, item)
                raise SystemExit('controlled second interruption during cached page replay')
            with patch.object(self.store, 'add', side_effect=add):
                with self.assertRaises(SystemExit):
                    scan_one(self.store, self.brand, self.rid, renderer=renderer)
            result = scan_one(self.store, self.brand, self.rid, renderer=renderer)
        self.assertEqual(result['scanner_status'], 'SCANNER_COMPLETE',
                         (result.get('status'), result.get('error')))
        self.assertEqual(result['processed'], 22)
        self.assertEqual(self.retry_row(PAGE2), before)
        self.assertEqual([url for url, _ in raw.calls].count(PAGE1), 1)
        self.assertEqual([url for url, _ in raw.calls].count(PAGE2), 2)
        self.assertEqual(len(renderer.calls), 2)

    def test_optional_exhausted_origin_probe_does_not_block_browser_crash_resume(self):
        self.new_run(authority_retry_limit=1, authority_probe_retry_limit=1)
        raw = OriginProbeHTTP(self.clock)
        renderer = JSONRenderer(self.clock, {
            PAGE1: [SystemExit('controlled browser interruption'), response(PAGE1, products())],
        })
        def durable(client, store, run_id, **policy):
            return AuthorityHTTP(client, store, run_id, **policy, clock=self.clock.time,
                                 sleep=self.clock.sleep, jitter=lambda a, b: 0)
        with patch('authority_batch.PublicHTTP', return_value=raw), \
                patch('authority_http.AuthorityHTTP', side_effect=durable), \
                patch('authority_batch.time.time', side_effect=self.clock.time), \
                patch('authority_batch.time.sleep', side_effect=self.clock.sleep):
            with self.assertRaises(SystemExit):
                scan_one(self.store, self.brand, self.rid, renderer=renderer)
            before = self.retry_row(BASE)
            self.assertEqual(before[2:], ('EXHAUSTED', 2))
            self.assertEqual(self.store.run(self.rid)['storefront_resolution']['probe_state'],
                             'authority_retry_exhausted')
            result = scan_one(self.store, self.brand, self.rid, renderer=renderer)
        self.assertEqual(result['scanner_status'], 'SCANNER_COMPLETE',
                         (result.get('status'), result.get('error')))
        self.assertEqual(result['processed'], 22)
        self.assertEqual(self.retry_row(BASE), before)
        self.assertEqual([url for url, _ in raw.calls].count(BASE), 2)

    def test_browser_budget_holds_before_missing_termination_page(self):
        self.new_run(render_page_limit=1)
        _, http = self.client({PAGE1: FetchError('blocked'), PAGE2: FetchError('blocked')})
        renderer = JSONRenderer(self.clock, {PAGE1: response(PAGE1, products()),
                                            PAGE2: response(PAGE2, [])})
        run = run_brand(self.store, self.rid, http, renderer=renderer)
        self.assert_held(run)
        self.assertEqual(run['error'], 'render_budget_exhausted')
        self.assertEqual([url for url, _ in renderer.calls], [PAGE1])
        self.assertEqual(self.store.product_count(self.rid), 22)
        self.assertIsNotNone(self.store.page(self.rid, 'catalogue-json:' + PAGE1))
        self.assertIsNone(self.store.page(self.rid, 'catalogue-json:' + PAGE2))

    def test_plain_dom_renderer_cannot_fabricate_json_recovery(self):
        _, http = self.client()
        dom_renderer = Mock(return_value='<html>{"products":[]}</html>')
        run = run_brand(self.store, self.rid, http, renderer=dom_renderer)
        self.assert_held(run)
        dom_renderer.assert_not_called()
        dom_renderer.fetch_json.assert_not_called()

    def test_old_source_fingerprint_cannot_relabel_a_saved_run(self):
        self.rid = self.store.create(self.brand, '0' * 64, SCANNER_VERSION,
                                     READER_VERSION, VERSION)['scan_run_id']
        raw, http = self.client()
        renderer = JSONRenderer(self.clock, {PAGE2: response(PAGE2, [])})
        with self.assertRaisesRegex(ValueError, 'Scanner changed'):
            run_brand(self.store, self.rid, http, renderer=renderer)
        self.assertEqual(raw.calls, [])
        self.assertEqual(renderer.calls, [])


if __name__ == '__main__':
    unittest.main()
