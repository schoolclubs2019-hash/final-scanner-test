"""Failure-path regressions for the production repair review."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from authority_batch import prepare, scan_one
from authority_http import AuthorityHTTP
from catalogue_discovery import FetchError, Response
from rendered_evidence import BrowserRenderer
from scanner_pipeline import run_brand, pipeline_fingerprint, SCANNER_VERSION
from scanner_store import Store
from throne_read import READER_VERSION
from throne_standard import VERSION
from test_authority_batch import brand
from test_operational_canary import Clock
from test_pipeline import page, product

ROOT = Path(__file__).resolve().parents[2]


class HTTP:
    base = 'https://brand.test'
    interval = .01
    validate = staticmethod(lambda url: url)

    def __init__(self, get):
        self.get = get


class ReviewRepairTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'test.db')
        self.addCleanup(self.store.close)
        self.brand = brand(1, 'One', 'https://brand.test')
        self.store.enqueue([self.brand])
        self.rid = self.store.create(self.brand, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']

    def test_method_not_allowed_and_server_errors_fall_through_only_before_emission(self):
        from catalogue_discovery import Discovery, PublicHTTP
        from test_production_repair import Probe
        for state in ('http_405', 'http_501', 'http_505', 'http_599'):
            with self.subTest(state=state):
                self.assertEqual([p['product_id'] for p in Discovery(Probe(state)).enumerate()], ['2'])
                partial = Probe(state, partial=True)
                with self.assertRaisesRegex(FetchError,state):
                    list(Discovery(partial).enumerate())
                self.assertFalse(any('/wp-json/' in url for url in partial.calls))
                with patch('catalogue_discovery.socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]):
                    http = PublicHTTP('https://brand.test/pages/cotton')
                    resolved = http.resolve(get=Mock(side_effect=FetchError(state,True)))
                self.assertEqual(resolved['crawl_origin'],'https://brand.test')

    def test_inci_catalogue_files_close_after_loading(self):
        import inci_read
        for load, expected in ((inci_read._load_catalog, {'aqua':0}),
                               (inci_read._load_hazards, [('aqua',0)])):
            source = io.StringIO('Aqua: 0\n')
            with patch('builtins.open',return_value=source):
                self.assertEqual(load(),expected)
            self.assertTrue(source.closed)

    def test_stale_and_ambiguous_candidates_are_reconciled_rejections(self):
        urls = ['https://brand.test/products/' + str(i) for i in range(4)]
        def fetch(url):
            index = urls.index(url)
            if index < 2:
                raise FetchError(('http_404', 'http_410')[index])
            body = page(url)
            if index == 2:
                body += '<script type="application/ld+json">' + json.dumps({'@type':'Product','url':url,'name':'Different Product','sku':'other'}) + '</script>'
            return Response(url, body, {})
        with patch('scanner_pipeline.Discovery.enumerate', return_value=iter({'url':u} for u in urls)):
            run = run_brand(self.store, self.rid, HTTP(fetch))
        self.assertTrue(run['discovery_complete'])
        self.assertEqual(run['catalogue_discovered'], 4)
        self.assertEqual(run['catalogue_confirmed'], 1)
        self.assertEqual(run['discovery_rejected'], {'http_404':1,'http_410':1,'ambiguous_product_identity':1})
        self.assertEqual(run['discovery_reconciliation'], 'PASS')
        self.assertEqual(run['technical_failures'], 0)

    def test_candidate_transient_and_security_failures_still_hold_discovery(self):
        for state, retryable in [('timeout',True),('blocked',False),('offsite_url',False),('unsafe_address',False)]:
            with self.subTest(state=state):
                rid = self.store.create(self.brand, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
                with patch('scanner_pipeline.Discovery.enumerate', return_value=iter([{'url':'https://brand.test/products/one'}])):
                    run = run_brand(self.store, rid, HTTP(Mock(side_effect=FetchError(state,retryable))))
                self.assertFalse(run['discovery_complete'])
                self.assertFalse(run.get('publish_eligible'))
                self.assertEqual(run['error'], state)

    def test_domain_deferral_keeps_products_pending_and_resume_reads_only_pending(self):
        clock = Clock()
        calls = []
        limited = True
        def fetch(url):
            calls.append(url)
            if url.endswith('/products/p2') and limited:
                raise FetchError('http_429', True, '86400')
            return Response(url, page(url), {})
        raw = HTTP(fetch)
        http = AuthorityHTTP(raw,self.store,self.rid,clock=clock.time,sleep=clock.sleep,jitter=lambda a,b:0)
        with patch('scanner_pipeline.Discovery.enumerate', return_value=iter([product(i) for i in range(1,4)])):
            run = run_brand(self.store,self.rid,http)
        self.assertEqual(run['status'],'DEFERRED')
        rows = list(self.store.iter_products(self.rid))
        self.assertIsNotNone(rows[0]['result'])
        self.assertTrue(all(r['result'] is None for r in rows[1:]))
        self.assertFalse(run.get('publish_eligible'))
        self.assertGreater(run['worker_retry_at'],clock.time())
        self.assertFalse(any(u.endswith('/products/p3') for u in calls))
        self.assertLess(sum(clock.sleeps), 10)
        limited = False
        clock.at = run['worker_retry_at'] + 1
        resumed = AuthorityHTTP(raw,self.store,self.rid,clock=clock.time,sleep=clock.sleep,jitter=lambda a,b:0)
        run = run_brand(self.store,self.rid,resumed,retry=True)
        self.assertEqual(run['scanner_status'],'SCANNER_COMPLETE')
        self.assertEqual((run['processed'],run['technical_failures']), (3,0))
        self.assertEqual(calls.count('https://brand.test/products/p1'),1)

    def test_resolution_failure_is_isolated_and_cannot_authorize_crawl(self):
        records = [brand(2,'Bad','https://bad.test'),brand(3,'Good','https://good.test')]
        records[0]['crawl_origin'] = 'https://unrelated.test'
        records[0]['storefront_resolution'] = {'allowed_hosts':['unrelated.test']}
        queue = self.root / 'queue.json'
        queue.write_text(json.dumps(records))
        def client(url):
            if 'bad.test' in url:
                raise FetchError('unsafe_address')
            return SimpleNamespace(resolve=lambda: {'crawl_origin':url,'allowed_hosts':['good.test']})
        with patch('authority_batch.PublicHTTP',side_effect=client):
            result = prepare(queue,self.root/'shards',workers=2,resolve=True)
        self.assertEqual(result['total'],2)
        rows = []
        for i in (1,2):
            rows.extend(json.loads((self.root/'shards'/f'W{i}.json').read_text()))
        bad = next(r for r in rows if r['brand_id']=='2')
        self.assertEqual(bad['storefront_resolution_error'],'unsafe_address')
        self.assertNotIn('crawl_origin',bad)
        self.assertNotIn('storefront_resolution',bad)
        with patch('authority_batch.PublicHTTP',side_effect=FetchError('unsafe_address')), patch('authority_batch.run_brand') as run:
            held = scan_one(self.store,self.brand,self.rid)
        run.assert_not_called()
        self.assertEqual(held['status'],'REPAIR_REQUIRED')
        self.assertFalse(held['publish_eligible'])

    def test_browser_rejects_missing_response_and_unsafe_redirect_chain_before_dom(self):
        for target, middle, state in [('https://evil.test/x',None,'offsite_url'),('http://127.0.0.1/x',None,'offsite_url'),
                                       ('https://brand.test/p','https://evil.test/x','offsite_url'),
                                       (None,None,'render_http_error')]:
            with self.subTest(target=target,middle=middle):
                pw = Mock()
                browser = pw.chromium.launch.return_value
                page_mock = browser.new_context.return_value.new_page.return_value
                page_mock.url = 'https://brand.test/p'
                request = SimpleNamespace(url=target,redirected_from=SimpleNamespace(url=middle,redirected_from=None) if middle else None)
                page_mock.goto.return_value = SimpleNamespace(url=target,request=request,status=200) if target else None
                cm = Mock()
                cm.__enter__ = Mock(return_value=pw)
                cm.__exit__ = Mock(return_value=False)
                with patch('playwright.sync_api.sync_playwright',return_value=cm), patch('catalogue_discovery.socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]):
                    with self.assertRaisesRegex(FetchError,state):
                        BrowserRenderer()('https://brand.test/p')
                page_mock.content.assert_not_called()
                page_mock.locator.assert_not_called()

    def test_browser_blocks_redirect_before_private_or_offsite_network_request(self):
        from test_pipeline import FixtureServer
        with FixtureServer() as server:
            handler = server.server.RequestHandlerClass
            original = handler.do_GET
            forbidden = []
            def get(request):
                if request.path == '/redirect':
                    request.send('',302,{'Location':server.base+'/hop'})
                elif request.path == '/hop':
                    request.send('',302,{'Location':server.base.replace('127.0.0.1','localhost')+'/forbidden'})
                elif request.path == '/safe-redirect':
                    request.send('',302,{'Location':server.base+'/javascript'})
                elif request.path == '/forbidden':
                    forbidden.append(request.path)
                    request.send('<main>private evidence</main>')
                else:
                    original(request)
            with patch.object(handler,'do_GET',get):
                with self.assertRaisesRegex(FetchError,'offsite_url'):
                    BrowserRenderer(allow_local=True)(server.base+'/redirect')
                self.assertEqual(forbidden,[])
                html = BrowserRenderer(allow_local=True)(server.base+'/safe-redirect')
                self.assertIn('100% cotton',html)

    def test_offsite_asset_redirect_is_blocked_without_losing_product_page(self):
        from test_pipeline import FixtureServer
        with FixtureServer() as server:
            handler = server.server.RequestHandlerClass
            original = handler.do_GET
            forbidden = []
            def get(request):
                if request.path == '/product-with-asset':
                    request.send('<main>100% cotton</main><script src="/asset-redirect"></script><iframe src="/asset-redirect"></iframe>')
                elif request.path == '/asset-redirect':
                    request.send('', 302, {'Location': server.base.replace('127.0.0.1', 'localhost') + '/forbidden'})
                elif request.path == '/forbidden':
                    forbidden.append(request.path)
                    request.send('')
                else:
                    original(request)
            with patch.object(handler, 'do_GET', get):
                html = BrowserRenderer(allow_local=True)(server.base + '/product-with-asset')
                self.assertIn('100% cotton', html)
                self.assertEqual(forbidden, [])

    def test_api_startup_never_prints_key_fragments(self):
        import throne_api
        output = io.StringIO()
        with patch.object(throne_api,'API_KEY','do-not-log-credential'), patch.object(throne_api,'ThreadingHTTPServer') as server, patch('sys.argv',['throne_api.py']),contextlib.redirect_stdout(output):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            throne_api.main()
        self.assertIn('Bearer (configured)',output.getvalue())
        self.assertNotIn('do-not',output.getvalue())

    def test_generic_derived_origin_never_invents_an_ingredient_score(self):
        from inci_read import score_inci
        for text in ('Plant based surfactants','Aqua, coconut-derived cleansers','essential oil blend'):
            result = score_inci('Fixture',text)
            self.assertEqual(result['assessment_status'],'unresolved')
            self.assertIsNone(result['ingredient_natural_pct'])
            self.assertTrue(result['naturally_derived_ingredients'])

    def test_reader_suite_is_discoverable_and_failure_is_not_ignored(self):
        import test_read
        self.assertGreater(unittest.defaultTestLoader.loadTestsFromModule(test_read).countTestCases(),0)
        with patch.object(test_read.R,'classify',return_value=('wrong','','')), contextlib.redirect_stdout(io.StringIO()):
            result = unittest.TestResult()
            test_read.ReaderRegressionTest('test_reader_regressions').run(result)
        self.assertEqual(len(result.failures),1)

    def test_aws_test_restores_preexisting_auth_environment(self):
        from test_aws_reader_lambda import AwsReaderLambdaTest
        with patch.dict(os.environ,{'THRONE_API_KEY':'original-fixture-value'}):
            case = AwsReaderLambdaTest('test_health_exposes_fingerprint')
            case.setUp()
            self.assertEqual(os.environ['THRONE_API_KEY'],'test-secret')
            case.doCleanups()
            self.assertEqual(os.environ['THRONE_API_KEY'],'original-fixture-value')

    def test_each_artifact_directory_fails_the_ci_guard(self):
        script = ROOT/'scripts/check_rebuildable_artifacts.sh'
        self.assertEqual(subprocess.run(['bash',str(script)],cwd=self.root,capture_output=True).returncode,0)
        for name in ('_cache','_aws-reader-build'):
            artifact = self.root/name/'file'
            artifact.parent.mkdir()
            artifact.write_text('generated')
            self.assertEqual(subprocess.run(['bash',str(script)],cwd=self.root,capture_output=True).returncode,1)
            artifact.unlink()

    def test_privileged_aws_workflow_refuses_fork_and_pr_validation(self):
        workflow = (ROOT/'.github/workflows/deploy-scanner.yml').read_text()
        condition = next(line.strip()[4:] for line in workflow.splitlines() if line.strip().startswith('if: '))
        expression = condition.replace('&&',' and ').replace('||',' or ')
        for event, source, conclusion, expected in [('push','myidealwrld/htt-scanner','success',True),
                                                  ('pull_request','myidealwrld/htt-scanner','success',False),
                                                  ('push','fork/htt-scanner','success',False),
                                                  ('push','myidealwrld/htt-scanner','failure',False)]:
            github = SimpleNamespace(repository='myidealwrld/htt-scanner',event_name='workflow_run',
                event=SimpleNamespace(workflow_run=SimpleNamespace(event=event,conclusion=conclusion,head_repository=SimpleNamespace(full_name=source))))
            self.assertEqual(eval(expression,{'__builtins__':{},'github':github}),expected)
        azure = (ROOT/'.github/workflows/azure-production-canary.yml').read_text()
        self.assertNotIn('\n  push:',azure)
        self.assertIn('\n  workflow_dispatch:',azure)


if __name__ == '__main__':
    unittest.main()
