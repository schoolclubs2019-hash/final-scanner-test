import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from catalogue_discovery import Discovery, FetchError, PublicHTTP, Response, capture, ProductHTML
from department_readers import route_product
from publication_parity import FIELDS, SURFACES, ContractError, StagingTransport, compare, parity_check, propagate, read_client_configuration, resolve_idealwrld_context
from scanner_pipeline import SCANNER_VERSION, catalogue_change, pipeline_fingerprint, require_canaries, run_brand, select_queue
from scanner_store import Store
from scan_contract import scan
from htt_publish import assert_publishable
from text_normalizer import normalize_row
from throne_read import READER_VERSION
from throne_standard import VERSION

FIXTURES = Path(__file__).with_name('fixtures')

def product(number=1, base='https://brand.test', **extra):
    return {'product_id': str(number), 'title': 'Blanket Shirt', 'url': f'{base}/products/p{number}',
            'body_html': '<p>100% organic cotton</p>', **extra}

def page(url, composition='100% organic cotton'):
    return '<script type="application/ld+json">' + json.dumps({'@type': 'Product', 'url': url, 'name': 'Blanket Shirt'}) + '</script><main><details><summary>Materials &amp; Care</summary><p>' + composition + '</p></details></main>'

class FixtureServer:
    def __init__(self, total=3):
        self.total, self.calls = total, []
        self.current = {'scan_run_id': 'last-good', 'score': 4}
        self.staged, self.rows, self.fail, self.stale, self.fixture = None, {}, None, None, False
        self.block_product_pages = False
        self.challenge_product_pages = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def send(self, value, status=200, headers=None):
                data = value.encode() if isinstance(value, str) else json.dumps(value).encode()
                self.send_response(status)
                for k, v in (headers or {}).items(): self.send_header(k, v)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if isinstance(value,str) else 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_GET(self):
                owner.calls.append(('GET', self.path))
                path, query = urlsplit(self.path).path, parse_qs(urlsplit(self.path).query)
                if path.startswith('/fault/'):
                    self.send('upstream failure',int(path.rsplit('/',1)[-1])); return
                if path in {'/products.json','/wp-json/wc/store/v1/products'}:
                    shopify=path=='/products.json'; size=250 if shopify else 100; index=int(query.get('page',['1'])[0])
                    values=[{'id':i,'handle':'p'+str(i),'title':'Blanket Shirt','name':'Shirt','permalink':owner.base+'/products/p'+str(i),'body_html':'100% cotton','description':'100% cotton'} for i in range((index-1)*size+1,min(index*size,owner.total)+1)]
                    self.send({'products':values} if shopify else values,headers={} if shopify else {'X-WP-TotalPages':str((owner.total+99)//100)}); return
                if path=='/sitemap.xml':
                    self.send(f'<sitemapindex><sitemap><loc>{owner.base}/product-sitemap.xml</loc></sitemap></sitemapindex>'); return
                if path=='/product-sitemap.xml':
                    self.send(f'<urlset><url><loc>{owner.base}/products/p1</loc></url><url><loc>{owner.base}/pages/about</loc></url></urlset>'); return
                if path.startswith('/products/'):
                    if owner.block_product_pages:
                        self.send({'error':'blocked'},403); return
                    if owner.challenge_product_pages:
                        self.send('<html><title>Just a moment...</title><body>Verify you are human</body></html>'); return
                    self.send(page(owner.base+path)); return
                if path=='/kent-panel':
                    opened = query.get('open', ['0'])[0] == '1'
                    html = (FIXTURES/'kent-boxer-panel.reconstructed.html').read_text()
                    html = html.replace('<details ', '<details ' + ('open ' if opened else '') +
                        "ontoggle=\"this.querySelector('.accordion__content').textContent=this.open?'100% organic supima cotton':''\" ", 1)
                    html = html.replace('</summary>', '<svg><title>Expand</title></svg></summary>')
                    self.send(html); return
                if path=='/javascript':
                    self.send('<main><button aria-controls="materials" aria-expanded="false" onclick="document.getElementById(\'materials\').innerHTML=\'100% cotton\';this.setAttribute(\'aria-expanded\',\'true\')">Materials</button><div id="materials"></div><div class="recommendations">100% polyester</div></main>'); return
                if path.startswith('/config/'):
                    self.send({'environment':'staging','fixture_mode':owner.fixture,'HTT_API_URL':owner.base,'HTT_READER_URL':owner.base+'/reader'}); return
                if path.startswith('/surface/'):
                    name=path.split('/')[2]
                    if owner.fail==name: self.send({},503); return
                    data=dict(owner.current)
                    if name.endswith('_sku'): data.update(owner.rows.get(path.rsplit('/',1)[-1],{}))
                    if owner.stale==name: data['scan_run_id']='stale-generation'
                    self.send('<script id="htt-result" type="application/json">'+json.dumps(data)+'</script>' if name=='htt_website' else data); return
                self.send('not found',404)
            def do_POST(self):
                owner.calls.append(('POST',self.path)); body=json.loads(self.rfile.read(int(self.headers['Content-Length']))); action=self.path.strip('/')
                if owner.fail==action: self.send({'error':'injected_failure'},503); return
                if action=='stage':
                    owner.staged=body['canonical']; self.send({'scan_run_id':owner.staged['scan_run_id'],'status':'staged'})
                elif action=='products':
                    for row in body['rows']: owner.rows[row['product_id']]={key:row.get(key) for key in ('product_id','sku','score','terminal_state')}
                    self.send({'accepted':len(body['rows'])})
                elif action=='commit':
                    owner.current=dict(owner.staged); self.send({'scan_run_id':body['scan_run_id'],'status':'published','atomic':True,'cache_invalidated':True,'sync_enqueued':True})
                elif action=='sync': self.send({'ok':True,'status':'success'})
                else: self.send({},404)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
    def __enter__(self): self.thread.start(); return self
    def __exit__(self,*args): self.server.shutdown(); self.server.server_close(); self.thread.join()
    def http(self): return PublicHTTP(self.base,retries=0,allow_local=True,requests_per_second=100000)
    def config(self):
        return {'environment':'staging','allowed_hosts':['127.0.0.1'],'allow_local_http':True,
                'publication':{'contract':'htt-generations-v1',**{a:{'url':self.base+'/'+a} for a in ('stage','products','commit')}},
                'sync':{'url':self.base+'/sync'},'surfaces':{n:{'url':self.base+'/surface/'+n+('/{product_id}' if n.endswith('_sku') else ''), **({'format':'html','script_id':'htt-result'} if n=='htt_website' else {})} for n in SURFACES},
                'client_configuration':{n:{'url':self.base+'/config/'+n,'expected_api_url':self.base,'expected_reader_url':self.base+'/reader'} for n in ('shopify','wix')}}

class PipelineTest(unittest.TestCase):
    def setUp(self): self.temp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.temp.name)/'state.db')
    def tearDown(self): self.store.close(); self.temp.cleanup()
    def new_run(self,domain='https://brand.test',**extra):
        brand={'brand_id':'42','htt_id':'htt-42','brand':'Example','domain':domain,'cpt':'clothes','adapter':'shopify',**extra}
        self.store.enqueue([brand]); return self.store.create(brand,pipeline_fingerprint(),SCANNER_VERSION,READER_VERSION,VERSION)['scan_run_id']
    def local_run(self,server,**extra):
        rid=self.new_run(server.base,**extra); return rid,run_brand(self.store,rid,server.http())
    def propagate(self,server,rid): return propagate(self.store,rid,server.config(),StagingTransport(server.config()),[p['result'] for p in self.store.products(rid)])
    def test_shopify_paginates_to_exhaustion(self):
        with FixtureServer(601) as s:
            self.assertEqual(len(list(Discovery(s.http()).enumerate('shopify'))),601); self.assertIn(('GET','/products.json?limit=250&page=4'),s.calls)
    def test_woocommerce_pagination(self):
        with FixtureServer(205) as s:
            self.assertEqual(len(list(Discovery(s.http()).enumerate('woocommerce'))),205); self.assertEqual(len(s.calls),3)

    def test_catalogue_endpoints_use_storefront_origin_not_queue_landing_path(self):
        class HTTP:
            base='https://brand.test/pages/certifications'
            validate=lambda self,url:url
            def __init__(self): self.calls=[]
            def get(self,url):
                self.calls.append(url)
                if url.endswith('/products.json?limit=250&page=1'):
                    return Response(url,json.dumps({'products':[{'id':1,'handle':'p1'}]}),{})
                if url.endswith('/products.json?limit=250&page=2'):
                    return Response(url,json.dumps({'products':[]}),{})
                raise AssertionError(url)
        http=HTTP()
        rows=list(Discovery(http).commerce('shopify'))
        self.assertEqual(rows[0]['url'],'https://brand.test/products/p1')
        self.assertEqual(http.calls[0],'https://brand.test/products.json?limit=250&page=1')

    def test_auto_discovery_falls_back_from_initial_410(self):
        class HTTP:
            base='https://brand.test'
            validate=lambda self,url:url
            def __init__(self): self.calls=[]
            def get(self,url):
                self.calls.append(url)
                if '/products.json' in url:
                    raise FetchError('http_410')
                if '/wp-json/wc/store/v1/products' in url:
                    raise FetchError('http_404')
                if url.endswith('/sitemap.xml'):
                    return Response(url,'<urlset><url><loc>https://brand.test/products/p1</loc></url></urlset>',{})
                raise AssertionError(url)
        http=HTTP()
        rows=list(Discovery(http).enumerate('auto'))
        self.assertEqual([row['url'] for row in rows],['https://brand.test/products/p1'])
        self.assertTrue(any('/products.json' in url for url in http.calls))

    def test_auto_discovery_never_falls_back_after_partial_410(self):
        class HTTP:
            base='https://brand.test'
            validate=lambda self,url:url
            def get(self,url):
                if 'page=1' in url:
                    return Response(url,json.dumps({'products':[{'id':1,'handle':'p1'}]}),{})
                if 'page=2' in url:
                    raise FetchError('http_410')
                raise AssertionError(url)
        with self.assertRaisesRegex(FetchError,'http_410'):
            list(Discovery(HTTP()).enumerate('auto'))

    def test_sitemap_discovery_uses_robots_declared_market_index(self):
        class HTTP:
            base='https://brand.test/US/store'
            validate=lambda self,url:url
            def __init__(self): self.calls=[]
            def get(self,url):
                self.calls.append(url)
                if url == 'https://brand.test/sitemap.xml':
                    raise FetchError('http_404')
                if url == 'https://brand.test/robots.txt':
                    return Response(url,'Sitemap: https://brand.test/AU/sitemap_index.xml\nSitemap: https://brand.test/US/sitemap_index.xml\n',{})
                if url == 'https://brand.test/US/sitemap_index.xml':
                    return Response(url,'<urlset><url><loc>https://brand.test/US/products/p1</loc></url></urlset>',{})
                raise AssertionError(url)
        http=HTTP()
        rows=list(Discovery(http).sitemaps())
        self.assertEqual([row['url'] for row in rows],['https://brand.test/US/products/p1'])
        self.assertIn('https://brand.test/robots.txt',http.calls)

    def test_select_queue_prefers_stable_brand_id_and_rejects_ambiguous_names(self):
        queue=[
            ({'brand_id':'1','brand':'Same','slug':'same-one'},None),
            ({'brand_id':'2','brand':'Same','slug':'same-two'},None),
        ]
        selected=select_queue(queue,'2')
        self.assertEqual([row[0]['brand_id'] for row in selected],['2'])
        with self.assertRaisesRegex(ValueError,'Ambiguous brand selector'):
            select_queue(queue,'Same')

    def test_out_of_scope_products_reconcile_but_do_not_enter_canonical_report(self):
        rid = self.new_run(manual_catalogue_complete=True, slug='example')
        class HTTP:
            base = 'https://brand.test'
            validate = lambda self, url: url
            def get(self, url):
                raise AssertionError('manual evidence must not fetch')
        items = [
            product(1, title='Essential Bra', body_html='', material='81% merino wool, 14% nylon, 5% elastane',
                    manual_evidence_verified=True),
            product(2, title='Essential Laundry Bag', body_html='Made of GOTS-certified organic cotton fabric.',
                    manual_evidence_verified=True),
            product(3, title='Lint Roller', body_html='', manual_evidence_verified=True),
        ]
        result = run_brand(self.store, rid, HTTP(), manual=items)
        self.assertEqual(result['catalogue_confirmed'], 3)
        self.assertEqual(result['processed'], 3)
        self.assertEqual(result['report_processed'], 1)
        self.assertEqual(result['excluded_accessory'], 1)
        self.assertEqual(result['excluded_nontextile'], 1)
        self.assertEqual(result['excluded'], 2)
        self.assertEqual(result['canonical']['catalogue_total'], 1)
        self.assertEqual(result['canonical']['processed_total'], 1)
        self.assertEqual(result['canonical']['sku_count'], 1)
        self.assertEqual(result['canonical']['catalogue_identified_total'], 3)
        self.assertEqual(result['canonical']['excluded_accessory_count'], 1)
        self.assertEqual(result['canonical']['excluded_nontextile_count'], 1)
        self.assertEqual(result['technical_failures'], 0)
        self.assertTrue(result['publish_eligible'], result.get('publication_blockers'))
        assert_publishable(result)

    def test_blocked_product_html_uses_first_party_catalogue_evidence(self):
        with FixtureServer(3) as s:
            s.block_product_pages = True
            rid, result = self.local_run(s, adapter='shopify')
            self.assertEqual(result['processed'], 3)
            self.assertEqual(result['technical_failures'], 0)
            self.assertEqual(result['scored'], 3)
            self.assertTrue(result['publish_eligible'])
            rows = list(self.store.result_rows(rid))
            self.assertTrue(all(row['terminal_state'] == 'scored' for row in rows))

    def test_challenge_html_uses_first_party_catalogue_evidence(self):
        with FixtureServer(3) as s:
            s.challenge_product_pages = True
            rid, result = self.local_run(s, adapter='shopify')
            self.assertEqual(result['processed'], 3)
            self.assertEqual(result['technical_failures'], 0)
            self.assertEqual(result['scored'], 3)
            self.assertTrue(result['publish_eligible'])
            products = list(self.store.iter_products(rid))
            self.assertTrue(all(p['item'].get('page_fetch_status') == 'blocked' for p in products))
            self.assertTrue(all(p['result']['terminal_state'] == 'scored' for p in products))
    def test_sitemap_identity_and_rejections(self):
        with FixtureServer() as s:
            _,r=self.local_run(s,adapter='sitemap'); self.assertEqual((r['catalogue_discovered'],r['catalogue_confirmed'],r['processed']),(2,1,1))
    def test_jsonld_must_match(self): self.assertEqual(ProductHTML(page('https://brand.test/products/other')).evidence(product()['url'])[0],[])
    def test_outerknown_deep_sections(self):
        item=product(body_html=''); out=capture(item,Response(item['url'],page(item['url']),{})); row=scan([out],{},True)['read'][0]
        self.assertEqual(row['terminal_state'],'scored'); self.assertEqual(row['department'],'clothing')
    def test_outerknown_historical_shape(self):
        # Synthetic historical distribution; original historical HTML is unavailable.
        items=[product(i) for i in range(1,109)]
        for i in range(6,94): items[i].update(body_html='',fetch_status='extractor_unresolved')
        items[107]['fetch_status']='http_404'; result=scan(items,{'catalogue':{'complete':True,'confirmed_total':108}},True)
        self.assertEqual(len(result['read']),108); self.assertTrue(all(r['terminal_state'] for r in result['read']))
        self.assertFalse(any(r['terminal_state'] in {'category_unresolved','undisclosed'} for r in result['read'])); self.assertFalse(result['brand']['publish_eligible'])
    def test_story_recorded_sources(self):
        obj=json.loads((FIXTURES/'story-product.recorded.json').read_text())['products'][0]
        item=product(obj['id'],url='https://www.storymfg.com/products/'+obj['handle'],title=obj['title'],body_html=obj['body_html'])
        objects,_=ProductHTML((FIXTURES/'story-page.recorded.html').read_text()).evidence(item['url']);self.assertEqual(len(objects),1)
        with self.assertRaisesRegex(FetchError,'render_required'):capture(item,Response(item['url'],(FIXTURES/'story-page.recorded.html').read_text(),{}))
    def test_story_cross_sell_and_noise(self):
        for text in ('Pair this with linen shorts','Model wears cotton trousers','Complete the look with our denim jacket','You may also like 100% cotton'):
            row=scan([product(body_html='<p>100% polyester</p><p>'+text+'</p>')],{},True)['read'][0]; self.assertEqual(row['band'],'synthetic',text)
        item=product(body_html=''); html='<div class="recommendations"><h2>Composition</h2>100% polyester</div><details><summary>Materials</summary>100% cotton</details>'
        self.assertEqual(scan([capture(item,Response(item['url'],html,{}))],{},True)['read'][0]['band'],'plant')
    def test_kent_fit_fabric_static_panel_is_product_evidence(self):
        html = (FIXTURES/'kent-boxer-panel.reconstructed.html').read_text()
        # Stronger percentages outside the product panel must never be pulled in.
        html += '<div class="product-graphic">100% polyester</div>'
        html += '<div class="recommendations"><details><summary>Fit &amp; Fabric</summary>100% nylon</details></div>'
        item = product(body_html='', title="Men's Organic Supima Cotton Breather Boxer")
        out = capture(item, Response(item['url'], html, {}))
        row = scan([out], {}, True)['read'][0]
        self.assertEqual(row['terminal_state'], 'scored')
        self.assertEqual(row['band'], 'plant')
        sections = [s for s in out['evidence_sources'] if s['source'].startswith('product_section:')]
        self.assertEqual(len(sections), 1)
        self.assertIn('100% organic supima cotton', sections[0]['raw_original'])
        self.assertNotIn('polyester', out['page_text'])
        self.assertNotIn('nylon', out['page_text'])

    def test_kent_fit_fabric_rendered_panel(self):
        html = (FIXTURES/'kent-boxer-panel.reconstructed.html').read_text()
        item = product(body_html='Organic cotton jersey', title='Cotton Boxer')
        out = capture(item, Response(item['url'], '<main>Loading</main>', {}), lambda _: html)
        row = scan([out], {}, True)['read'][0]
        self.assertEqual(row['terminal_state'], 'scored')
        self.assertEqual(row['band'], 'plant')
        self.assertTrue(any(s['source']=='rendered:product_section:fit & fabric' for s in out['evidence_sources']))

    def test_kent_nested_heading_handles_icon_and_ignores_promo(self):
        html = (FIXTURES/'kent-boxer-panel.reconstructed.html').read_text()
        html = html.replace('</summary>', '<svg><title>Expand</title></svg></summary>')
        html += '<section><h2>ORGANIC SUPIMA COTTON</h2><p>100% polyester</p></section>'
        _, sources = ProductHTML(html).evidence('https://brand.test/products/p1')
        self.assertEqual(len(sources), 1)
        self.assertIn('100% organic supima cotton', sources[0]['raw_original'])
        self.assertNotIn('polyester', sources[0]['raw_original'])

    def test_missing_extractor_not_undisclosed(self):
        item=product(body_html=''); response=Response(item['url'],page(item['url'],'Wash cold'),{})
        with self.assertRaisesRegex(FetchError,'render_required'):capture(item,response)
        out=capture(item,response,lambda _:page(item['url'],'Wash cold')); self.assertEqual(scan([out],{},True)['read'][0]['terminal_state'],'undisclosed')
        with self.assertRaisesRegex(FetchError,'extractor_unresolved'):capture(item,Response(item['url'],'<main>Loading</main>',{}),lambda _:'<main>Loading</main>')
    def test_category_precedence(self):
        for title,dep in [('Blanket Shirt','clothing'),('Blanket Jacket','clothing'),('Blanket Scarf','clothing'),('Shirt Jacket','clothing'),('Throw Blanket','bedding'),('Bed Blanket','bedding')]:self.assertEqual(route_product({'title':title})[0],dep)
        self.assertEqual(route_product({'title':'Blanket Shirt','product_type':'bedding'})[0],'bedding')
        self.assertEqual(route_product({'title':'Blanket Shirt','cpt':'bedding','department':'clothing'})[0],'bedding')
    def test_components_percentages_unicode(self):
        item=normalize_row(product(composition='Shell: 100% cotton; Lining: 100% Poliu00e9ster',title='Womenu2019s Jacket'))
        result=scan([item],{'catalogue':{'complete':True,'confirmed_total':1}},True); row=result['read'][0]
        self.assertEqual([c['percentage_total'] for c in row['components'][:2]],[100,100]); self.assertIn('Poliu00e9ster',row['raw_original']['composition']); self.assertFalse(result['brand']['publish_eligible'])
        self.assertEqual(row['components'][0]['materials'][0]['classification'], 'plant')
        self.assertEqual(row['components'][1]['materials'][0]['classification'], 'synthetic')
        for composition in ('60% cotton, 30% linen','110% cotton'):
            row=scan([product(composition=composition,body_html='')],{},True)['read'][0]; self.assertIn(row['terminal_state'],{'partial_composition','evidence_conflict'}); self.assertIsNone(row['score'])
    def test_http_faults(self):
        with FixtureServer() as s:
            for code,state in [(404,'http_404'),(429,'http_429'),(500,'http_500'),(403,'blocked')]:
                with self.assertRaises(FetchError) as exc:s.http().get(s.base+'/fault/'+str(code))
                self.assertEqual(exc.exception.state,state)
        with self.assertRaisesRegex(FetchError,'bad_json'):Response('','{',{}).json()
        for fault in ('timeout','connection_error','blocked','reader_error','http_404','http_500','http_429'):
            row=scan([product(fetch_status=fault)],{},True)['read'][0]; self.assertIsNone(row['score']); self.assertNotEqual(row['terminal_state'],'undisclosed')

    def test_idealwrld_id_resolves_from_stable_app_catalogue_identity(self):
        canonical={'slug':'example','htt_id':'htt-42','idealwrld_id':None}
        config={'surfaces':{'app_catalogue':{'url':'https://iw.test/brands/{slug}'}}}
        class Transport:
            def request(self,url,**kwargs):
                self.url=url
                return {'id':777,'slug':'example','htt_id':'htt-42'}
        transport=Transport()
        resolved=resolve_idealwrld_context(canonical,config,transport)
        self.assertEqual(resolved['idealwrld_id'],777)
        self.assertEqual(transport.url,'https://iw.test/brands/example')

    def test_idealwrld_id_resolution_rejects_wrong_stable_identity(self):
        canonical={'slug':'example','htt_id':'htt-42','idealwrld_id':None}
        config={'surfaces':{'app_catalogue':{'url':'https://iw.test/brands/{slug}'}}}
        class Transport:
            def request(self,url,**kwargs):
                return {'id':777,'slug':'other','htt_id':'htt-42'}
        with self.assertRaisesRegex(ContractError,'slug identity mismatch'):
            resolve_idealwrld_context(canonical,config,Transport())

    def test_wix_control_plane_attestation(self):
        with tempfile.TemporaryDirectory() as folder, FixtureServer() as s:
            path=Path(folder)/'wix-attestation.json'
            path.write_text(json.dumps({
                'source':'wix-env-pull',
                'release_version':'5.18.0',
                'environment':'production',
                'fixture_mode':False,
                'HTT_API_URL':'https://holdthethrone.com/wp-json/htt/v1',
                'HTT_READER_URL':'https://reader.test',
            }))
            spec={
                'control_plane_file':str(path),
                'attestation_source':'wix-env-pull',
                'expected_release_version':'5.18.0',
            }
            actual=read_client_configuration(spec, {}, StagingTransport(s.config()))
            self.assertFalse(actual['fixture_mode'])
            self.assertEqual(actual['release_version'],'5.18.0')
            spec['expected_release_version']='5.19.0'
            with self.assertRaisesRegex(ContractError,'release version mismatch'):
                read_client_configuration(spec, {}, StagingTransport(s.config()))
    def test_timeout_reset_interstitial(self):
        with FixtureServer() as s:
            for error in (TimeoutError(),ConnectionResetError()):
                with patch('urllib.request.OpenerDirector.open',side_effect=error):
                    with self.assertRaises(FetchError):s.http().get(s.base)
        with self.assertRaisesRegex(FetchError,'blocked'):capture(product(body_html=''),Response('','Just a moment cf-chl-',{}))
    def test_product_page_captcha_script_is_not_a_challenge(self):
        item = product(body_html='')
        html = page(item['url']) + '<script src="/captcha.js"></script><script>var captcha = "challenge-platform";</script>'
        captured = capture(item, Response(item['url'], html, {}))
        self.assertNotEqual(captured.get('page_fetch_status'), 'blocked')
        result = scan([captured], {}, True)['read'][0]
        self.assertEqual(result['terminal_state'], 'scored')
        self.assertEqual(result['score'], 5)

    def test_rendered_product_with_captcha_script_is_read(self):
        item = product(body_html='')
        html = page(item['url']) + '<script src="https://cdn.example/captcha.js"></script>'
        captured = capture(item, Response(item['url'], '<title>Just a moment</title>', {}), lambda url: html)
        self.assertEqual(scan([captured], {}, True)['read'][0]['terminal_state'], 'scored')
        self.assertEqual(captured['page_fetch_status'], 'blocked')

    def test_real_rendered_challenge_remains_a_failure(self):
        item = product(body_html='')
        with self.assertRaisesRegex(FetchError, 'blocked'):
            capture(item, Response(item['url'], '', {}), lambda url: '<title>Just a moment</title>Verify you are human')

    def test_unresolved_diagnostics_preserve_product_reason(self):
        from scanner_pipeline import unresolved_report
        from scanner_store import Store
        from test_authority import create
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'diagnostics.db')
            rid = create(store)
            item = product()
            key = store.add(rid, item)
            # Exercise the normal worker with a source access failure.
            class Blocked:
                base='https://brand.test'
                validate=lambda self,url:url
                def get(self,url): raise FetchError('http_500', True)
            run_brand(store, rid, Blocked(), manual=[item])
            report = unresolved_report(store, rid)
            self.assertEqual(report['unresolved_count'], 1)
            self.assertEqual(report['samples'][0]['url'], item['url'])
            self.assertEqual(report['samples'][0]['fetch_status'], 'http_500')
            self.assertEqual(report['samples'][0]['terminal_state'], 'technical_failure')
            store.close()

    def test_terminal_runner_never_calls_failed_ci_a_pass(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            db, config = directory/'scan.db', directory/'config.json'
            db.touch(); config.write_text('{}')
            git = directory/'git'
            git.write_text('#!/bin/bash\ncase "$1" in branch) echo coderabbit/repair-scanner-publication-parity/ffee4f79;; rev-parse) echo tested-sha;; esac\n')
            git.chmod(0o755)
            gh = directory/'gh'
            for watch_status in (0, 1):
                gh.write_text('#!/bin/bash\ncase "$2" in\n'
                    "list) echo '[{\"databaseId\":1,\"headSha\":\"tested-sha\"}]';;\n"
                    f'watch) exit {watch_status};;\n'
                    "view) echo '{\"headSha\":\"tested-sha\",\"conclusion\":\"failure\"}';;\nesac\n")
                gh.chmod(0o755)
                env = {**os.environ, 'PATH': str(directory)+os.pathsep+os.environ['PATH'], 'PYTHON_BIN':sys.executable, 'BASH_ENV':'/dev/null'}
                result = subprocess.run(['bash', str(Path(__file__).with_name('run_kent_canary.sh')), str(db), str(config)], env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('GITHUB FULL VALIDATION PASS', result.stdout)
                self.assertNotIn('RENDERER READY', result.stdout)

    def test_malformed_and_repeated_enumeration(self):
        class HTTP:
            base='https://brand.test'
            validate=lambda self,url:url
            get=lambda self,url:Response(url,'<urlset>',{})
        with self.assertRaisesRegex(FetchError,'malformed_sitemap'):list(Discovery(HTTP()).sitemaps())
        HTTP.get=lambda self,url:Response(url,json.dumps({'products':[{'id':1,'handle':'one'}]}),{})
        with self.assertRaisesRegex(FetchError,'pagination_repeated'):list(Discovery(HTTP()).commerce('shopify'))
    def test_duplicate_conflicts(self):
        rid=self.new_run();self.store.add(rid,product(sku='SAME'));self.store.add(rid,product(sku='SAME'));self.assertEqual(len(self.store.products(rid)),1)
        self.store.add(rid,product(2,sku='SAME'));self.assertTrue(all(p['item']['_identity_conflict'] for p in self.store.products(rid)))
        self.assertEqual(scan([product(),product(body_html='100% polyester')],{},True)['read'][0]['terminal_state'],'evidence_conflict')
    def test_anomaly_and_url_changes(self):
        self.assertTrue(catalogue_change(2533,[product(i) for i in range(7)])['anomaly']);self.assertFalse(catalogue_change(2533,[product(i) for i in range(2487)])['anomaly'])
        self.assertEqual(len(catalogue_change(1,[product(url='https://brand.test/products/new')],[product()])['changed_urls']),1)
    def test_terminal_saved_publication_uses_resume_without_browser_or_refresh(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);database=directory/'state.db';database.touch()
            config=directory/'config.json';config.write_text('{}')
            logged=directory/'args.json'
            git=directory/'git'
            git.write_text('#!/bin/bash\ncase "$1" in branch) echo coderabbit/repair-scanner-publication-parity/ffee4f79;; rev-parse) echo tested-sha;; esac\n')
            gh=directory/'gh'
            gh.write_text('#!/bin/bash\ncase "$2" in\n'
                          "list) echo '[{\"databaseId\":1,\"headSha\":\"tested-sha\"}]';;\n"
                          "view) echo '{\"headSha\":\"tested-sha\",\"conclusion\":\"success\"}';;\nesac\n")
            python=directory/'python-stub'
            python.write_text('#!'+sys.executable+'\nimport json,subprocess,sys\nfrom pathlib import Path\n'
                'if sys.argv[1]=="-c": raise SystemExit(subprocess.call([sys.executable]+sys.argv[1:]))\n'
                'if sys.argv[1]=="-":\n'
                '    source=sys.stdin.read()\n'
                '    assert "playwright" not in source, "saved publication must not launch browser"\n'
                'else:\n'
                '    Path('+repr(str(logged))+').write_text(json.dumps(sys.argv[1:]))\n')
            for path in (git,gh,python):path.chmod(0o755)
            env={**os.environ,'PATH':str(directory)+os.pathsep+os.environ['PATH'],
                 'PYTHON_BIN':str(python),'HTT_API_KEY':'fixture-only','BASH_ENV':'/dev/null'}
            result=subprocess.run(['bash',str(Path(__file__).with_name('run_kent_canary.sh')),
                                   str(database),str(config),'completed-run'],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            args=json.loads(logged.read_text())
            self.assertIn('--publish',args);self.assertIn('--resume',args)
            self.assertEqual(args[args.index('--run-id')+1],'completed-run')
            self.assertNotIn('--refresh',args);self.assertNotIn('--render',args)

    def test_discovery_failure_resume(self):
        with FixtureServer() as s:
            rid=self.new_run(s.base);original=s.http()
            class Fail:
                base=original.base
                validate=original.validate
                def get(self,url):
                    if 'page=2' in url:raise FetchError('http_500',True)
                    return original.get(url)
            result=run_brand(self.store,rid,Fail());self.assertFalse(result['discovery_complete']);self.assertEqual(result['htt_publish_status'],'NOT ATTEMPTED')
            self.assertEqual(run_brand(self.store,rid,original)['processed'],3)
    def test_nonretryable_discovery_failure_is_held_not_retried(self):
        rid=self.new_run()
        class Missing:
            base='https://brand.test'
            validate=lambda self,url:url
            def get(self,url): raise FetchError('http_404',False)
        result=run_brand(self.store,rid,Missing())
        self.assertEqual(result['status'],'REPAIR_REQUIRED')
        self.assertEqual(result['error'],'http_404')
        self.assertFalse(result['discovery_complete'])

    def test_immutable_identity(self):
        one=self.new_run();two=self.new_run();self.assertNotEqual(one,two)
        with self.assertRaises(ValueError):self.store.update(one,scanner_version='other')
        self.assertEqual(self.store.run(one)['scan_run_id'],one)
    def test_alpha_queue_canary_gate(self):
        self.new_run(brand='Zulu');self.store.enqueue([{'brand_id':'43','htt_id':'htt-43','brand':'alpha','domain':'https://brand.test','cpt':'clothes'}]);self.assertEqual(self.store.queue()[0][0]['brand'],'alpha')
        with self.assertRaisesRegex(ValueError,'canaries incomplete'):require_canaries(self.store)
    def test_large_catalogue(self):
        rid=self.new_run(manual_catalogue_complete=True)
        class HTTP:
            base='https://brand.test'
            validate=lambda self,url:url
            def get(self,url):raise AssertionError('Unexpected fetch')
        result=run_brand(self.store,rid,HTTP(),manual=[product(i,manual_evidence_verified=True) for i in range(1,5002)])
        self.assertEqual((result['processed'],result['unaccounted'],result['canonical']['coverage'],result['canonical']['score']),(5001,0,100,5));self.assertEqual(result['status'],'SCANNER_COMPLETE')
    def test_process_killed_mid_brand_and_batch(self):
        with FixtureServer(305) as s:
            for checkpoint in (7,300):
                rid=self.new_run(s.base,force_rescan=True)
                script='''import os,sys
from scanner_store import Store
from scanner_pipeline import run_brand
from catalogue_discovery import PublicHTTP
s=Store(sys.argv[1]); count=0; original=s.save_product
def save(*a,**k):
 global count
 original(*a,**k); count+=1
 if count==int(sys.argv[4]): os._exit(73)
s.save_product=save
run_brand(s,sys.argv[2],PublicHTTP(sys.argv[3],allow_local=True,retries=0,requests_per_second=100000))
'''
                child=subprocess.run([sys.executable,'-c',script,str(self.store.path),rid,s.base,str(checkpoint)],cwd=Path(__file__).parent);self.assertEqual(child.returncode,73)
                self.assertEqual(sum(bool(p['result']) for p in self.store.products(rid)),checkpoint);before=len(s.calls)
                self.assertEqual(run_brand(self.store,rid,s.http())['processed'],305);self.assertEqual(len(s.calls)-before,305-checkpoint)
    def test_rejected_publication_preserves_last_good(self):
        with FixtureServer() as s:
            rid,r=self.local_run(s);self.assertTrue(r['publish_eligible'],r.get('publication_blockers'));s.fail='commit';r=self.propagate(s,rid)
            self.assertEqual(s.current['scan_run_id'],'last-good');self.assertNotEqual(r['status'],'COMPLETE')
    def test_sync_retry_and_all_surface_parity(self):
        with FixtureServer() as s:
            rid,_=self.local_run(s);s.fail='sync';r=self.propagate(s,rid);self.assertEqual(r['htt_publish_status'],'PASS');self.assertEqual(r['idealwrld_sync_status'],'IW_SYNC_RETRY')
            s.fail=None;r=self.propagate(s,rid);self.assertEqual(r['status'],'COMPLETE',r.get('error'));self.assertEqual(s.calls.count(('POST','/commit')),1)
    def test_every_stale_surface_and_fixture_mode_fail(self):
        with FixtureServer(1) as s:
            rid,_=self.local_run(s);self.propagate(s,rid);canonical=self.store.run(rid)['canonical'];rows=[p['result'] for p in self.store.products(rid)]
            for name in SURFACES:
                s.stale=name;self.assertEqual(parity_check(canonical,s.config(),StagingTransport(s.config()),rows)['status'],'FAIL',name)
            s.stale=None;s.fixture=True;self.assertEqual(parity_check(canonical,s.config(),StagingTransport(s.config()),rows)['status'],'FAIL')
    def test_readonly_parity_validates_production_config(self):
        with FixtureServer(1) as server:
            rid,_=self.local_run(server);self.propagate(server,rid)
            config=server.config();config['environment']='read-only'
            transport=StagingTransport(config);original=transport.request
            def deployed(url,*args,**kwargs):
                value=original(url,*args,**kwargs)
                if '/config/' in url:value['environment']='production'
                return value
            with patch.object(transport,'request',side_effect=deployed):
                result=parity_check(self.store.run(rid)['canonical'],config,transport,self.store.result_rows(rid))
            self.assertEqual(result['status'],'PASS')

    def test_missing_blanks_identity(self):
        expected={k:None for k in FIELDS};expected['htt_id']='htt-42';self.assertTrue(compare(expected,{}, {},FIELDS))
        for key,value in [('htt_id','other'),('score','')]:
            actual=dict(expected);actual[key]=value;self.assertTrue(compare(expected,actual,{},FIELDS))
    def test_htt_iw_mismatch(self):
        with FixtureServer(1) as s:
            rid,_=self.local_run(s);s.stale='htt_api';r=self.propagate(s,rid);self.assertEqual(r['htt_readback_status'],'FAIL');self.assertNotIn(('POST','/sync'),s.calls)
            s.stale='idealwrld';r=self.propagate(s,rid);self.assertEqual(r['idealwrld_readback_status'],'FAIL');self.assertEqual(r['status'],'SYNC_PENDING')
    def test_no_production_writes(self):
        with self.assertRaises(ContractError):StagingTransport({'environment':'production'})
        with self.assertRaises(ContractError):StagingTransport({'environment':'staging','allowed_hosts':['holdthethrone.com']}).request('https://holdthethrone.com/wp-json/htt/v1/scans',{})
    def test_deploy_guard(self):
        # Never invoke a production checkout's deploy command with inherited CI
        # credentials; an explicit untrusted origin exercises the actual guard.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scanner = root / 'Clothing/Scanner'
            scanner.mkdir(parents=True)
            script = scanner / 'deploy_aws_reader.sh'
            script.write_text((Path(__file__).parent / script.name).read_text())
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin',
                            'https://github.com/example/untrusted.git'], check=True)
            result = subprocess.run(['bash', str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Deployment disabled', result.stderr)

    def test_deploy_guard_without_git_metadata(self):
        # Azure immutable releases are source archives, not Git checkouts.
        # The deployment guard must fail closed with the documented status
        # instead of leaking git's "not a repository" exit code 128.
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'deploy_aws_reader.sh'
            script.write_text((Path(__file__).parent / 'deploy_aws_reader.sh').read_text())
            result = subprocess.run(['bash', str(script)], cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Deployment disabled', result.stderr)
            self.assertNotIn('not a git repository', result.stderr.lower())

    def test_all_nonlocal_writes_disabled(self):
        for environment in ('staging','read-only'):
            transport=StagingTransport({'environment':environment,'allowed_hosts':['staging.example']})
            with self.assertRaisesRegex(ContractError,'Live writes disabled'):
                transport.request('https://staging.example/scans',{})
    def test_readonly_config_accepts_production_get_without_writes(self):
        transport=StagingTransport({'environment':'read-only','allowed_hosts':['holdthethrone.com']})
        with patch('urllib.request.OpenerDirector.open',side_effect=OSError('offline')):
            with self.assertRaisesRegex(ContractError,'OSError'):
                transport.request('https://holdthethrone.com/wp-json/htt/v1/brands/example')
        with self.assertRaisesRegex(ContractError,'Live writes disabled'):
            transport.request('https://holdthethrone.com/wp-json/htt/v1/scans',{})
    def test_owner_supplied_routes_and_dry_run(self):
        from htt_adapters import publication_plan
        with FixtureServer(1) as server:
            rid,run=self.local_run(server)
            plan=publication_plan(run,self.store.products(rid),{'htt_api':'https://holdthethrone.com/wp-json/htt/v1','idealwrld_api':'https://idealwrld.com/wp-json/htt/v1','app_report':'https://idealwrld.com/wp-json/htt/v1'})
            self.assertFalse(plan['request_allowed']);self.assertTrue(plan['intake_url'].endswith('/scans'))
            self.assertTrue(plan['idealwrld_push_receiver'].endswith('/standard-sync'))
            self.assertTrue(plan['consumer_routes']['app_report'].endswith('/report?id={idealwrld_id}'))
            self.assertNotIn('brand',plan['intake_payload']['scannerOwnedMetadata'])
    def test_variant_enumeration_preserves_sku_identity(self):
        class HTTP:
            base='https://brand.test'
            validate=lambda self,url:url
            def get(self,url):
                products=[{'id':1,'handle':'shirt','title':'Shirt','variants':[{'id':11,'sku':'ONE'},{'id':12,'sku':'TWO'}]}] if 'page=1' in url else []
                return Response(url,json.dumps({'products':products}),{})
        items=list(Discovery(HTTP(),census_unit='variant').enumerate('shopify'))
        self.assertEqual([i['variant_id'] for i in items],['11','12']);self.assertEqual([i['sku'] for i in items],['ONE','TWO'])
    def test_missing_sku_endpoint_fails(self):
        from publication_parity import endpoint
        with self.assertRaises(ContractError):endpoint({'url':'https://example.test/brands/{slug}/skus/{sku}'},{'slug':'brand','sku':''})
    def test_manual_subset_cannot_publish(self):
        with FixtureServer(1) as server:
            rid=self.new_run(server.base)
            run=run_brand(self.store,rid,server.http(),manual=[product(base=server.base,manual_evidence_verified=True)])
            self.assertIn('MANUAL_CATALOGUE_COMPLETENESS_UNVERIFIED',run['publication_blockers'])

    def test_beauty_uses_canonical_ingredient_reader(self):
        item=product(title='Botanical serum',body_html='Ingredients: Aqua, Glycerin, Aloe Barbadensis Leaf Extract')
        out=capture(item,Response(item['url'],page(item['url'],'Ingredients: Aqua, Glycerin, Aloe Barbadensis Leaf Extract'),{}))
        row=scan([out],{},True)['read'][0]
        self.assertEqual(row['department'],'beauty');self.assertEqual(row['terminal_state'],'scored')

    def test_retry_does_not_turn_extraction_failure_into_undisclosed(self):
        item=product(body_html='',fetch_status='render_required')
        with self.assertRaisesRegex(FetchError,'render_required'):
            capture(item,Response(item['url'],page(item['url'],'Wash cold'),{}))


class StoreCompatibilityTest(unittest.TestCase):
    def legacy(self, path, item='{"url":"https://brand.test/products/p1","sku":"kept"}'):
        import sqlite3
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE products(run_id TEXT,identity TEXT,item TEXT NOT NULL,
                    result TEXT,state TEXT NOT NULL,retryable INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(run_id,identity));
                CREATE TABLE runs(id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,data TEXT NOT NULL);
            ''')
            db.execute('INSERT INTO runs VALUES(?,?,?)', ('old-run', 'kent', '{"scan_run_id":"old-run"}'))
            db.execute('INSERT INTO products VALUES(?,?,?,?,?,?,?)',
                       ('old-run', 'p1', item, '{"terminal_state":"blocked"}', 'blocked', 1, 3))

    def test_foreign_products_schema_is_rejected_without_writes(self):
        import sqlite3
        from scanner_store import DatabaseSchemaError
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'scanner.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE products(id TEXT PRIMARY KEY,payload TEXT)')
                db.execute('INSERT INTO products VALUES(?,?)', ('saved', 'original evidence'))
            before = path.read_bytes()
            with self.assertRaisesRegex(DatabaseSchemaError, 'products is missing .*item'):
                Store(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(str(path)+'-wal').exists())

    def test_other_incompatible_tables_are_rejected_before_migration(self):
        import sqlite3
        from scanner_store import DatabaseSchemaError
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'scanner.sqlite'
            self.legacy(path)
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE queue(id TEXT, payload TEXT)')
            before = path.read_bytes()
            with self.assertRaisesRegex(DatabaseSchemaError, 'queue is missing'):
                Store(path)
            self.assertEqual(path.read_bytes(), before)

    def test_known_legacy_migration_preserves_history_and_retry_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'scanner.sqlite'
            self.legacy(path)
            for _ in range(2):
                store = Store(path)
                try:
                    row, = store.products('old-run')
                    self.assertEqual(row['item'], row['discovery'])
                    self.assertEqual(row['item']['sku'], 'kept')
                    self.assertEqual(row['state'], 'blocked')
                    self.assertTrue(row['retryable'])
                    self.assertEqual(row['attempts'], 3)
                    self.assertEqual(row['result']['terminal_state'], 'blocked')
                    self.assertEqual(store.run('old-run'), {'scan_run_id':'old-run'})
                finally:
                    store.close()

    def test_failed_migration_rolls_back_schema_and_rows(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'scanner.sqlite'
            self.legacy(path, item='invalid JSON')
            with sqlite3.connect(path) as db:
                before = list(db.iterdump())
            with self.assertRaisesRegex(sqlite3.OperationalError, 'malformed JSON'):
                Store(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(list(db.iterdump()), before)

    def test_cli_reports_incompatible_database_without_traceback(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'scanner.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE products(id TEXT PRIMARY KEY)')
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('scanner_pipeline.py')),
                                     '--db', str(path), '--status'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('SCANNER DATABASE SCHEMA MISMATCH', result.stdout)
            self.assertNotIn('Traceback', result.stderr)

if __name__=='__main__':unittest.main()
