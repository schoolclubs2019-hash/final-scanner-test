"""Controlled authority load/fault proofs. No production network or writes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from catalogue_discovery import Discovery, Response
from consumer_lookup import lookup
from delta_refresh import fresh
from row_stream import RowStream
from scan_contract import aggregate_rows, read_products
from scanner_pipeline import run_brand, pipeline_fingerprint, SCANNER_VERSION, select_queue
from scanner_store import Store, now
from throne_read import READER_VERSION
from throne_standard import VERSION
from test_pipeline import FixtureServer, product
from publication_parity import propagate, StagingTransport


class CatalogueHTTP:
    base = 'https://catalogue.test'
    def __init__(self, total=15000, delta=False):
        self.ids = list(range(1,14801)) + list(range(15001,15381)) if delta else range(1,total+1)
        self.delta = delta
        self.reads = 0
        self.pages = 0
        self.max_page = 0
    def validate(self, url): return url
    def get(self, url):
        parts=urlsplit(url)
        if parts.path == '/products.json':
            page=int(parse_qs(parts.query)['page'][0]); ids=self.ids[(page-1)*250:page*250]
            self.max_page=max(self.max_page,len(ids)); self.pages+=1
            products=[]
            for number in ids:
                synthetic=number in {1,15000} or self.delta and 14701 <= number <= 14800
                products.append({'id':number,'handle':f'p{number}','title':'Shirt','sku':f'SKU-{number}',
                                 'body_html':'100% polyester' if synthetic else '100% cotton','updated_at':'v2' if self.delta and 14701 <= number <= 14800 else 'v1'})
            return Response(url,json.dumps({'products':products}),{})
        self.reads+=1
        return Response(url,'<main><p>Product description supplied by matched platform object.</p></main>',{})


def create(store, **extras):
    brand={'brand_id':'load-proof','htt_id':'htt-load','brand':'Controlled Catalogue','slug':'controlled',
           'domain':CatalogueHTTP.base,'cpt':'clothes','department':'clothing','adapter':'shopify',**extras}
    store.enqueue([brand])
    return store.create(brand,pipeline_fingerprint(),SCANNER_VERSION,READER_VERSION,VERSION)['scan_run_id']


class LargeAuthorityProof(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.path=Path(cls.temp.name)/'authority.db';cls.store=Store(cls.path)
        cls.run_id=create(cls.store)
        script='''import os,signal,sys
from test_authority import CatalogueHTTP
from scanner_store import Store
from scanner_pipeline import run_brand
store=Store(sys.argv[1]); original=store.save_product; count=0
def save(*args,**kwargs):
 global count
 original(*args,**kwargs);count+=1
 if count==7500:os.kill(os.getpid(),signal.SIGKILL)
store.save_product=save
run_brand(store,sys.argv[2],CatalogueHTTP())
'''
        cls.child=subprocess.run([sys.executable,'-c',script,str(cls.path),cls.run_id],cwd=Path(__file__).parent)
        cls.before=cls.store.db.execute('SELECT COUNT(*) FROM products WHERE run_id=? AND result IS NOT NULL',(cls.run_id,)).fetchone()[0]
        cls.http=CatalogueHTTP();cls.batch_sizes=[]
        def bounded(items,*args,**kwargs):
            cls.batch_sizes.append(len(items));assert len(items)<=300
            return read_products(items,*args,**kwargs)
        with patch('scanner_pipeline.read_products',side_effect=bounded):
            cls.result=run_brand(cls.store,cls.run_id,cls.http)
    @classmethod
    def tearDownClass(cls): cls.store.close();cls.temp.cleanup()
    def test_15000_kill_resume_and_complete_canonical_aggregate(self):
        self.assertEqual(self.child.returncode,-signal.SIGKILL);self.assertEqual(self.before,7500)
        self.assertEqual(self.http.reads,7500);self.assertEqual(self.http.pages,0)
        r=self.result
        self.assertEqual((r['catalogue_confirmed'],r['processed'],r['unaccounted']),(15000,15000,0))
        self.assertEqual(r['canonical']['score'],4)
        self.assertEqual(r['report']['brand']['counts']['plant'],14998)
        self.assertEqual(r['scanner_status'],'SCANNER_COMPLETE');self.assertFalse(r['fully_complete'])
        self.assertEqual(r['discovery_reconciliation'],'PASS');self.assertLessEqual(max(self.batch_sizes),300)
        for number in (1,7500,15000):
            found=self.store.db.execute("SELECT result,attempts FROM products WHERE run_id=? AND json_extract(discovery,'$.product_id')=?",(self.run_id,str(number))).fetchone()
            self.assertIsNotNone(found);self.assertIsNotNone(found[0]);self.assertEqual(found[1],1)
    def test_delta_15000_to_15180(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'delta.db');self.store.db.backup(store.db)
            rid=create(store);http=CatalogueHTTP(delta=True);result=run_brand(store,rid,http)
            self.assertEqual(result['delta_counts'],{'UNCHANGED':14700,'CHANGED':100,'NEW':380,'REMOVED':200,'REQUIRES_RECHECK':0})
            self.assertEqual(http.reads,480)
            self.assertEqual((result['processed'],result['unaccounted']),(15180,0))
            self.assertEqual(result['canonical']['score'],4)
            self.assertEqual(result['report']['brand']['counts']['plant'],15079)
            self.assertEqual(store.product_count(self.run_id),15000)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM changes WHERE run_id=? AND state='REMOVED'",(rid,)).fetchone()[0],200)
            store.close()
    def test_50000_discovery_metadata_no_ceiling(self):
        http=CatalogueHTTP(50000);items=Discovery(http).enumerate('shopify')
        count=0;first=last=None
        for item in items:
            count+=1;first=first or item['product_id'];last=item['product_id']
        self.assertEqual((count,first,last),(50000,'1','50000'));self.assertEqual(http.pages,201);self.assertEqual(http.max_page,250)


class AuthorityContractTest(unittest.TestCase):
    def setUp(self): self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'test.db')
    def tearDown(self): self.store.close();self.temp.cleanup()
    def small_run(self):
        rid=create(self.store);return rid,run_brand(self.store,rid,CatalogueHTTP(3))
    def test_stream_aggregate_equals_list_with_same_canonical_rules(self):
        items=[product(i,composition='100% cotton' if i%3 else '100% polyester',body_html='') for i in range(1,19)]
        rows,stats=read_products(items,{},True);payload={'catalogue':{'complete':True,'confirmed_total':len(rows)}}
        expected=aggregate_rows(rows,payload,True,stats,compact=True)
        actual=aggregate_rows(RowStream(lambda:iter(rows),len(rows)),payload,True,stats,compact=True)
        self.assertEqual(actual,expected)
    def test_expired_evidence_and_changed_rules_recheck(self):
        rid,_=self.small_run()
        with self.store.db:
            self.store.db.execute("UPDATE products SET item=json_set(item,'$.fetchedAt','2000-01-01T00:00:00+00:00') WHERE run_id=?",(rid,))
        next_id=create(self.store);http=CatalogueHTTP(3);r=run_brand(self.store,next_id,http)
        self.assertEqual(r['delta_counts']['REQUIRES_RECHECK'],3);self.assertEqual(http.reads,3)
        # Trusted unexpired raw evidence is rescored after a scanner fingerprint change.
        old=self.store.run(next_id);old['scanner_fingerprint']='previous-rules'
        with self.store.db:self.store.db.execute('UPDATE runs SET data=? WHERE id=?',(json.dumps(old),next_id))
        next_id=create(self.store);http=CatalogueHTTP(3);r=run_brand(self.store,next_id,http)
        self.assertEqual(r['delta_counts']['REQUIRES_RECHECK'],3);self.assertEqual(http.reads,0)
        self.assertFalse(fresh({'fetchedAt':'2100-01-01T00:00:00+00:00'},30))
    def test_force_rescan_does_not_reuse(self):
        self.small_run();rid=create(self.store,force_rescan=True);http=CatalogueHTTP(3);r=run_brand(self.store,rid,http)
        self.assertEqual(http.reads,3);self.assertEqual(r['reused_evidence'],0)
    def test_crash_discovery_then_resume_without_repeating_network_pages(self):
        rid=create(self.store);http=CatalogueHTTP(501);save=self.store.add;count=0
        def terminate(*args):
            nonlocal count
            save(*args);count+=1
            if count==270:raise SystemExit('killed in discovery')
        with patch.object(self.store,'add',side_effect=terminate):
            with self.assertRaises(SystemExit):run_brand(self.store,rid,http)
        self.assertEqual(http.pages,2)
        http=CatalogueHTTP(501);r=run_brand(self.store,rid,http)
        self.assertEqual(http.pages,2);self.assertEqual(r['catalogue_discovered'],501);self.assertEqual(r['duplicate_count'],0)
    def test_crash_aggregation_only_reaggregates_persisted_rows(self):
        rid=create(self.store);http=CatalogueHTTP(3)
        with patch('scanner_pipeline.aggregate_rows',side_effect=SystemExit('killed aggregation')):
            with self.assertRaises(SystemExit):run_brand(self.store,rid,http)
        http=CatalogueHTTP(3);r=run_brand(self.store,rid,http)
        self.assertEqual(http.reads,0);self.assertEqual(r['processed'],3)
    def test_compute_timeout_is_terminal_not_undisclosed(self):
        rid=create(self.store);calls=0
        def backend(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:raise TimeoutError('AWS computation timeout')
            return read_products(*args,**kwargs)
        with patch('scanner_pipeline.read_products',side_effect=backend):r=run_brand(self.store,rid,CatalogueHTTP(3))
        self.assertEqual(r['technical_failures'],1);self.assertEqual(r['undisclosed'],0);self.assertFalse(r['publish_eligible'])
    def test_public_client_cannot_claim_authority_or_bulk_read(self):
        from aws_reader_lambda import handler
        from test_aws_reader_lambda import event
        # Use the existing client identity contract from the Lambda fixture.
        for payload in ({'mode':'AUTHORITY_CENSUS','items':[product()]}, {'mode':'CONSUMER_LOOKUP','items':[product(),product(2)]}):
            request=event('POST','/v1/read',payload)
            request['headers']={'x-throne-install-id':'12345678-1234-1234-1234-123456789012'}
            self.assertEqual(handler(request,None)['statusCode'],403)
    def test_consumer_reuses_current_result_without_crawling(self):
        calls=[];queued=[]
        canonical={'htt_id':'htt-one','score':5,'publication_status':'verified','scan_run_id':'current','standard_version':VERSION,'scan_date':now()}
        class Transport:
            def request(self,url):calls.append(url);return canonical
        result=lookup('htt-one','brand',Transport(),'https://htt.test/api',lambda *a:queued.append(a))
        self.assertEqual(result['status'],'CURRENT');self.assertEqual(len(calls),1);self.assertEqual(queued,[])
        class MissingSKU:
            def request(self,url): return None if '/skus/' in url else canonical
        result=lookup('htt-one','brand',MissingSKU(),'https://htt.test/api',lambda *a:queued.append(a),sku='missing')
        self.assertEqual(result['status'],'UPDATE_REQUIRED');self.assertEqual(len(queued),1)
    def test_consumer_large_stale_result_queues_once_no_scan(self):
        calls=[]
        class Transport:
            def request(self,url):calls.append(url);return {'htt_id':'htt-one','catalogue_total':50000,'publication_status':'stale'}
        for _ in range(2):
            result=lookup('htt-one','brand',Transport(),'https://htt.test/api',self.store.enqueue_refresh)
            self.assertEqual(result['status'],'UPDATE_REQUIRED');self.assertFalse(result['authority_scan_started'])
        self.assertEqual(len(calls),2);self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM refresh_requests').fetchone()[0],1)
    def test_queue_filters(self):
        queue=[({'brand':name,'slug':name.lower()},None) for name in ('Alpha','Aritzia','Zara')]
        self.assertEqual([b['brand'] for b,_ in select_queue(queue,start_brand='Aritzia')],['Aritzia','Zara'])
        self.assertEqual(len(select_queue(queue,brand='aritzia')),1)
    def test_crash_staging_and_sync_pending_resume(self):
        with FixtureServer(2) as server:
            brand={'brand_id':'42','htt_id':'htt-42','brand':'Example','domain':server.base,'cpt':'clothes','adapter':'shopify'}
            self.store.enqueue([brand]);rid=self.store.create(brand,pipeline_fingerprint(),SCANNER_VERSION,READER_VERSION,VERSION)['scan_run_id']
            run_brand(self.store,rid,server.http());transport=StagingTransport(server.config());original=transport.request
            def crash(url,*args,**kwargs):
                result=original(url,*args,**kwargs)
                if url.endswith('/stage'):raise SystemExit('killed after stage')
                return result
            with patch.object(transport,'request',side_effect=crash):
                with self.assertRaises(SystemExit):propagate(self.store,rid,server.config(),transport,self.store.result_rows(rid))
            self.assertEqual(server.current['scan_run_id'],'last-good')
            def sync_crash(url,*args,**kwargs):
                if url.endswith('/sync'):raise SystemExit('killed sync pending')
                return original(url,*args,**kwargs)
            with patch.object(transport,'request',side_effect=sync_crash):
                with self.assertRaises(SystemExit):propagate(self.store,rid,server.config(),transport,self.store.result_rows(rid))
            self.assertEqual(self.store.run(rid)['htt_publish_status'],'PASS')
            result=propagate(self.store,rid,server.config(),transport,self.store.result_rows(rid))
            self.assertTrue(result['fully_complete']);self.assertEqual(server.calls.count(('POST','/commit')),1)
            # A later read-only parity command must revoke a prior completion flag.
            server.stale='app_report'
            config=Path(self.temp.name)/'config.json';config.write_text(json.dumps(server.config()))
            command=subprocess.run([sys.executable,'scanner_pipeline.py','--db',str(self.store.path),
                                    '--parity-check',rid,'--config',str(config)],cwd=Path(__file__).parent,capture_output=True,text=True)
            self.assertEqual(command.returncode,1)
            self.assertFalse(self.store.run(rid)['fully_complete'])
            self.assertEqual(self.store.run(rid)['completion_status'],'INCOMPLETE')


if __name__=='__main__':unittest.main()
