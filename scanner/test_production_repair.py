"""Deterministic regressions for production discovery, identity and worker failures."""
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from catalogue_discovery import (Discovery, FetchError, PublicHTTP, Response,
                                 confirm_candidate, crawl_origin)
from authority_batch import domain_affine_shards, publication_lock, scan_one
from scanner_pipeline import run_brand, pipeline_fingerprint, select_queue, SCANNER_VERSION
from scanner_store import Store
from throne_read import READER_VERSION
from throne_standard import VERSION
from publication_parity import resolve_idealwrld_context, ContractError


class Probe:
    base = 'https://brand.test/pages/organic-cotton'
    validate = lambda self, url: url

    def __init__(self, state, partial=False):
        self.state, self.partial, self.calls = state, partial, []

    def get(self, url):
        self.calls.append(url)
        if '/products.json' in url:
            if self.partial and url.endswith('page=1'):
                return Response(url, json.dumps({'products': [{'id': 1, 'handle': 'shirt'}]}), {})
            raise FetchError(self.state)
        if '/wp-json/' in url:
            return Response(url, json.dumps([{'id': 2, 'permalink': 'https://brand.test/product/dress'}]), {'X-WP-TotalPages': '1'})
        raise FetchError('http_404')


def lock_probe(path, ready, acquired):
    ready.set()
    with publication_lock(path):
        acquired.set()


