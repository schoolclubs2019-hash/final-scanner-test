"""Offline audit capture/export preserves source evidence and census boundaries."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalogue_audit import collect_sitemap_audit, export_audit, normalize_audit_policy, verify_export
from catalogue_discovery import FetchError, Response
from scan_policy import ScanPaused
from scanner_store import Store, encode


BASE = 'https://audit.test'
ROOT = BASE + '/sitemap.xml'
ROBOTS = BASE + '/robots.txt'
PRODUCTS = BASE + '/sitemap_products_1.xml'
PAGES = BASE + '/sitemap_pages_1.xml'
PRODUCT = BASE + '/products/cotton-shirt'
PAGE1 = BASE + '/products.json?limit=250&page=1'
PAGE2 = BASE + '/products.json?limit=250&page=2'


def xml(kind, *urls):
    tag = 'sitemap' if kind == 'sitemapindex' else 'url'
    return '<' + kind + ' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
        f'<{tag}><loc>{url}</loc><lastmod>2026-10-07</lastmod></{tag}>' for url in urls) + '</' + kind + '>'


class HTTP:
    base = BASE

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.validations = []

    def validate(self, url):
        self.validations.append(url)
        if not url.startswith(BASE + '/'):
            raise FetchError('offsite_url')

    def get(self, url):
        self.calls.append(url)
        value = self.responses.get(url, FetchError('http_404'))
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(url, value, {'content-type': 'application/xml'})


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.db = self.directory / 'checkpoint.sqlite'
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.run = self.store.create({'brand_id': '7', 'htt_id': '42', 'brand': 'Audit',
                                     'domain': BASE, 'cpt': 'clothing'}, 'fingerprint', 'scanner', 'reader', 'standard')
        self.run_id = self.run['scan_run_id']

    def catalogue(self, empty_end=True, browser=False):
        self.store.add(self.run_id, {'url': PRODUCT, 'product_id': '123', 'title': 'Cotton shirt',
                                    'sku': 'COT-1', 'discovery_source': 'shopify', 'source_endpoint': PAGE1})
        first = Response(PAGE1, json.dumps({'products': [{'id': 123, 'handle': 'cotton-shirt'}]}),
                         {'content-type': 'application/json', 'set-cookie': 'SECRET_COOKIE',
                          'authorization': 'SECRET_AUTH'})
        if browser:
            self.save_browser(first)
        else:
            self.store.save_page(self.run_id, PAGE1, first)
        if empty_end:
            self.store.save_page(self.run_id, PAGE2, Response(PAGE2, '{"products":[]}', {}))
        self.store.update(self.run_id, discovery_complete=True, discovery_reconciliation='PASS',
                          catalogue_snapshot_id=self.store.discovery_digest(self.run_id),
                          scanner_status='SCANNER_COMPLETE', config={'password': 'SECRET_CONFIG'})

    def save_browser(self, response):
        data = {'url': response.url, 'text': response.text, 'headers': response.headers,
                'provenance': {'capture_source': 'browser_http_response', 'requested_url': response.url,
                               'adapter': 'shopify', 'http_status': 200,
                               'response_sha256': hashlib.sha256(response.text.encode()).hexdigest()}}
        with self.store.db:
            self.store.db.execute('INSERT INTO pages VALUES(?,?,?)',
                                  (self.run_id, 'catalogue-json:' + response.url, encode(data)))

    def export(self, name='audit', **kwargs):
        return export_audit(self.db, self.run_id, self.directory / name, **kwargs)

    def test_cross_check_saves_actual_sitemaps_and_selected_graph_without_census_changes(self):
        self.catalogue()
        before = self.store.run(self.run_id)
        http = HTTP({ROOT: xml('sitemapindex', PRODUCTS, PAGES), PRODUCTS: xml('urlset', PRODUCT)})
        state = collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        self.assertEqual(state['status'], 'COMPLETE')
        self.assertEqual(http.calls, [ROOT, ROBOTS, PRODUCTS])
        self.assertEqual(self.store.page(self.run_id, ROOT)['text'], http.responses[ROOT])
        self.assertEqual(state['edges'], [{'parent': ROOT, 'child': PRODUCTS, 'selected': True, 'first_party': True},
                                         {'parent': ROOT, 'child': PAGES, 'selected': False, 'first_party': True}])
        after = self.store.run(self.run_id)
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertEqual(self.store.product_count(self.run_id), 1)
        manifest = self.export()
        self.assertEqual(manifest['sitemap']['comparison_status'], 'MATCH')
        self.assertEqual(manifest['sitemap']['matched_urls'], [PRODUCT])

    def test_pause_keeps_cursor_raw_response_and_previous_docs_then_resumes_without_refetch(self):
        http = HTTP({ROOT: xml('sitemapindex', PRODUCTS), PRODUCTS: xml('urlset', PRODUCT)})
        def budget():
            if self.store.page(self.run_id, PRODUCTS):
                raise ScanPaused('site_pass_budget')
        with self.assertRaises(ScanPaused):
            collect_sitemap_audit(http, self.store, self.run_id, check_budget=budget, policy={'mode': 'cross_check'})
        state = self.store.run(self.run_id)['sitemap_audit']
        self.assertEqual(state['status'], 'PAUSED')
        self.assertEqual([entry['requested_url'] for entry in state['documents']], [ROOT])
        self.assertEqual(state['pending'][0]['url'], PRODUCTS)
        self.assertIsNotNone(self.store.page(self.run_id, PRODUCTS))
        resumed = collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        self.assertEqual(resumed['status'], 'COMPLETE')
        self.assertEqual(http.calls, [ROOT, ROBOTS, PRODUCTS])
        self.assertTrue(resumed['documents'][-1]['from_cache'])

    def test_retry_error_preserves_pending_and_caller_cooldown(self):
        self.store.update(self.run_id, status='RETRY', worker_retry_at=123456, total_429=3)
        error = FetchError('http_429', True, '60')
        http = HTTP({ROOT: xml('sitemapindex', PRODUCTS), PRODUCTS: error})
        with self.assertRaises(FetchError) as raised:
            collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        self.assertIs(raised.exception, error)
        run = self.store.run(self.run_id)
        self.assertEqual((run['status'], run['worker_retry_at'], run['total_429']), ('RETRY', 123456, 3))
        self.assertEqual(run['sitemap_audit']['pending'][0]['url'], PRODUCTS)
        self.assertEqual(run['sitemap_audit']['reason'], 'http_429')
        self.assertEqual(len(run['sitemap_audit']['documents']), 1)

    def test_robots_fallback_saves_text_and_fetches_all_first_party_market_roots(self):
        robots = BASE + '/robots.txt'
        other = BASE + '/en-ca/sitemap.xml'
        http = HTTP({robots: f'User-agent: *\nSitemap: {PRODUCTS}\nSitemap: {other}\n',
                     PRODUCTS: xml('urlset', PRODUCT), other: xml('urlset', BASE + '/en-ca/products/cotton-shirt')})
        state = collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        self.assertEqual(state['status'], 'COMPLETE')
        self.assertEqual(http.calls, [ROOT, robots, PRODUCTS, other])
        self.assertEqual(state['failures'], [{'url': ROOT, 'state': 'http_404'}])
        manifest = self.export()
        self.assertEqual(manifest['sitemap']['roots'], sorted([PRODUCTS, other]))
        self.assertIn({'parent': robots, 'child': other, 'selected': True, 'captured': True, 'first_party': True},
                      manifest['sitemap']['graph'])
        self.assertTrue(any(doc['kind'] == 'robots' for doc in manifest['documents']))

    def test_captured_only_is_no_fetch_and_never_calls_json_a_sitemap(self):
        self.catalogue()
        http = HTTP()
        state = collect_sitemap_audit(http, self.store, self.run_id)
        self.assertEqual(state['status'], 'NOT_REQUESTED')
        self.assertEqual(http.calls, [])
        manifest = self.export()
        self.assertEqual(manifest['sitemap']['status'], 'NOT_CAPTURED')
        self.assertEqual(manifest['sitemap']['comparison_status'], 'NOT_AVAILABLE')
        self.assertEqual(manifest['catalogue']['status'], 'COMPLETE')
        self.assertEqual({doc['kind'] for doc in manifest['documents']}, {'shopify'})

    def test_limits_and_empty_index_never_complete(self):
        for name, policy, text, reason in [
            ('documents', {'max_documents': 1}, xml('sitemapindex', PRODUCTS), 'audit_document_limit'),
            ('bytes', {'max_total_bytes': 1}, xml('urlset', PRODUCT), 'audit_byte_limit'),
            ('response', {'max_response_bytes': 1}, xml('urlset', PRODUCT), 'audit_response_limit'),
            ('urls', {'max_urls': 1}, xml('urlset', PRODUCT, PRODUCT + '-two'), 'audit_url_limit'),
            ('empty', {}, xml('sitemapindex'), 'sitemap_urlset_not_captured'),
        ]:
            with self.subTest(name=name):
                self.store.update(self.run_id, sitemap_audit=None)
                with self.store.db:
                    self.store.db.execute('DELETE FROM pages WHERE run_id=?', (self.run_id,))
                try:
                    state = collect_sitemap_audit(HTTP({ROOT: text}), self.store, self.run_id,
                                                  policy={'mode': 'cross_check', **policy})
                except FetchError as exc:
                    self.assertEqual(exc.state, reason)
                    state = self.store.run(self.run_id)['sitemap_audit']
                self.assertEqual((state['status'], state['reason']), ('INCOMPLETE', reason))

    def test_bad_xml_is_retained(self):
        for text in ['<html>challenge</html>', '<!DOCTYPE x [<!ENTITY x "expanded">]><urlset/>']:
            with self.subTest(text=text):
                self.store.update(self.run_id, sitemap_audit=None)
                with self.store.db:
                    self.store.db.execute('DELETE FROM pages WHERE run_id=?', (self.run_id,))
                http = HTTP({ROOT: text})
                with self.assertRaises(FetchError):
                    collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
                self.assertEqual(http.calls, [ROOT])
                self.assertEqual(self.store.page(self.run_id, ROOT)['text'], text)
                self.assertEqual(self.store.run(self.run_id)['sitemap_audit']['status'], 'INCOMPLETE')

    def test_leaf_urls_do_not_resolve_dns_and_external_branches_are_explicit_not_fetched(self):
        external = 'http://127.0.0.1/private'
        http = HTTP({ROOT: xml('sitemapindex', PRODUCTS, external), PRODUCTS: xml('urlset', PRODUCT, external)})
        state = collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        self.assertEqual(state['status'], 'COMPLETE')
        self.assertEqual(http.calls, [ROOT, ROBOTS, PRODUCTS])
        self.assertNotIn(PRODUCT, http.validations)
        self.assertNotIn(external, http.validations)
        result = self.export()
        self.assertEqual(result['sitemap']['external_leaf_urls'], [external])
        self.assertIn({'parent': ROOT, 'child': external, 'selected': False, 'captured': False, 'first_party': False},
                      result['sitemap']['graph'])

    def test_policy_is_bounded_strict_and_immutable_on_revisit(self):
        for invalid in [{'mode': 'automatic'}, {'unknown': 1}, {'max_documents': True},
                        {'max_response_bytes': 8000001}, {'audit_required': 'false'}, {'max_urls': 0},
                        {'max_export_bytes': 2147483649}, {'max_export_bytes': True}, {'audit_required': True}]:
            with self.subTest(policy=invalid), self.assertRaises(ValueError):
                normalize_audit_policy(invalid)
        collect_sitemap_audit(HTTP(), self.store, self.run_id)
        with self.assertRaises(ValueError):
            collect_sitemap_audit(HTTP(), self.store, self.run_id, policy={'mode': 'cross_check'})

    def test_export_is_offline_readonly_checksummed_and_excludes_private_metadata(self):
        self.catalogue(browser=True)
        original = self.store.db.iterdump()
        before = '\n'.join(original)
        with patch('socket.create_connection', side_effect=AssertionError('No export network requests')):
            manifest = self.export()
        self.assertEqual('\n'.join(self.store.db.iterdump()), before)
        self.assertEqual(manifest['network_requests'], 0)
        self.assertEqual(manifest['export']['manifest_path'], str(self.directory / 'audit/manifest.json'))
        self.assertEqual(manifest['export']['file_count'], len([p for p in (self.directory / 'audit').rglob('*') if p.is_file()]))
        self.assertEqual(manifest['catalogue']['status'], 'COMPLETE')
        output = self.directory / 'audit'
        combined = b''.join(path.read_bytes() for path in output.rglob('*') if path.is_file())
        for secret in (b'SECRET_COOKIE', b'SECRET_AUTH', b'SECRET_CONFIG'):
            self.assertNotIn(secret, combined)
        for line in (output / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)
        for doc in manifest['documents']:
            self.assertEqual(hashlib.sha256((output / doc['file']).read_bytes()).hexdigest(), doc['sha256'])


    def test_missing_selected_branch_is_incomplete_and_unselected_branch_is_not_compared(self):
        self.catalogue()
        self.store.save_page(self.run_id, ROOT, Response(ROOT, xml('sitemapindex', PRODUCTS, PAGES), {}))
        self.store.save_page(self.run_id, PAGES, Response(PAGES, xml('urlset', BASE + '/pages/about'), {}))
        missing = self.export('missing')
        self.assertEqual(missing['sitemap']['status'], 'INCOMPLETE')
        self.assertEqual(missing['sitemap']['unfetched_selected_links'], [PRODUCTS])
        self.assertEqual(missing['sitemap']['comparison_status'], 'NOT_AVAILABLE')
        self.store.save_page(self.run_id, PRODUCTS, Response(PRODUCTS, xml('urlset', PRODUCT), {}))
        complete = self.export('complete')
        self.assertEqual(complete['sitemap']['comparison_status'], 'MATCH')
        self.assertNotIn(BASE + '/pages/about', complete['sitemap']['sitemap_only_urls'])

    def test_short_first_page_is_not_exhaustion_and_page_gap_is_explicit(self):
        self.catalogue(empty_end=False)
        manifest = self.export('short')
        self.assertEqual(manifest['catalogue']['status'], 'INCOMPLETE')
        self.assertEqual(manifest['catalogue']['capture']['basis'], 'no_exhaustion_evidence')
        page3 = PAGE2.replace('page=2', 'page=3')
        self.store.save_page(self.run_id, page3, Response(page3, '{"products":[]}', {}))
        gap = self.export('gap')
        self.assertEqual(gap['catalogue']['capture']['basis'], 'missing_catalogue_page')

    def test_original_inventory_is_exported_separately_from_current_product_url(self):
        self.catalogue()
        row = self.store.products(self.run_id)[0]
        item = {**row['item'], 'url': PRODUCT + '-moved'}
        self.store.save_product(self.run_id, row['identity'], item, {'terminal_state': 'scored', 'product_id': '123'})
        self.export()
        product = json.loads((self.directory / 'audit/products.jsonl').read_text())
        self.assertEqual(product['original_url'], PRODUCT)
        self.assertEqual(product['current_url'], PRODUCT + '-moved')

    def test_corrupt_browser_provenance_or_original_discovery_is_never_complete(self):
        self.catalogue(browser=True)
        with self.store.db:
            raw = self.store.page(self.run_id, 'catalogue-json:' + PAGE1)
            raw['provenance']['response_sha256'] = 'bad'
            self.store.db.execute('UPDATE pages SET data=? WHERE run_id=? AND url=?',
                                  (encode(raw), self.run_id, 'catalogue-json:' + PAGE1))
        manifest = self.export('provenance')
        self.assertEqual(manifest['catalogue']['status'], 'INCOMPLETE')
        self.assertFalse(next(doc for doc in manifest['documents'] if doc['cache_key'].startswith('catalogue-json:'))['provenance_valid'])
        with self.store.db:
            self.store.db.execute('UPDATE products SET discovery_hash=? WHERE run_id=?', ('bad', self.run_id))
        manifest = self.export('discovery')
        self.assertFalse(manifest['catalogue']['snapshot_matches'])
        self.assertEqual(len(manifest['catalogue']['invalid_discovery_identities']), 1)

    def test_saved_sitemap_checkpoint_rejects_changed_xml_on_revisit_and_export(self):
        http = HTTP({ROOT: xml('urlset', PRODUCT)})
        collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        replacement = {'url': ROOT, 'text': xml('urlset', PRODUCT + '-changed'), 'headers': {}}
        with self.store.db:
            self.store.db.execute('UPDATE pages SET data=? WHERE run_id=? AND url=?',
                                  (encode(replacement), self.run_id, ROOT))
        result = self.export()
        self.assertEqual(result['sitemap']['status'], 'INCOMPLETE')
        self.assertFalse(result['sitemap']['checkpoint_matches'])
        self.assertEqual(result['audit_collection']['status'], 'COMPLETE')
        with self.assertRaisesRegex(FetchError, 'audit_checkpoint_mismatch'):
            collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        self.assertEqual(http.calls, [ROOT, ROBOTS])
        self.assertEqual(self.store.run(self.run_id)['sitemap_audit']['status'], 'INCOMPLETE')

    def test_missing_saved_xml_checkpoint_and_pending_audit_are_visible_offline(self):
        http = HTTP({ROOT: xml('sitemapindex', PRODUCTS), PRODUCTS: FetchError('timeout', True)})
        with self.assertRaises(FetchError):
            collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        with self.store.db:
            self.store.db.execute('DELETE FROM pages WHERE run_id=? AND url=?', (self.run_id, ROOT))
        result = self.export()
        self.assertEqual(result['sitemap']['missing_checkpoint_documents'], [ROOT])
        self.assertEqual(result['sitemap']['status'], 'INCOMPLETE')
        self.assertEqual(result['audit_collection']['pending'][0]['url'], PRODUCTS)
        self.assertEqual(result['audit_collection']['reason'], 'timeout')
        self.assertEqual(result['audit_collection']['policy']['mode'], 'cross_check')

    def test_export_limit_and_existing_output_do_not_leave_partial_or_overwrite(self):
        self.catalogue()
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            self.export(max_export_bytes=200)
        self.assertFalse((self.directory / 'audit').exists())
        self.assertFalse(list(self.directory.glob('.audit.*')))
        destination = self.directory / 'existing'
        destination.mkdir()
        (destination / 'keep').write_text('original')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.export('existing')
        self.assertEqual((destination / 'keep').read_text(), 'original')

    def test_malformed_sitemap_and_cycles_do_not_claim_complete_coverage(self):
        for name, value in [('invalid', '<html/>'), ('cycle', xml('sitemapindex', ROOT))]:
            with self.subTest(name=name):
                with self.store.db:
                    self.store.db.execute('DELETE FROM pages WHERE run_id=?', (self.run_id,))
                self.store.save_page(self.run_id, ROOT, Response(ROOT, value, {}))
                result = self.export(name)
                self.assertEqual(result['sitemap']['status'], 'INCOMPLETE')
                self.assertEqual(result['sitemap']['comparison_status'], 'NOT_AVAILABLE')

    def test_verify_export_binds_run_fingerprint_manifest_members_and_counts(self):
        self.catalogue()
        exported = self.export()
        result = verify_export(self.directory / 'audit', self.run_id, 'fingerprint')
        self.assertEqual(result['manifest_sha256'], exported['export']['manifest_sha256'])
        self.assertEqual(result['product_count'], 1)
        self.assertEqual(result['catalogue_status'], 'COMPLETE')
        self.assertEqual(result['sitemap_status'], 'NOT_CAPTURED')
        for run, fingerprint in [('another-run', 'fingerprint'), (self.run_id, 'wrong-fingerprint')]:
            with self.subTest(run=run, fingerprint=fingerprint), self.assertRaises(ValueError):
                verify_export(self.directory / 'audit', run, fingerprint)

    def test_verify_rejects_altered_or_missing_xml_and_member_symlinks(self):
        self.store.save_page(self.run_id, ROOT, Response(ROOT, xml('urlset', PRODUCT), {}))
        for change in ('altered', 'missing', 'symlink'):
            with self.subTest(change=change):
                exported = self.export(change)
                output = self.directory / change
                member = output / exported['documents'][0]['file']
                if change == 'altered':
                    member.write_text(xml('urlset', PRODUCT + '-altered'))
                elif change == 'missing':
                    member.unlink()
                else:
                    original = member.read_bytes()
                    member.unlink()
                    other = self.directory / 'outside.xml'
                    other.write_bytes(original)
                    member.symlink_to(other)
                with self.assertRaises(ValueError):
                    verify_export(output)

    def test_verify_rejects_traversal_and_modified_inventory_even_if_checksum_list_rewritten(self):
        self.catalogue()
        self.export('traversal')
        output = self.directory / 'traversal'
        (output / 'SHA256SUMS').write_text('0' * 64 + '  ../outside\n')
        with self.assertRaisesRegex(ValueError, 'Unsafe audit member path'):
            verify_export(output)
        exported = self.export('modified')
        output = self.directory / 'modified'
        inventory = output / 'products.jsonl'
        inventory.write_text(inventory.read_text().replace('Cotton shirt', 'Invented title'))
        sums = output / 'SHA256SUMS'
        prior = exported['files']['products.jsonl']['sha256']
        sums.write_text(sums.read_text().replace(prior, hashlib.sha256(inventory.read_bytes()).hexdigest()))
        with self.assertRaisesRegex(ValueError, 'manifest member mismatch'):
            verify_export(output)

    def test_verify_respects_total_byte_bound(self):
        self.catalogue()
        self.export()
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            verify_export(self.directory / 'audit', max_export_bytes=100)


    def test_successful_default_sitemap_also_captures_robots_advertised_roots(self):
        self.catalogue()
        extra = BASE + '/additional-products.xml'
        second_product = BASE + '/products/second-shirt'
        http = HTTP({ROOT: xml('urlset', PRODUCT),
                     ROBOTS: f'Sitemap: {extra}\n',
                     extra: xml('urlset', second_product)})
        state = collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        self.assertEqual(state['status'], 'COMPLETE')
        self.assertEqual(http.calls, [ROOT, ROBOTS, extra])
        result = self.export()
        self.assertEqual(result['sitemap']['roots'], sorted([ROOT, extra]))
        self.assertEqual(result['sitemap']['matched_urls'], [PRODUCT])
        self.assertEqual(result['sitemap']['sitemap_only_urls'], [second_product])
        self.assertEqual(result['sitemap']['comparison_status'], 'DIFFERENCE')
        self.assertEqual(self.store.page(self.run_id, ROBOTS)['text'], f'Sitemap: {extra}\n')

    def test_blocked_robots_keeps_successful_xml_but_never_claims_complete_cross_check(self):
        http = HTTP({ROOT: xml('urlset', PRODUCT), ROBOTS: FetchError('http_429', True, '60')})
        with self.assertRaises(FetchError):
            collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        state = self.store.run(self.run_id)['sitemap_audit']
        self.assertEqual(state['status'], 'INCOMPLETE')
        self.assertEqual(state['reason'], 'http_429')
        self.assertEqual(state['pending'][0]['url'], ROBOTS)
        self.assertEqual(state['documents'][0]['kind'], 'urlset')
        result = self.export()
        self.assertEqual(result['sitemap']['status'], 'INCOMPLETE')
        self.assertEqual(result['sitemap']['comparison_status'], 'NOT_AVAILABLE')
        self.assertEqual(result['audit_collection']['reason'], 'http_429')

    def test_missing_robots_is_explicit_and_missing_all_xml_never_satisfies_required_audit(self):
        http = HTTP({ROOT: xml('urlset', PRODUCT)})
        state = collect_sitemap_audit(http, self.store, self.run_id,
                                      policy={'mode': 'cross_check', 'audit_required': True})
        self.assertEqual(state['status'], 'COMPLETE')
        self.assertEqual(state['failures'], [{'url': ROBOTS, 'state': 'http_404'}])
        self.assertEqual(self.export()['audit_collection']['failures'], state['failures'])
        self.store.update(self.run_id, sitemap_audit=None)
        with self.store.db:
            self.store.db.execute('DELETE FROM pages WHERE run_id=?', (self.run_id,))
        state = collect_sitemap_audit(HTTP(), self.store, self.run_id,
                                      policy={'mode': 'cross_check', 'audit_required': True})
        self.assertEqual(state['status'], 'INCOMPLETE')
        self.assertEqual(state['reason'], 'sitemap_urlset_not_captured')
        self.assertEqual(state['failures'], [{'url': ROOT, 'state': 'http_404'},
                                             {'url': ROBOTS, 'state': 'http_404'}])

    def test_robots_pause_resumes_saved_response_and_preserves_default_sitemap(self):
        extra = BASE + '/additional-products.xml'
        http = HTTP({ROOT: xml('urlset', PRODUCT), ROBOTS: f'Sitemap: {extra}\n',
                     extra: xml('urlset', PRODUCT + '-two')})
        def budget():
            if self.store.page(self.run_id, ROBOTS):
                raise ScanPaused('site_pass_budget')
        with self.assertRaises(ScanPaused):
            collect_sitemap_audit(http, self.store, self.run_id, check_budget=budget,
                                  policy={'mode': 'cross_check'})
        state = self.store.run(self.run_id)['sitemap_audit']
        self.assertEqual([doc['requested_url'] for doc in state['documents']], [ROOT])
        self.assertEqual(state['pending'][0]['url'], ROBOTS)
        resumed = collect_sitemap_audit(http, self.store, self.run_id, policy={'mode': 'cross_check'})
        self.assertEqual(resumed['status'], 'COMPLETE')
        self.assertEqual(http.calls, [ROOT, ROBOTS, extra])
        self.assertTrue(next(doc for doc in resumed['documents'] if doc['kind'] == 'robots')['from_cache'])


    def test_required_xml_loss_after_scanner_complete_blocks_publication_and_attestation(self):
        from authority_batch import export_visit_audit, publish_one, verify_saved_audit
        from production_worker import attest_canary
        policy = normalize_audit_policy({'mode': 'cross_check', 'audit_required': True})
        self.run = self.store.create({'brand_id': '8', 'htt_id': '43', 'brand': 'Required audit',
                                     'domain': BASE, 'cpt': 'clothing', 'sitemap_audit_policy': policy},
                                    'fingerprint', 'scanner', 'reader', 'standard')
        self.run_id = self.run['scan_run_id']
        self.catalogue()
        self.store.update(self.run_id, scanner_status='SCANNER_COMPLETE',
                          publish_eligible=True, integrity_status='PASS', status='SCANNER_COMPLETE')
        collect_sitemap_audit(HTTP({ROOT: xml('urlset', PRODUCT)}), self.store, self.run_id, policy=policy)
        saved = export_visit_audit(self.store, self.run_id, self.directory)
        self.assertEqual(verify_saved_audit(self.store, saved)['sitemap_status'], 'COMPLETE')
        core_before = {key: saved[key] for key in ('scanner_status', 'publish_eligible', 'integrity_status', 'status')}
        with self.store.db:
            self.store.db.execute('DELETE FROM pages WHERE run_id=? AND url=?', (self.run_id, ROOT))
        saved = export_visit_audit(self.store, self.run_id, self.directory)
        self.assertEqual(saved['audit_export_status'], 'PASS')
        self.assertEqual(saved['audit_export_sitemap_status'], 'INCOMPLETE')
        self.assertEqual({key: saved[key] for key in core_before}, core_before)
        proof = verify_export(self.directory / saved['audit_export_path'], self.run_id, 'fingerprint')
        self.assertEqual(proof['sitemap_status'], 'INCOMPLETE')
        with self.assertRaisesRegex(ValueError, 'sitemap'):
            verify_saved_audit(self.store, saved)
        with patch('htt_publish.settings', side_effect=AssertionError('No publication transport')) as settings:
            with self.assertRaisesRegex(ValueError, 'sitemap'):
                publish_one(self.store, self.run_id, {}, self.directory / 'publication.lock')
            settings.assert_not_called()
        # The existing canonical gate is independent; isolate the new proof gate.
        with patch('htt_publish.assert_publishable'):
            with self.assertRaisesRegex(ValueError, 'sitemap'):
                attest_canary(self.store, self.run_id, {'sitemap_audit_policy': policy})


if __name__ == '__main__':
    unittest.main()
