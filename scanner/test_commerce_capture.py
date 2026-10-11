import json,unittest
from catalogue_discovery import Response
from commerce_capture import shopify_ajax_capture,CommerceEvidenceError
class CommerceCaptureTest(unittest.TestCase):
    def capture(self,obj=None,cart=None):
        obj=obj or {'id':123,'handle':'robe','url':'/products/robe','images':['//cdn.shopify.com/robe.jpg'],
            'variants':[{'id':1,'sku':'R-S','options':['Small'],'price':12345,'available':True},
                        {'id':2,'sku':None,'options':['Large'],'price':15000,'available':False}]}
        return shopify_ajax_capture({'url':'https://brand.test/products/robe','product_id':'123'},
            Response('https://brand.test/products/robe.js',json.dumps(obj),{}),
            Response('https://brand.test/cart.js',json.dumps(cart or {'currency':'USD'}),{}),'2026-10-07T08:00:00Z','anonymous-context')
    def test_exact_variant_prices_and_absent_sku(self):
        result=self.capture();a,b=result['variants']
        self.assertEqual((a['price'],b['price']),('123.45','150.00'))
        self.assertEqual(b['sku'],'');self.assertEqual(b['sku_status'],'not_disclosed')
        self.assertEqual(b['availability'],'out_of_stock');self.assertTrue(result['variant_census_complete'])
        self.assertEqual(result['product_images'][0]['scope'],'product');self.assertIsNone(a['image_source_url'])
    def test_currency_is_required_not_inferred_from_domain(self):
        with self.assertRaisesRegex(CommerceEvidenceError,'currency_unresolved'):self.capture(cart={'currency':'$'})
    def test_duplicate_and_foreign_variant_ids_rejected(self):
        for variants in ([{'id':1,'price':100},{'id':1,'price':200}], [{'id':1,'product_id':999,'price':100}]):
            with self.assertRaises(CommerceEvidenceError):self.capture({'id':123,'handle':'robe','url':'/products/robe','variants':variants})
    def test_fractional_ajax_integer_not_silently_scaled(self):
        with self.assertRaisesRegex(CommerceEvidenceError,'price_invalid'):
            self.capture({'id':123,'handle':'robe','url':'/products/robe','variants':[{'id':1,'price':12.5}]})
    def test_ceiling_is_not_complete_census(self):
        result=self.capture({'id':123,'handle':'robe','url':'/products/robe','variants':[{'id':i,'price':100} for i in range(1,251)]})
        self.assertFalse(result['variant_census_complete']);self.assertEqual(len(result['variants']),250)

class CommerceCheckpointTest(unittest.TestCase):
    def setUp(self):
        import tempfile,pathlib
        from scanner_store import Store
        self.temp=tempfile.TemporaryDirectory();self.store=Store(pathlib.Path(self.temp.name)/'state.db')
        self.item={'product_id':'123','url':'https://brand.test/products/robe',
                   'commerce_json':{'id':123,'variants':[{'id':1,'sku':''}]}}
    def tearDown(self):self.store.close();self.temp.cleanup()
    def pair(self,urls,executor):
        return [Response(urls[0],'{"currency":"USD"}',{}),Response(urls[1],json.dumps({
            'id':123,'url':'/products/robe','handle':'robe','variants':[{'id':1,'price':12345,'sku':None}]}),{})]
    def test_complete_pair_is_checkpointed_and_replayed_without_network(self):
        from commerce_capture import capture_product_commerce
        first=capture_product_commerce(self.item,self.pair,None,self.store,'run')
        def no_network(*args):raise AssertionError('checkpoint must avoid network')
        second=capture_product_commerce(self.item,no_network,None,self.store,'run')
        self.assertEqual(first,second);self.assertEqual(first['variants'][0]['price'],'123.45')
    def test_changed_variant_census_is_held_without_caching_pair(self):
        from commerce_capture import capture_product_commerce
        from catalogue_discovery import FetchError
        self.item['commerce_json']['variants']=[{'id':2}]
        with self.assertRaisesRegex(FetchError,'variant_census_changed'):
            capture_product_commerce(self.item,self.pair,None,self.store,'run')
        self.assertIsNone(self.store.page('run','commerce-pair:'+self.item['url']))
    def test_missing_adapter_is_technical_not_absent_sku(self):
        from commerce_capture import capture_product_commerce
        from catalogue_discovery import FetchError
        self.item['commerce_json']={}
        with self.assertRaisesRegex(FetchError,'commerce_adapter_required'):
            capture_product_commerce(self.item,None,None,self.store,'run')