class ProductionRepairTest(unittest.TestCase):
    def test_page_url_probes_origin_and_preserves_identity(self):
        http = Probe('http_400')
        self.assertEqual(len(list(Discovery(http).enumerate())), 1)
        self.assertTrue(all('/pages/' not in url for url in http.calls))
        self.assertEqual(http.base, 'https://brand.test/pages/organic-cotton')
        self.assertEqual(crawl_origin(http.base), 'https://brand.test')

    def test_all_initial_probe_misses_fall_through(self):
        for state in ('http_400', 'http_404', 'http_410', 'bad_json', 'bad_catalogue_schema',
                      'timeout', 'connection_error', 'http_429', 'authority_retry_exhausted'):
            with self.subTest(state=state):
                discovery = Discovery(Probe(state))
                self.assertEqual([x['product_id'] for x in discovery.enumerate()], ['2'])
                self.assertEqual(discovery.outcomes[0]['state'], state)

    def test_partial_catalogue_and_security_never_fall_back(self):
        for state, partial in [('timeout', True), ('bad_json', True), ('offsite_url', False), ('unsafe_address', False)]:
            http = Probe(state, partial)
            with self.assertRaises(FetchError):
                list(Discovery(http).enumerate())
            self.assertFalse(any('/wp-json/' in url for url in http.calls))

    def test_canonical_and_locale_redirects_are_bounded(self):
        # Exercise the actual urllib redirect handler, including DNS protections.
        import urllib.request
        def opener(handler):
            class Opener:
                def open(inner, request, timeout):
                    target = 'https://en.brand.test/'
                    handler().redirect_request(request, None, 301, '', {}, target)
                    response = Mock()
                    response.url = target
                    response.read.return_value = b'<html></html>'
                    response.headers = {}
                    response.__enter__ = Mock(return_value=response)
                    response.__exit__ = Mock(return_value=False)
                    return response
            return Opener()
        with patch('catalogue_discovery.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]), patch('catalogue_discovery.urllib.request.build_opener', side_effect=opener):
            http = PublicHTTP('https://www.brand.test/pages/cotton')
            result = http.resolve()
            self.assertEqual(result['crawl_origin'], 'https://en.brand.test')
            self.assertEqual(len(result['redirects']), 1)
            http.validate('https://en.brand.test/products.json')
            with self.assertRaisesRegex(FetchError, 'offsite'):
                http.validate('https://other.test/products.json')

    def test_malicious_and_private_redirects_rejected(self):
        import urllib.request
        for target in ('https://evil.test/', 'http://127.0.0.1/', 'https://user:pass@brand.test/'):
            def opener(handler):
                class Opener:
                    def open(inner, request, timeout):
                        handler().redirect_request(request, None, 301, '', {}, target)
                return Opener()
            with patch('catalogue_discovery.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]), patch('catalogue_discovery.urllib.request.build_opener', side_effect=opener):
                with self.assertRaises(FetchError):
                    PublicHTTP('https://brand.test').resolve()
        with patch('catalogue_discovery.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaisesRegex(FetchError, 'unsafe_address'):
                PublicHTTP('https://brand.test')

    def test_static_rendered_and_schema_free_product_confirmation(self):
        url = 'https://brand.test/products/shirt'
        html = '<script type="application/ld+json">' + json.dumps({'@type': 'Product', 'url': '/products/shirt', 'name': 'Cotton shirt', 'material': '100% cotton', 'sku': 'SHIRT-1'}) + '</script>'
        for static, render in [(html, None), ('<main>Loading shirt</main>', lambda url: html)]:
            item, accepted, reason = confirm_candidate({'url': url}, Response(url, static, {}), render)
            self.assertTrue(accepted, reason)
        metadata = '<meta property="og:type" content="product"><meta property="og:url" content="'+url+'"><meta property="product:retailer_item_id" content="SHIRT-2">'
        self.assertTrue(confirm_candidate({'url': url}, Response(url, metadata, {}))[1])

    def test_category_and_unrelated_schema_do_not_confirm(self):
        renderer = Mock(return_value='<main>No product evidence</main>')
        for url in ('https://brand.test/collections/shirts', 'https://brand.test/blog/story', 'https://brand.test/products/shirt'):
            html = '<script type="application/ld+json">{"@type":"Product","url":"https://other.test/products/shirt"}</script>'
            self.assertFalse(confirm_candidate({'url': url}, Response(url, html, {}), renderer)[1])
        self.assertEqual(renderer.call_count, 1)

    def test_stale_downstream_id_is_resolved_using_htt_identity(self):
        canonical = {'slug': 'shirt', 'htt_id': 'htt-1', 'idealwrld_id': 1}
        config = {'surfaces': {'app_catalogue': {'url': 'https://app.test/brands/{slug}'}}}
        transport = Mock()
        transport.request.return_value = {'id': 777, 'htt_id': 'htt-1', 'slug': 'shirt'}
        self.assertEqual(resolve_idealwrld_context(canonical, config, transport)['idealwrld_id'], 777)
        self.assertEqual(canonical['idealwrld_id'], 1)
        transport.request.return_value = {'id': 777, 'slug': 'shirt'}
        with self.assertRaises(ContractError):
            resolve_idealwrld_context(canonical, config, transport)

    def test_exact_id_wins_over_duplicate_names(self):
        rows = [({'brand_id': str(i), 'brand': 'Same', 'slug': 'same'}, None) for i in (1, 2)]
        self.assertEqual(select_queue(rows, '2'), [rows[1]])
        with self.assertRaises(ValueError):
            select_queue(rows, 'Same')

    def test_resolved_domain_and_locales_have_one_owner(self):
        rows = [{'brand_id': '1', 'domain': 'https://www.brand.test/pages/cotton'},
                {'brand_id': '2', 'domain': 'https://en.brand.test'},
                {'brand_id': '3', 'domain': 'https://old.test', 'crawl_origin': 'https://brand.test'}]
        shards, _ = domain_affine_shards(rows, 4)
        self.assertEqual(sum(bool(s) for s in shards), 1)

    def test_global_publication_lock_serializes_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'publish.lock'
            ready, acquired = multiprocessing.Event(), multiprocessing.Event()
            with publication_lock(path):
                process = multiprocessing.Process(target=lock_probe, args=(path, ready, acquired))
                process.start()
                self.assertTrue(ready.wait(5))
                self.assertFalse(acquired.wait(.1))
            self.assertTrue(acquired.wait(5))
            process.join(5)
            self.assertEqual(process.exitcode, 0)

    def test_transient_retry_resumes_then_defers_durably(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'worker.db')
            self.addCleanup(store.close)
            brand = {'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'One', 'domain': 'https://brand.test', 'cpt': 'clothes'}
            store.enqueue([brand])
            rid = store.create(brand, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
            store.update(rid, status='RETRY')
            def retry(store, rid, *args, **kwargs):
                self.assertTrue(kwargs.get('retry'))
                store.update(rid, status='RETRY', error='timeout')
                return store.run(rid)
            with patch('authority_batch.PublicHTTP'), patch('authority_batch.run_brand', side_effect=retry) as runner, patch('authority_batch.time.sleep'):
                result = scan_one(store, brand, rid)
                self.assertEqual(runner.call_count, 3)
                self.assertEqual(result['status'], 'DEFERRED')
                self.assertGreater(result['worker_retry_at'], 0)
                self.assertEqual(scan_one(store, brand, rid)['status'], 'DEFERRED')
                self.assertEqual(runner.call_count, 3)

    def test_no_products_malformed_sitemap_and_luggage_remain_held(self):
        from test_pipeline import product
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            self.addCleanup(store.close)
            brand = {'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'Luggage', 'domain': 'https://brand.test',
                     'cpt': 'clothes', 'adapter': 'sitemap', 'manual_catalogue_complete': True}
            for fault in ('empty', 'malformed', 'luggage', 'department'):
                with self.subTest(fault=fault):
                    row = {**brand, 'cpt': 'luggage' if fault == 'department' else 'clothes'}
                    store.enqueue([row])
                    rid = store.create(row, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
                    http = Mock(base='https://brand.test')
                    http.get.return_value = Response('https://brand.test/sitemap.xml', '<not-xml', {})
                    manual = [] if fault == 'empty' else [product(title='Hard shell suitcase', product_type='Luggage', body_html='ABS plastic', manual_evidence_verified=True)]
                    run = run_brand(store, rid, http, manual=None if fault == 'malformed' else manual)
                    self.assertFalse(run.get('publish_eligible'))
                    self.assertIsNone((run.get('canonical') or {}).get('score'))
                    if fault == 'malformed':
                        self.assertEqual(run['error'], 'malformed_sitemap')
                    if fault == 'department':
                        http.get.assert_not_called()
                        self.assertEqual(run['scanner_status'], 'HELD')

    def test_recorded_static_product_schema(self):
        html = (Path(__file__).with_name('fixtures') / 'story-page.recorded.html').read_text()
        url = 'https://www.storymfg.com/products/theory-waffle-jacket-r-t-s-co-64'
        self.assertTrue(confirm_candidate({'url': url}, Response(url, html, {}))[1])

    def test_real_browser_confirms_and_scores_sitemap_javascript_product(self):
        from test_pipeline import FixtureServer
        from rendered_evidence import BrowserRenderer
        with tempfile.TemporaryDirectory() as directory, FixtureServer() as server:
            store = Store(Path(directory) / 'render.db')
            self.addCleanup(store.close)
            brand = {'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'Example', 'domain': server.base,
                     'cpt': 'clothes', 'adapter': 'sitemap'}
            store.enqueue([brand])
            rid = store.create(brand, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
            original = server.server.RequestHandlerClass.do_GET
            def get(handler):
                if handler.path == '/products/p1':
                    handler.send("<main>Loading shirt</main><script>const s=document.createElement('script');s.type='application/ld+json';s.textContent=JSON.stringify({'@type':'Product',url:location.href,name:'Cotton shirt',sku:'SHIRT-1',material:'100% cotton'});document.head.appendChild(s)</script>")
                else:
                    original(handler)
            with patch.object(server.server.RequestHandlerClass, 'do_GET', get):
                result = run_brand(store, rid, server.http(), BrowserRenderer(allow_local=True))
            self.assertEqual(result['canonical']['score'], 5)
            self.assertEqual((result['catalogue_confirmed'], result['processed'], result['technical_failures']), (1, 1, 0))
            self.assertTrue(result['publish_eligible'])
            self.assertEqual(result['discovery_rejected'], {'non_product_page_url': 1})
            product_row = store.products(rid)[0]
            self.assertTrue(product_row['discovery']['product_sitemap_identity'])
            self.assertEqual(product_row['item']['confirmation_source'], 'rendered_html')

    def test_worker_requires_canary_before_opening_shard(self):
        from authority_batch import worker
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                worker(directory, 'W1', None, publish=True)
            self.assertFalse((Path(directory) / 'W1.db').exists())

    def test_interruption_after_retry_checkpoint_obeys_deadline_and_ceiling(self):
        from test_operational_canary import Clock
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'worker.db')
            self.addCleanup(store.close)
            brand = {'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'One', 'domain': 'https://brand.test', 'cpt': 'clothes'}
            store.enqueue([brand])
            rid = store.create(brand, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
            clock = Clock()
            store.update(rid, status='RETRY', worker_attempts=1, worker_retry_at=clock.time() + 80)
            def finish(store, rid, *args, **kwargs):
                self.assertGreaterEqual(clock.time(), 1080)
                store.update(rid, status='SCANNER_COMPLETE', scanner_status='SCANNER_COMPLETE')
                return store.run(rid)
            with patch('authority_batch.PublicHTTP'), patch('authority_batch.run_brand', side_effect=finish), patch('authority_batch.time.time', side_effect=clock.time), patch('authority_batch.time.sleep', side_effect=clock.sleep):
                self.assertEqual(scan_one(store, brand, rid)['status'], 'SCANNER_COMPLETE')
            self.assertLessEqual(max(clock.sleeps), 30)
            store.update(rid, status='RETRY', scanner_status='INCOMPLETE', worker_attempts=3)
            with patch('authority_batch.PublicHTTP') as http:
                self.assertEqual(scan_one(store, brand, rid)['status'], 'DEFERRED')
                http.assert_not_called()

    def test_downstream_catalogue_selects_one_stable_identity(self):
        canonical = {'slug': 'shirt', 'htt_id': 'htt-1', 'idealwrld_id': 1}
        config = {'surfaces': {'app_catalogue': {'url': 'https://app.test/brands'}}}
        transport = Mock()
        match = {'id': 777, 'htt_id': 'htt-1', 'slug': 'shirt'}
        transport.request.return_value = [{'id': 888, 'htt_id': 'htt-2', 'slug': 'shirt'}, match]
        self.assertEqual(resolve_idealwrld_context(canonical, config, transport)['idealwrld_id'], 777)
        transport.request.return_value = [match, match]
        with self.assertRaises(ContractError):
            resolve_idealwrld_context(canonical, config, transport)

    def test_locale_prefix_cannot_cross_public_or_private_suffix(self):
        from storefront import storefront_family, registrable_domain
        self.assertNotEqual(storefront_family('us.co.uk'), storefront_family('fr.co.uk'))
        self.assertNotEqual(storefront_family('en.github.io'), storefront_family('fr.github.io'))
        self.assertNotEqual(storefront_family('en.myshopify.com'), storefront_family('fr.myshopify.com'))
        self.assertEqual(storefront_family('shop.brand.co.uk'), storefront_family('en.brand.co.uk'))
        self.assertEqual(registrable_domain('www.city.kawasaki.jp'), 'city.kawasaki.jp')
        self.assertIsNone(registrable_domain('example.ck'))

    def test_four_workers_can_verify_the_same_canary_without_lock_races(self):
        from concurrent.futures import ThreadPoolExecutor
        from production_worker import verify_canary
        import time
        with tempfile.TemporaryDirectory() as directory:
            proof = {'run_id': 'fixture'}
            Path(directory, 'canary-proof.json').write_text(json.dumps(proof))
            def attest(*args):
                time.sleep(.03)
                return proof
            with patch('production_worker.attest_canary', side_effect=attest), ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(verify_canary, directory, {}) for _ in range(4)]
                for future in futures:
                    self.assertIsNone(future.result(timeout=5))

    def test_all_adapters_unavailable_has_precise_held_reason(self):
        http = Mock(base='https://brand.test')
        http.get.side_effect = FetchError('http_404')
        discovery = Discovery(http)
        with self.assertRaisesRegex(FetchError, '^all_adapters_unavailable$'):
            list(discovery.enumerate())
        self.assertEqual([r['adapter'] for r in discovery.outcomes], ['shopify', 'woocommerce', 'sitemap'])
        self.assertTrue(all(r['state'] == 'http_404' for r in discovery.outcomes))

    def test_rendered_conflicting_products_cannot_select_one_composition(self):
        url = 'https://brand.test/products/shirt'
        objects = [{'@type': 'Product', 'url': url, 'sku': sku, 'name': 'Shirt', 'material': material}
                   for sku, material in [('A', '100% cotton'), ('B', '100% polyester')]]
        html = '<script type="application/ld+json">' + json.dumps(objects) + '</script>'
        with self.assertRaisesRegex(FetchError, 'ambiguous_product_identity'):
            confirm_candidate({'url': url}, Response(url, '<main>Loading</main>', {}), lambda url: html)

    def test_rendered_identity_alone_does_not_prove_extractor_coverage(self):
        from catalogue_discovery import capture
        from test_pipeline import product
        item = product(body_html='')
        html = '<script type="application/ld+json">' + json.dumps({'@type': 'Product', 'url': item['url'], 'name': 'Shirt'}) + '</script>'
        with self.assertRaisesRegex(FetchError, 'extractor_unresolved'):
            capture(item, Response(item['url'], '<main>Loading</main>', {}), lambda url: html)

    def test_legacy_transport_holds_5001_without_truncation(self):
        from htt_adapters import publication_plan
        run = {'scan_run_id': 'fixture-run', 'brand_id': '1', 'htt_id': 'htt-1', 'brand': 'Example',
               'domain': 'https://brand.test', 'cpt': 'clothes', 'scanner_fingerprint': 'fixture',
               'canonical': {'scan_date': '2026-10-05T00:00:00Z'}, 'report': {}}
        for count in (5000, 5001):
            products = ({'item': {'product_id': str(i), 'url': f'https://brand.test/products/{i}'}}
                        for i in range(count))
            plan = publication_plan(run, products, {'htt_api': 'https://htt.test'})
            self.assertEqual(len(plan['intake_payload']['styles']), count)
            self.assertEqual('LEGACY_INTAKE_TRANSPORT_LIMIT_REQUIRES_GENERATION_ADAPTER' in plan['blockers'], count > 5000)

    def test_canary_rejects_kent_even_under_another_listing_name(self):
        from production_worker import is_kent
        self.assertTrue(is_kent({'brand_id': 'new-id', 'brand': 'Different name', 'domain': 'https://www.wearkent.com/pages/about'}))
        self.assertFalse(is_kent({'brand_id': 'new-id', 'brand': 'Different name', 'domain': 'https://brand.test'}))


if __name__ == '__main__':
    unittest.main()
