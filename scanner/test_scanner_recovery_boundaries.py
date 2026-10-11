"""Regression checks for evidence-channel recovery without bypassing integrity."""
from pathlib import Path
import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from authority_http import AuthorityHTTP
from catalogue_discovery import (FetchError, Response, confirm_candidate,
                                 product_json_identity, product_json_probe_url)
from confirmed_product import confirm_product
from scanner_pipeline import pipeline_fingerprint, run_brand, SCANNER_VERSION
from scanner_store import Store
from test_operational_canary import Clock, LimitedHTTP
from throne_read import READER_VERSION
from throne_standard import VERSION


GIFT_URL = 'https://brand.test/products/gift-card-pro'


def gift_candidate(url=GIFT_URL):
    return {'url': url, 'discovery_source': 'product_sitemap',
            'product_sitemap_identity': True,
            'catalogue_source_url': 'https://brand.test/sitemap_products_1.xml'}


def gift_payload(**changes):
    return {'id': 1234, 'title': 'BRANWYN Gift Card', 'handle': 'gift-card-pro',
            'url': '/products/gift-card-pro', 'description': 'Digital gift card',
            'type': 'Gift Card', 'variants': [{'id': 5678, 'price': 5000}],
            'price': 5000, **changes}


class ProductJSONRecoveryTest(unittest.TestCase):
    def ajax_product(self, **changes):
        from scan_contract import read_products
        from scanner_store import discovery_hash
        url = 'https://brand.test/products/merino-bra'
        payload = gift_payload(id=123, title='Merino bra', handle='merino-bra',
                               url='/products/merino-bra', type='Bra',
                               description='81% Merino wool, 14% nylon, 5% elastane',
                               variants=[{'id': 456, 'product_id': 123, 'sku': 'REAL-BRA-SKU'}], **changes)
        discovery = product_json_identity(gift_candidate(url),
                                           Response(url + '.js', json.dumps(payload), {}), url + '.js')
        row = read_products([discovery], {'department': 'clothing'}, True)[0][0]
        return {'item': copy.deepcopy(discovery), 'discovery': discovery, 'result': row,
                'discovery_hash': discovery_hash(discovery)}

    def test_ajax_single_variant_projects_only_verified_genuine_sku(self):
        from htt_adapters import publication_style
        product = self.ajax_product()
        original = copy.deepcopy(product)
        style = publication_style(product)
        self.assertEqual(style['sku'], 'REAL-BRA-SKU')
        self.assertEqual(style['sku_identity_source']['source'],
                         'discovery.product_lookup_json.variants[0].sku')
        self.assertEqual(style['sku_identity_source']['variant_id'], '456')
        self.assertEqual(style['sku_identity_source']['response_sha256'],
                         product['discovery']['product_lookup_response_sha256'])
        self.assertEqual(style['terminal_state'], 'scored')
        self.assertEqual(style['score'], 1)
        self.assertEqual(product, original)
        self.assertFalse(product['result'].get('sku'))

    def test_ajax_sku_projection_rejects_unbound_or_ambiguous_evidence(self):
        from htt_adapters import publication_style
        from scanner_store import discovery_hash
        for fault in ('endpoint', 'response_hash', 'snapshot_hash', 'processed_hash',
                      'product', 'style_product', 'style_url', 'variant_parent',
                      'multiple', 'missing_sku', 'wrong_handle', 'non_product_provenance'):
            with self.subTest(fault=fault):
                product = self.ajax_product()
                discovery = product['discovery']
                obj = discovery['product_lookup_json']
                if fault == 'endpoint':
                    discovery['product_lookup_source_endpoint'] = 'https://other.test/products/merino-bra.js'
                elif fault == 'response_hash':
                    discovery['product_lookup_response_sha256'] = '0' * 64
                elif fault == 'snapshot_hash':
                    product['discovery_hash'] = '0' * 64
                elif fault == 'processed_hash':
                    product['item']['product_lookup_response_sha256'] = '0' * 64
                elif fault == 'product':
                    obj['id'] = 999
                elif fault == 'style_product':
                    product['result']['product_id'] = '999'
                elif fault == 'style_url':
                    product['result']['url'] = 'https://brand.test/products/another'
                elif fault == 'variant_parent':
                    obj['variants'][0]['product_id'] = 999
                elif fault == 'multiple':
                    obj['variants'].append({'id': 457, 'product_id': 123, 'sku': 'ANOTHER-REAL-SKU'})
                elif fault == 'missing_sku':
                    obj['variants'][0]['sku'] = ''
                elif fault == 'wrong_handle':
                    obj['handle'] = 'another'
                elif fault == 'non_product_provenance':
                    discovery['product_sitemap_identity'] = False
                    discovery['product_lookup_source_endpoint'] = None
                # Preserve a valid immutable digest for identity/ambiguity cases;
                # these must be rejected by the binding rules independently.
                if fault in {'endpoint', 'product', 'variant_parent', 'multiple', 'missing_sku',
                             'wrong_handle', 'non_product_provenance'}:
                    product['discovery_hash'] = discovery_hash(discovery)
                    product['item'] = copy.deepcopy(discovery)
                self.assertFalse(publication_style(product).get('sku'))

    def test_selected_skus_and_intake_share_ajax_single_variant_identity(self):
        from htt_adapters import publication_plan
        from htt_publish import selected_skus
        product = self.ajax_product()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'run.db')
            try:
                brand = {'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'Fixture', 'slug': 'fixture',
                         'domain': 'https://brand.test', 'cpt': 'clothes', 'department': 'clothing',
                         'manual_catalogue_complete': True}
                rid = store.create(brand, pipeline_fingerprint(), SCANNER_VERSION,
                                   READER_VERSION, VERSION)['scan_run_id']
                # Exercise normal saved-census publication projection. The result
                # has no direct SKU; only the bound source variant provides one.
                run = run_brand(store, rid, LimitedHTTP([]),
                                manual=[{**product['discovery'], 'manual_evidence_verified': True}])
                sample = selected_skus(store, rid)
                payload = publication_plan(run, store.iter_products(rid),
                                           {'htt_api': 'https://htt.test'})['intake_payload']
                self.assertEqual([row['sku'] for row in sample], ['REAL-BRA-SKU'])
                self.assertEqual([row['sku'] for row in payload['styles']], ['REAL-BRA-SKU'])
                self.assertEqual(sample[0]['product_id'], '123')
                self.assertEqual(payload['styles'][0]['sku_identity_source'], sample[0]['sku_identity_source'])
                self.assertEqual(run['canonical']['score'], 1)
                self.assertTrue(run['publish_eligible'])
            finally:
                store.close()

    def test_gift_card_shell_uses_exact_product_json_identity(self):
        payload = gift_payload()
        response = Response(GIFT_URL + '.js', json.dumps(payload), {})
        lookup = Mock(return_value=response)
        renderer = Mock()
        out, accepted, reason = confirm_candidate(
            gift_candidate(), Response(GIFT_URL, '<main>Loading gift-card form</main>', {}),
            renderer, lookup)
        self.assertTrue(accepted, reason)
        self.assertEqual(out['platform_product_id'], '1234')
        self.assertEqual(out['title'], 'BRANWYN Gift Card')
        self.assertEqual(out['confirmation_source'], 'product_json')
        self.assertEqual(out['product_lookup_json'], payload)
        self.assertEqual(out['product_lookup_response_sha256'],
                         hashlib.sha256(response.text.encode()).hexdigest())
        # Do not expose Shopify's integer minor-unit prices as dollar amounts.
        from scan_contract import read_products
        row = read_products([out], {'department': 'clothing'}, True)[0][0]
        self.assertEqual(row['terminal_state'], 'excluded_nontextile')
        self.assertIsNone(row['score'])
        self.assertIsNone(row['price'])
        lookup.assert_called_once_with(GIFT_URL + '.js')
        renderer.assert_not_called()

    def test_product_url_or_name_without_public_product_evidence_cannot_confirm(self):
        for candidate in ({'url': GIFT_URL, 'title': 'Gift Card'},
                          {**gift_candidate(), 'discovery_source': 'sitemap'},
                          gift_candidate('https://brand.test/pages/gift-card-pro'),
                          gift_candidate('https://brand.test/collections/gift-card-pro'),
                          gift_candidate('https://user:secret@brand.test/products/gift-card-pro'),
                          gift_candidate(GIFT_URL + '?variant=123')):
            self.assertIsNone(product_json_probe_url(candidate))
            self.assertFalse(confirm_product(candidate)[0])

    def test_wrong_product_host_url_handle_and_existing_id_are_rejected(self):
        for changes in ({'handle': 'another'}, {'url': '/products/another'},
                        {'url': 'https://other.test/products/gift-card-pro'},
                        {'url': 'https://user:secret@brand.test/products/gift-card-pro'}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(FetchError, 'product_json_identity_mismatch'):
                    product_json_identity(gift_candidate(),
                                          Response(GIFT_URL + '.js', json.dumps(gift_payload(**changes)), {}),
                                          GIFT_URL + '.js')
        for field in ('product_id', 'platform_product_id'):
            with self.assertRaisesRegex(FetchError, 'product_json_identity_mismatch'):
                product_json_identity({**gift_candidate(), field: '9999'},
                                      Response(GIFT_URL + '.js', json.dumps(gift_payload()), {}),
                                      GIFT_URL + '.js')
        for destination in ('https://other.test/products/gift-card-pro.js',
                            'https://brand.test/products/another.js'):
            with self.assertRaisesRegex(FetchError, 'product_json_identity_mismatch'):
                product_json_identity(gift_candidate(), Response(destination, json.dumps(gift_payload()), {}),
                                      GIFT_URL + '.js')

    def test_malformed_product_identity_never_falls_through_to_rendered_evidence(self):
        renderer = Mock(return_value='<main>Gift Card</main>')
        for change in ({'id': 0}, {'id': True}, {'id': 'not-an-id'}, {'title': []},
                       {'variants': []}, {'variants': [{'id': 0}]},
                       {'variants': [{'id': 5678}, {'id': 5678}]}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(FetchError, 'malformed_product_identity'):
                    confirm_candidate(gift_candidate(), Response(GIFT_URL, '', {}), renderer,
                                      Mock(return_value=Response(GIFT_URL + '.js',
                                                                 json.dumps(gift_payload(**change)), {})))
        renderer.assert_not_called()

    def test_unavailable_product_json_uses_existing_rendered_positive_gate(self):
        html = '<script type="application/ld+json">' + json.dumps(
            {'@type': 'Product', 'url': GIFT_URL, 'name': 'BRANWYN Gift Card', 'sku': 'GIFT-1'}) + '</script>'
        for failure in (FetchError('http_404'), FetchError('bad_json'), FetchError('http_429', True)):
            with self.subTest(state=failure.state):
                out, accepted, reason = confirm_candidate(
                    gift_candidate(), Response(GIFT_URL, '', {}), Mock(return_value=html),
                    Mock(side_effect=failure))
                self.assertTrue(accepted, reason)
                self.assertEqual(out['confirmation_probe'], {'url': GIFT_URL + '.js', 'state': failure.state})
                self.assertEqual(out['confirmation_source'], 'rendered_html')

    def test_gift_card_is_reconciled_in_full_census_without_entering_textile_score(self):
        class HTTP:
            base = 'https://brand.test'
            validate = staticmethod(lambda url: url)

            def get(self, url):
                if url == self.base + '/sitemap.xml':
                    return Response(url, '<sitemapindex><sitemap><loc>' + self.base
                                    + '/sitemap_products_1.xml</loc></sitemap></sitemapindex>', {})
                if url == self.base + '/sitemap_products_1.xml':
                    return Response(url, '<urlset><url><loc>' + GIFT_URL + '</loc></url>'
                                    '<url><loc>' + self.base + '/products/shirt</loc></url></urlset>', {})
                if url == GIFT_URL + '.js':
                    return Response(url, json.dumps(gift_payload()), {})
                if url == GIFT_URL:
                    return Response(url, '<main>Loading gift-card form</main>', {})
                if url == self.base + '/products/shirt':
                    return Response(url, '<script type="application/ld+json">' + json.dumps(
                        {'@type': 'Product', 'url': url, 'sku': 'SHIRT-1', 'name': 'Cotton shirt',
                         'material': '100% cotton'}) + '</script>', {})
                raise FetchError('http_404')

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'run.db')
            self.addCleanup(store.close)
            brand = {'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'Fixture', 'slug': 'fixture',
                     'domain': HTTP.base, 'cpt': 'clothes', 'department': 'clothing', 'adapter': 'sitemap'}
            rid = store.create(brand, pipeline_fingerprint(), SCANNER_VERSION,
                               READER_VERSION, VERSION)['scan_run_id']
            renderer = Mock()
            result = run_brand(store, rid, HTTP(), renderer)
            self.assertEqual((result['catalogue_discovered'], result['catalogue_confirmed'],
                              result['processed'], result['unaccounted']), (2, 2, 2, 0))
            self.assertEqual(result['discovery_rejected'], {})
            self.assertEqual(result['discovery_reconciliation'], 'PASS')
            self.assertEqual(result['excluded_nontextile'], 1)
            self.assertEqual(result['technical_failures'], 0)
            self.assertTrue(result['publish_eligible'])
            self.assertEqual(result['canonical']['catalogue_total'], 1)
            self.assertEqual(result['canonical']['score'], 5)
            renderer.assert_not_called()

    def test_static_429_recovers_and_scores_from_saved_exact_product_json(self):
        shirt_url = 'https://brand.test/products/shirt'

        class HTTP(LimitedHTTP):
            def get(self, target):
                self.calls.append(target)
                if target.endswith('/sitemap.xml'):
                    return Response(target, '<sitemapindex><sitemap><loc>' + self.base
                                    + '/sitemap_products_1.xml</loc></sitemap></sitemapindex>', {})
                if target.endswith('/sitemap_products_1.xml'):
                    return Response(target, '<urlset><url><loc>' + GIFT_URL + '</loc></url>'
                                    '<url><loc>' + shirt_url + '</loc></url></urlset>', {})
                if target == GIFT_URL + '.js':
                    return Response(target, json.dumps(gift_payload()), {})
                if target == shirt_url + '.js':
                    return Response(target, json.dumps(gift_payload(
                        id=1235, title='Cotton shirt', handle='shirt', url='/products/shirt',
                        type='Shirt', description='100% cotton')), {})
                if target in {GIFT_URL, shirt_url}:
                    raise FetchError('http_429', True, '15')
                raise FetchError('http_404')

        for renderer in (None, Mock()):
            with self.subTest(render_enabled=renderer is not None), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / 'run.db')
                try:
                    brand = {'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'Fixture', 'slug': 'fixture',
                             'domain': HTTP.base, 'cpt': 'clothes', 'department': 'clothing', 'adapter': 'sitemap'}
                    rid = store.create(brand, pipeline_fingerprint(), SCANNER_VERSION,
                                       READER_VERSION, VERSION)['scan_run_id']
                    clock = Clock()
                    raw = HTTP([])
                    http = AuthorityHTTP(raw, store, rid, retry_limit=1,
                                         clock=clock.time, sleep=clock.sleep, jitter=lambda low, high: 0)
                    result = run_brand(store, rid, http, renderer)
                    self.assertTrue(result['publish_eligible'], result.get('error'))
                    self.assertEqual(result['technical_failures'], 0)
                    self.assertEqual(result['canonical']['score'], 5)
                    self.assertEqual((result['catalogue_confirmed'], result['processed']), (2, 2))
                    self.assertEqual(result['excluded_nontextile'], 1)
                    self.assertEqual(result['http_429_count'], 4)
                    self.assertIsNone(result.get('authority_retry'))
                    self.assertEqual(result['discovery_reconciliation'], 'PASS')
                    for product in store.products(rid):
                        item = product['item']
                        self.assertEqual(raw.calls.count(item['url']), 2)
                        self.assertEqual(raw.calls.count(item['url'] + '.js'), 1)
                        self.assertEqual(item['source_endpoint'], item['url'] + '.js')
                        self.assertEqual(item['response_sha256'], item['product_lookup_response_sha256'])
                        self.assertEqual(store.db.execute(
                            'SELECT attempts,state FROM authority_retries WHERE run_id=? AND url=?',
                            (rid, item['url'])).fetchone(), (2, 'EXHAUSTED'))
                    if renderer is not None:
                        renderer.assert_not_called()
                finally:
                    store.close()


class RenderThrottleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'run.db')
        self.addCleanup(self.store.close)
        brand = {'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'Fixture', 'slug': 'fixture',
                 'cpt': 'clothes', 'department': 'clothing', 'domain': 'https://brand.test'}
        self.rid = self.store.create(brand, pipeline_fingerprint(), SCANNER_VERSION,
                                     READER_VERSION, VERSION)['scan_run_id']
        self.clock = Clock()

    def client(self, errors=()):
        return AuthorityHTTP(LimitedHTTP(errors), self.store, self.rid, retry_limit=1,
                             clock=self.clock.time, sleep=self.clock.sleep,
                             jitter=lambda low, high: 0)

    def test_browser_obeys_exhausted_static_retry_after(self):
        http = self.client([FetchError('http_429', True, '120')] * 2)
        url = http.base + '/products/shirt'
        with self.assertRaisesRegex(FetchError, 'authority_retry_exhausted'):
            http.get(url)
        deadline = self.store.run(self.rid)['authority_retry']['retry_at']

        def render(target):
            self.assertEqual(target, url)
            self.assertGreaterEqual(self.clock.time(), deadline)
            return '<main>Product evidence</main>'

        http.render(url, render)
        self.assertEqual(self.store.run(self.rid)['authority_retry']['url'], url)
        self.assertEqual(self.store.run(self.rid)['authority_retry']['state'], 'EXHAUSTED')
        self.assertLessEqual(max(self.clock.sleeps), 30)

    def test_long_domain_deadline_defers_before_browser_network(self):
        http = self.client()
        with self.store.db:
            self.store.db.execute('UPDATE authority_domains SET next_at=5000')
        renderer = Mock()
        with self.assertRaisesRegex(FetchError, 'domain_backoff'):
            http.render(http.base + '/products/shirt', renderer)
        renderer.assert_not_called()
        self.assertEqual(self.store.run(self.rid)['worker_retry_at'], 5000)
        self.assertEqual(self.store.run(self.rid)['status'], 'DEFERRED')

    def test_browser_429_persists_domain_deadline_for_static_channel(self):
        http = self.client()
        url = http.base + '/products/shirt'
        renderer = Mock(side_effect=FetchError('http_429', True, '120'))
        with self.assertRaisesRegex(FetchError, 'domain_backoff'):
            http.render(url, renderer)
        self.assertEqual(self.store.run(self.rid)['http_429_count'], 1)
        self.assertEqual(self.store.run(self.rid)['status'], 'DEFERRED')
        self.assertEqual(self.store.db.execute(
            'SELECT attempts,retry_at,state FROM authority_retries WHERE url=?',
            ('rendered:' + url,)).fetchone(), (1, 1120, 'BACKOFF'))
        http.get(http.base + '/products/next')
        self.assertGreaterEqual(self.clock.time(), 1120)
        self.assertLessEqual(max(self.clock.sleeps), 30)
        renderer.assert_called_once_with(url)

    def test_browser_retry_ceiling_is_durable(self):
        http = self.client()
        url = http.base + '/products/shirt'
        renderer = Mock(side_effect=FetchError('http_429', True, '15'))
        with self.assertRaisesRegex(FetchError, 'domain_backoff'):
            http.render(url, renderer)
        # Recreate the client to prove the ceiling and deadline live in SQLite.
        http = self.client()
        for _ in range(2):
            with self.assertRaisesRegex(FetchError, 'render_retry_exhausted'):
                http.render(url, renderer)
        self.assertEqual(renderer.call_count, 2)
        self.assertGreaterEqual(self.clock.time(), 1015)
        self.assertEqual(self.store.run(self.rid)['http_429_count'], 2)

    def test_real_browser_preserves_retry_after_on_429(self):
        from rendered_evidence import BrowserRenderer
        from test_pipeline import FixtureServer
        with FixtureServer() as server:
            calls = []

            def limited(handler):
                calls.append(handler.path)
                handler.send_response(429)
                handler.send_header('Retry-After', '120')
                handler.end_headers()
                handler.wfile.write(b'Too many requests')

            with patch.object(server.server.RequestHandlerClass, 'do_GET', limited):
                with self.assertRaises(FetchError) as raised:
                    BrowserRenderer(allow_local=True)(server.base + '/products/shirt')
            self.assertEqual(raised.exception.state, 'http_429')
            self.assertEqual(raised.exception.retry_after, '120')
            self.assertEqual(calls, ['/products/shirt'])

    def test_pipeline_defers_browser_429_and_resumes_without_failed_html_cache(self):
        url = 'https://brand.test/products/shirt'

        class HTTP(LimitedHTTP):
            def get(self, target):
                self.calls.append(target)
                if target.endswith('/sitemap.xml'):
                    return Response(target, '<sitemapindex><sitemap><loc>' + self.base
                                    + '/sitemap_products_1.xml</loc></sitemap></sitemapindex>', {})
                if target.endswith('/sitemap_products_1.xml'):
                    return Response(target, '<urlset><url><loc>' + url + '</loc></url></urlset>', {})
                if target == url:
                    raise FetchError('http_429', True, '15')
                raise FetchError('http_404')

        self.store.update(self.rid, adapter='sitemap')
        raw = HTTP([])
        http = AuthorityHTTP(raw, self.store, self.rid, retry_limit=1,
                             clock=self.clock.time, sleep=self.clock.sleep,
                             jitter=lambda low, high: 0)
        renderer = Mock(side_effect=FetchError('http_429', True, '120'))
        held = run_brand(self.store, self.rid, http, renderer)
        self.assertEqual(held['status'], 'DEFERRED')
        self.assertFalse(held['discovery_complete'])
        self.assertIsNone(self.store.page(self.rid, 'rendered:' + url))
        deadline = held['worker_retry_at']
        html = '<script type="application/ld+json">' + json.dumps(
            {'@type': 'Product', 'url': url, 'sku': 'SHIRT-1', 'name': 'Cotton shirt',
             'material': '100% cotton'}) + '</script>'

        def resumed_render(target):
            self.assertGreaterEqual(self.clock.time(), deadline)
            return html

        resumed = run_brand(self.store, self.rid, http, resumed_render, retry=True)
        self.assertTrue(resumed['publish_eligible'], resumed.get('error'))
        self.assertEqual(resumed['technical_failures'], 0)
        self.assertEqual(resumed['canonical']['score'], 5)
        self.assertEqual(raw.calls.count(url), 2)
        self.assertEqual(resumed['http_429_count'], 3)
        self.assertEqual(self.store.page(self.rid, 'rendered:' + url)['text'], html)

    def test_http_200_browser_challenge_is_not_replayed_on_resume(self):
        url = 'https://brand.test/products/shirt'

        class HTTP(LimitedHTTP):
            def get(self, target):
                if target.endswith('/sitemap.xml'):
                    return Response(target, '<sitemapindex><sitemap><loc>' + self.base
                                    + '/sitemap_products_1.xml</loc></sitemap></sitemapindex>', {})
                if target.endswith('/sitemap_products_1.xml'):
                    return Response(target, '<urlset><url><loc>' + url + '</loc></url></urlset>', {})
                if target == url:
                    return Response(target, '<main>Loading shirt</main>', {})
                raise FetchError('http_404')

        self.store.update(self.rid, adapter='sitemap')
        html = '<script type="application/ld+json">' + json.dumps(
            {'@type': 'Product', 'url': url, 'sku': 'SHIRT-1', 'name': 'Cotton shirt',
             'material': '100% cotton'}) + '</script>'
        renderer = Mock(side_effect=['<title>Just a moment</title>Verify you are human', html])
        held = run_brand(self.store, self.rid, HTTP([]), renderer)
        self.assertEqual(held['status'], 'RETRY')
        self.assertEqual(held['error'], 'blocked')
        self.assertIsNone(self.store.page(self.rid, 'rendered:' + url))
        resumed = run_brand(self.store, self.rid, HTTP([]), renderer, retry=True)
        self.assertTrue(resumed['publish_eligible'], resumed.get('error'))
        self.assertEqual(resumed['technical_failures'], 0)
        self.assertEqual(resumed['canonical']['score'], 5)
        self.assertEqual(renderer.call_count, 2)
        self.assertEqual(self.store.page(self.rid, 'rendered:' + url)['text'], html)


if __name__ == '__main__':
    unittest.main()
