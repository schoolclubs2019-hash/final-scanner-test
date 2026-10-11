"""Offline reproduction of sitemap product-page exhaustion and durable recovery."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

import catalogue_discovery
from authority_http import AuthorityHTTP
from catalogue_discovery import FetchError, PublicHTTP, Response
from confirmed_product import confirm_product
from htt_publish import assert_publishable
from scanner_pipeline import run_brand, recover_rendered_confirmation, pipeline_fingerprint, SCANNER_VERSION
from scanner_store import Store
from throne_read import READER_VERSION
from throne_standard import VERSION
from test_authority_batch import brand
from test_operational_canary import Clock

URL = 'https://brand.test/products/cotton-shirt'


def product_html(material='100% cotton'):
    obj = {'@type': 'Product', 'url': URL, 'name': 'Cotton shirt', 'sku': 'COT-1'}
    if material:
        obj['material'] = material
    return '<script type="application/ld+json">' + json.dumps(obj) + '</script>'


class SitemapHTTP:
    base = 'https://brand.test'
    interval = .01

    def __init__(self, state='http_429', url=URL):
        self.calls = []
        self.state, self.url = state, url
        self.validate = PublicHTTP(self.base).validate

    def get(self, url):
        self.validate(url)
        self.calls.append(url)
        if url.endswith('/sitemap.xml'):
            return Response(url, '<sitemapindex><sitemap><loc>' + self.base +
                            '/catalogue.xml</loc></sitemap></sitemapindex>', {})
        if url.endswith('/catalogue.xml'):
            return Response(url, '<urlset><url><loc>' + escape(self.url) + '</loc></url></urlset>', {})
        if url == self.url:
            raise FetchError(self.state, self.state == 'http_429')
        raise FetchError('http_404')


class ProductSitemapHTTP(SitemapHTTP):
    """Shopify-shaped explicit product sitemap used by the production regression."""

    def get(self, url):
        self.validate(url)
        self.calls.append(url)
        if url.endswith('/sitemap.xml'):
            return Response(url, '<sitemapindex><sitemap><loc>' + self.base +
                            '/sitemap_products_1.xml?from=1&amp;to=2</loc></sitemap></sitemapindex>', {})
        if '/sitemap_products_1.xml' in url:
            return Response(url, '<urlset><url><loc>' + escape(self.url) + '</loc></url></urlset>', {})
        if url == self.url:
            raise FetchError(self.state, self.state == 'http_429')
        raise FetchError('http_404')


def schema_free_material_html():
    return ('<html><head><meta property="og:type" content="product">'
            '<meta property="og:url" content="' + URL + '">'
            '<meta property="product:retailer_item_id" content="COT-1">'
            '<meta property="og:title" content="Cotton shirt"></head>'
            '<body><main><details><summary>Materials</summary>'
            '<p>100% cotton</p></details></main></body></html>')


class RenderedRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.dns = patch('catalogue_discovery.socket.getaddrinfo',
                         return_value=[(2, 1, 6, '', ('93.184.216.34', 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'scan.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.record = {**brand('42', 'Fixture textiles', 'https://brand.test'), 'adapter': 'sitemap'}
        self.store.enqueue([self.record])
        self.new_run()
        self.clock = Clock()
        self.render = Mock(return_value=product_html())

    def new_run(self):
        self.rid = self.store.create(self.record, pipeline_fingerprint(), SCANNER_VERSION,
                                     READER_VERSION, VERSION)['scan_run_id']

    def client(self, state='http_429', url=URL):
        raw = SitemapHTTP(state, url)
        http = AuthorityHTTP(raw, self.store, self.rid, clock=self.clock.time,
                             sleep=self.clock.sleep, jitter=lambda a, b: 0)
        return raw, http

    def assert_held(self, run):
        self.assertFalse(run.get('publish_eligible'))
        self.assertIsNone((run.get('canonical') or {}).get('score'))
        with self.assertRaises(ValueError):
            assert_publishable(run)

    def test_product_sitemap_identity_is_tightly_scoped(self):
        trusted = {
            'url': URL,
            'catalogue_source_url': 'https://brand.test/sitemap_products_1.xml?from=1&to=2',
            'discovery_source': 'product_sitemap',
            'product_sitemap_identity': True,
        }
        self.assertFalse(confirm_product(trusted)[0])
        for mutation in (
                {'discovery_source': 'sitemap'},
                {'product_sitemap_identity': False},
                {'catalogue_source_url': 'https://brand.test/sitemap.xml'},
                {'catalogue_source_url': 'https://other.test/sitemap_products_1.xml'},
                {'url': 'https://brand.test/pages/cotton-shirt'}):
            with self.subTest(mutation=mutation):
                accepted, _ = confirm_product({**trusted, **mutation})
                self.assertFalse(accepted)

    def test_explicit_product_sitemap_identity_recovers_schema_free_render(self):
        # Production-shaped BRANWYN path: the explicit Shopify product sitemap
        # supplies provenance, the static product page exhausts 429s, and the
        # browser supplies explicit product metadata plus composition.
        self.new_run()
        raw = ProductSitemapHTTP()
        http = AuthorityHTTP(raw, self.store, self.rid, clock=self.clock.time,
                             sleep=self.clock.sleep, jitter=lambda a, b: 0)
        renderer = Mock(return_value=schema_free_material_html())

        result = run_brand(self.store, self.rid, http, renderer)

        renderer.assert_called_once_with(URL)
        self.assertEqual(raw.calls.count(URL), 13)
        self.assertEqual(self.store.db.execute(
            'SELECT attempts,state,total_429 FROM authority_retries WHERE run_id=? AND url=?',
            (self.rid, URL)).fetchone(), (13, 'EXHAUSTED', 13))
        self.assertEqual(result['http_429_count'], 13)
        self.assertTrue(result['discovery_complete'])
        self.assertEqual(result['discovery_reconciliation'], 'PASS')
        self.assertEqual((result['catalogue_confirmed'], result['processed'], result['unaccounted']), (1, 1, 0))
        self.assertEqual(result['technical_failures'], 0)
        self.assertEqual(result['integrity_status'], 'PASS')
        self.assertTrue(result['publish_eligible'])
        self.assertEqual(result['canonical']['score'], 5)
        self.assertEqual(result['status'], 'SCANNER_COMPLETE')
        self.assertEqual(result['scanner_status'], 'SCANNER_COMPLETE')
        self.assertIsNone(result['error'])
        self.assertIsNone(result.get('authority_retry'))
        self.assertEqual(result['rendered_recoveries'][URL]['state'], 'EXHAUSTED')
        item = self.store.products(self.rid)[0]['item']
        self.assertEqual(item['sku'], 'COT-1')
        self.assertEqual(item['discovery_source'], 'product_sitemap')
        self.assertTrue(item['product_sitemap_identity'])
        self.assertIn('sitemap_products_1.xml', item['catalogue_source_url'])
        self.assertEqual(item['source_endpoint'], URL)
        self.assertEqual(item['confirmation_source'], 'rendered_html')
        self.assertEqual(item['response_sha256'],
                         hashlib.sha256(schema_free_material_html().encode()).hexdigest())
        assert_publishable(result)

    def test_named_product_sitemap_without_page_identity_remains_held(self):
        raw = ProductSitemapHTTP()
        http = AuthorityHTTP(raw, self.store, self.rid, clock=self.clock.time,
                             sleep=self.clock.sleep, jitter=lambda a, b: 0)
        renderer = Mock(return_value='<main><details><summary>Materials</summary>100% cotton</details></main>')
        result = run_brand(self.store, self.rid, http, renderer)
        self.assert_held(result)
        self.assertFalse(result['discovery_complete'])
        self.assertEqual(result['error'], 'rendered_product_identity_unresolved')
        self.assertEqual(result['authority_retry']['state'], 'EXHAUSTED')
        self.assertEqual(result['http_429_count'], 13)
        renderer.assert_called_once_with(URL)

    def test_exact_page_theme_json_confirms_real_platform_identity(self):
        from catalogue_discovery import confirm_candidate
        def html(obj):
            return '<script type="application/json" data-product-json>' + json.dumps(obj) + '</script>'
        obj = {'id': 1234, 'title': 'Cotton shirt', 'url': URL}
        item, accepted, _ = confirm_candidate({'url': URL}, Response(URL, html(obj), {}))
        self.assertTrue(accepted)
        self.assertEqual(item['platform_product_id'], '1234')
        for mutation in ({'url': 'https://other.test/products/cotton-shirt'}, {'url': '/products/another'}, {'url': None}):
            self.assertFalse(confirm_candidate({'url': URL}, Response(URL, html({**obj, **mutation}), {}))[1])
        for bad in ({**obj, 'id': True}, {**obj, 'id': 0}, {**obj, 'title': []}, [obj]):
            with self.assertRaisesRegex(FetchError, 'malformed_product_identity'):
                confirm_candidate({'url': URL}, Response(URL, html(bad), {}), self.render)
        with self.assertRaisesRegex(FetchError, 'ambiguous_product_identity'):
            confirm_candidate({'url': URL}, Response(URL, html(obj) + html({**obj, 'id': 2345}), {}), self.render)
        self.render.assert_not_called()

    def test_real_browser_recovers_exhaustion_with_theme_json_and_material_panel(self):
        from test_pipeline import FixtureServer
        from rendered_evidence import BrowserRenderer
        self.dns.stop()  # The real browser/HTTP fixture uses loopback DNS.
        with FixtureServer() as server:
            record = {**self.record, 'domain': server.base}
            rid = self.store.create(record, pipeline_fingerprint(), SCANNER_VERSION,
                                    READER_VERSION, VERSION)['scan_run_id']
            url = server.base + '/products/p1'
            handler = server.server.RequestHandlerClass
            original = handler.do_GET
            def serve(request):
                if request.path == '/products/p1':
                    obj = {'id': 1234, 'title': 'Cotton shirt', 'url': url}
                    request.send('<script type="application/json" data-product-json>' + json.dumps(obj) +
                                 '</script><main><details><summary>Materials</summary>100% cotton</details></main>')
                else:
                    original(request)
            raw = server.http()
            original_get = raw.get
            calls = []
            def static_get(target):
                calls.append(target)
                if target == url:
                    raise FetchError('http_429', True)
                return original_get(target)
            http = AuthorityHTTP(raw, self.store, rid, clock=self.clock.time,
                                 sleep=self.clock.sleep, jitter=lambda a, b: 0)
            browser = Mock(wraps=BrowserRenderer(allow_local=True))
            with patch.object(handler, 'do_GET', serve), patch.object(raw, 'get', side_effect=static_get):
                result = run_brand(self.store, rid, http, browser)
            self.assertTrue(result.get('publish_eligible'), result.get('error'))
            self.assertEqual(result['canonical']['score'], 5)
            self.assertEqual(result['http_429_count'], 13)
            self.assertEqual(calls.count(url), 13)
            browser.assert_called_once_with(url)
            self.assertEqual(self.store.products(rid)[0]['item']['platform_product_id'], '1234')

    def test_exhausted_static_uses_same_render_for_confirmation_and_processing(self):
        raw, http = self.client()
        with patch('catalogue_discovery.is_challenge_page', wraps=catalogue_discovery.is_challenge_page) as challenge:
            result = run_brand(self.store, self.rid, http, self.render)
        challenge.assert_any_call(product_html(), URL)
        self.render.assert_called_once_with(URL)
        self.assertEqual(raw.calls.count(URL), 13)
        self.assertEqual(self.store.db.execute(
            'SELECT attempts,state,total_429 FROM authority_retries WHERE run_id=? AND url=?',
            (self.rid, URL)).fetchone(), (13, 'EXHAUSTED', 13))
        self.assertEqual(result['http_429_count'], 13)
        self.assertEqual(result['rendered_recoveries'][URL]['state'], 'EXHAUSTED')
        self.assertIsNone(result.get('authority_retry'))
        self.assertIsNone(result['error'])
        self.assertEqual(self.store.page(self.rid, 'rendered:' + URL)['text'], product_html())
        self.assertTrue(result['discovery_complete'])
        self.assertEqual(result['discovery_reconciliation'], 'PASS')
        self.assertEqual((result['catalogue_confirmed'], result['processed'], result['unaccounted']), (1, 1, 0))
        self.assertEqual(result['technical_failures'], 0)
        self.assertEqual(result['integrity_status'], 'PASS')
        self.assertTrue(result['publish_eligible'])
        self.assertEqual(result['canonical']['score'], 5)
        self.assertEqual(result['status'], 'SCANNER_COMPLETE')
        self.assertEqual(result['scanner_status'], 'SCANNER_COMPLETE')
        self.assertEqual(result['publication_blockers'], [])
        assert_publishable(result)
        item = self.store.products(self.rid)[0]['item']
        self.assertEqual(item['confirmation_source'], 'rendered_html')
        self.assertEqual(item['response_sha256'], hashlib.sha256(product_html().encode()).hexdigest())
        with self.assertRaisesRegex(FetchError, 'authority_retry_exhausted'):
            http.get(URL)
        self.assertEqual(raw.calls.count(URL), 13)

    def test_blocked_and_direct_429_alternate_channels(self):
        for state in ('blocked', 'http_429'):
            with self.subTest(state=state):
                self.new_run()
                raw = SitemapHTTP(state)
                renderer = Mock(return_value=product_html())
                result = run_brand(self.store, self.rid, raw, renderer)
                self.assertTrue(result['publish_eligible'])
                renderer.assert_called_once_with(URL)
                self.assertEqual(raw.calls.count(URL), 1)

    def test_challenge_and_unconfirmed_html_remain_held(self):
        for html in ('<title>Just a moment</title>Verify you are human',
                     '<title>Just a moment</title>' + product_html(), '<main>Cotton shirt</main>',
                     '<script type="application/ld+json">{"@type":"Product", invalid}</script>'):
            with self.subTest(html=html):
                self.new_run()
                raw, http = self.client()
                renderer = Mock(return_value=html)
                result = run_brand(self.store, self.rid, http, renderer)
                self.assert_held(result)
                self.assertFalse(result['discovery_complete'])
                self.assertEqual(self.store.product_count(self.rid), 0)
                renderer.assert_called_once_with(URL)
                self.assertEqual(result['http_429_count'], 13)

    def test_identity_only_render_is_technical_never_fake_undisclosed(self):
        raw, http = self.client()
        self.render.return_value = product_html(material=None)
        result = run_brand(self.store, self.rid, http, self.render)
        self.assert_held(result)
        self.assertTrue(result['discovery_complete'])
        self.assertEqual((result['catalogue_confirmed'], result['processed']), (1, 1))
        self.assertEqual(result['technical_failures'], 1)
        self.assertEqual(result['undisclosed'], 0)
        self.assertEqual(self.store.products(self.rid)[0]['item']['fetch_status'], 'extractor_unresolved')
        self.render.assert_called_once_with(URL)
        self.assertEqual(raw.calls.count(URL), 13)

    def test_resume_after_confirmation_or_before_product_commit_reuses_cache(self):
        for interruption in ('add', 'save_product'):
            with self.subTest(interruption=interruption):
                self.new_run()
                raw, http = self.client()
                renderer = Mock(return_value=product_html())
                with patch.object(self.store, interruption, side_effect=SystemExit('interrupt')):
                    with self.assertRaises(SystemExit):
                        run_brand(self.store, self.rid, http, renderer)
                checkpoint = self.store.run(self.rid)
                self.assertEqual(checkpoint['status'], 'RUNNING')
                self.assertIsNone(checkpoint['error'])
                self.assertEqual(checkpoint['rendered_recoveries'][URL]['state'], 'EXHAUSTED')
                if interruption == 'save_product':
                    self.assertEqual(self.store.db.execute('SELECT state,retryable,result FROM products WHERE run_id=?',
                                                          (self.rid,)).fetchone(), ('pending', 0, None))
                self.store.close()
                self.store = Store(self.path)
                resumed_raw, resumed_http = self.client()
                result = run_brand(self.store, self.rid, resumed_http, renderer, retry=True)
                self.assertTrue(result['publish_eligible'])
                self.assertNotIn(URL, resumed_raw.calls)
                renderer.assert_called_once_with(URL)
                self.assertEqual(result['http_429_count'], 13)

    def test_no_renderer_and_non_detail_candidates_do_not_recover(self):
        for url, renderer in ((URL, None), ('https://brand.test/about', self.render)):
            with self.subTest(url=url):
                self.new_run()
                raw, http = self.client(url=url)
                result = run_brand(self.store, self.rid, http, renderer)
                self.assert_held(result)
                self.assertFalse(result['discovery_complete'])
                self.render.assert_not_called()

    def test_forbidden_failure_classes_never_render(self):
        for state in ('http_404', 'http_410', 'unsafe_url', 'offsite_url', 'unsafe_address',
                      'ambiguous_product_identity', 'malformed_product_identity', 'security_failure',
                      'malformed_sitemap', 'catalogue_corruption'):
            with self.subTest(state=state):
                self.new_run()
                raw, http = self.client(state)
                result = run_brand(self.store, self.rid, http, self.render)
                self.assert_held(result)
                self.render.assert_not_called()
                if state in {'http_404', 'http_410', 'ambiguous_product_identity'}:
                    self.assertTrue(result['discovery_complete'])
                    self.assertEqual(result['discovery_rejected'], {state: 1})
                    self.assertEqual(result['discovery_reconciliation'], 'PASS')
                else:
                    self.assertFalse(result['discovery_complete'])

    def test_actual_url_security_validation_precedes_rendering(self):
        for url in ('https://other.test/products/shirt', 'https://user:password@brand.test/products/shirt',
                    'file:///products/shirt', 'https://127.0.0.1/products/shirt'):
            with self.subTest(url=url):
                self.new_run()
                raw, http = self.client(url=url)
                result = run_brand(self.store, self.rid, http, self.render)
                self.assert_held(result)
                self.render.assert_not_called()
        self.new_run()
        raw, http = self.client()
        # Switch DNS to a private address before product confirmation.
        with patch('catalogue_discovery.socket.getaddrinfo', side_effect=lambda *a: (
                [(2, 1, 6, '', ('127.0.0.1', 443))] if raw.calls and raw.calls[-1].endswith('catalogue.xml')
                else [(2, 1, 6, '', ('93.184.216.34', 443))])):
            result = run_brand(self.store, self.rid, http, self.render)
        self.assertEqual(result['error'], 'unsafe_address')
        self.render.assert_not_called()

    def test_scoped_recovery_never_clears_unrelated_failure(self):
        for error, url, status in (('other_failure', URL, 'FAILED'),
                                   ('authority_retry_exhausted', URL + '-other', 'FAILED'),
                                   ('authority_retry_exhausted', URL, 'REPAIR_REQUIRED')):
            self.store.update(self.rid, error=error, status=status,
                              authority_retry={'url': url, 'state': 'EXHAUSTED'})
            before = self.store.run(self.rid)
            recover_rendered_confirmation(self.store, self.rid, URL)
            self.assertEqual(self.store.run(self.rid), before)

    def test_canary_diagnostics_are_allowlisted_before_preflight_error(self):
        import production_worker
        state = Path(self.temp.name) / 'canary'
        config = Path(self.temp.name) / 'config.json'
        config.write_text('{}')
        run = {'status': 'FAILED', 'error': 'authority_retry_exhausted',
               'scanner_status': 'PENDING', 'canonical': {'score': None, 'secret': 'CANONICAL_SECRET'},
               'config': 'CONFIG_SECRET', 'api_key': 'API_SECRET', 'headers': 'HEADER_SECRET'}
        def failed_scan(store, record, *_args, **_kwargs):
            saved = store.create(record, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)
            store.update(saved['scan_run_id'], **run)
            return store.run(saved['scan_run_id'])
        out = io.StringIO()
        with patch('sys.argv', ['production_worker.py', 'canary', '--queue', 'unused.json', '--state', str(state),
                                '--config', str(config), '--brand-id', '42']), \
                patch('production_worker.load_queue', return_value=[self.record]), \
                patch('production_worker.production_preflight'), \
                patch('production_worker.scan_one', side_effect=failed_scan), \
                patch('production_worker.publish_one') as publish, contextlib.redirect_stdout(out):
            with self.assertRaisesRegex(ValueError, 'PUBLICATION PREFLIGHT FAILED'):
                production_worker.main()
        publish.assert_not_called()
        diagnostics = json.loads(out.getvalue())
        self.assertEqual(set(diagnostics), {'status', 'scanner_status', 'discovery_complete',
                         'discovery_reconciliation', 'catalogue_discovered', 'catalogue_confirmed', 'processed',
                         'technical_failures', 'unaccounted', 'integrity_status', 'publish_eligible',
                         'publication_blockers', 'canonical_score', 'error'})
        self.assertEqual(diagnostics['error'], 'authority_retry_exhausted')
        self.assertNotIn('SECRET', out.getvalue())
        self.assertFalse((state / 'canary-proof.json').exists())


if __name__ == '__main__':
    unittest.main()