class CommerceStagingTest(unittest.TestCase):
    def test_authenticated_staging_receipt_is_bound_to_product_and_digest(self):
        from htt_publish import HTTIntake
        from unittest.mock import Mock,patch
        from publication_parity import ContractError
        commercial={'variant_census_complete':True,'variants':[{'variant_id':'1','sku':''}]}
        store=Mock();store.run.return_value={'brand_id':'42','htt_id':'htt-42','slug':'brand','require_commerce_capture':True}
        product={'item':{'product_id':'123','url':'https://brand.test/products/robe','commercial':commercial},'result':{'terminal_state':'scored'}}
        store.iter_products.side_effect=lambda _:iter([product])
        intake=HTTIntake('https://htt.test/api',None);calls=[]
        def post(path,body):
            calls.append((path,body));return {'status':'staged','product_id':body['product_id'],'source_digest':body['commercial_sha256']}
        with patch.object(intake,'_post',side_effect=post):intake.stage_commerce(store,'run')
        self.assertEqual(calls[0][0],'/brands/brand/commerce-stage');self.assertEqual(calls[0][1]['commercial'],commercial)
        with patch.object(intake,'_post',return_value={'status':'staged','product_id':'wrong','source_digest':'wrong'}):
            with self.assertRaisesRegex(ContractError,'RECEIPT MISMATCH'):intake.stage_commerce(store,'run')

    def test_media_staging_resumes_only_with_bound_identity_and_progress(self):
        from htt_publish import HTTIntake
        from unittest.mock import patch
        from publication_parity import ContractError
        intake=HTTIntake('https://htt.test/api',None)
        body={'product_id':'123'}
        pending={'status':'staging','product_id':'123','source_digest':'digest','captured_images':2}
        done={**pending,'status':'staged'}
        with patch.object(intake,'_post',side_effect=[pending,done]) as post:
            self.assertEqual(intake.stage_product_commerce('brand',body,'digest'),done)
            self.assertEqual(post.call_count,2)
        with patch.object(intake,'_post',side_effect=[pending,pending]):
            with self.assertRaisesRegex(ContractError,'DID NOT PROGRESS'):
                intake.stage_product_commerce('brand',body,'digest')

class CommercialLegacyProjectionTest(unittest.TestCase):
    def test_selected_sku_gets_its_own_variant_price_never_the_product_minimum(self):
        from htt_adapters import publication_style
        product={'item':{'url':'https://brand.test/products/a','product_id':'123','sku':'LARGE','variant_id':'2','price':10,
            'commercial':{'variant_census_complete':True,'variants':[
                {'variant_id':'1','sku':'SMALL','price':'10.00','currency':'USD'},
                {'variant_id':'2','sku':'LARGE','price':'15.00','currency':'USD'}]}},'result':{'terminal_state':'scored'}}
        style=publication_style(product)
        self.assertEqual((style['price'],style['currency']),('15.00','USD'))
        self.assertEqual(style['composition_scope'],'product')
        self.assertEqual(style['variant_composition_scope'],'not_established')
        product['item']['variant_id']='unknown'
        style=publication_style(product);self.assertIsNone(style['price']);self.assertEqual(style['currency'],'')

class OptionalCommerceTest(unittest.TestCase):
    def test_optional_missing_adapter_retains_explicit_reason(self):
        from commerce_capture import capture_available_commerce
        commercial, reason = capture_available_commerce({'url':'https://brand.test/p/robe'},None,None,None,'run',False)
        self.assertIsNone(commercial);self.assertEqual(reason,'commerce_adapter_required')
    def test_required_commerce_still_blocks(self):
        from commerce_capture import capture_available_commerce
        from catalogue_discovery import FetchError
        with self.assertRaises(FetchError):
            capture_available_commerce({'url':'https://brand.test/p/robe'},None,None,None,'run',True)
    def test_disk_pressure_is_never_masked_as_missing_price(self):
        from commerce_capture import capture_available_commerce
        from scan_policy import ScanPaused
        from unittest.mock import patch
        with patch('commerce_capture.capture_product_commerce',side_effect=ScanPaused('disk_pressure',12345)):
            with self.assertRaises(ScanPaused):capture_available_commerce({},None,None,None,'run',False)
