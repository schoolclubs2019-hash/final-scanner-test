"""Public API regressions for attribution, routing, reporting and automation."""
import json
import os
import unittest
from unittest.mock import patch
from pathlib import Path
from aws_reader_lambda import handler
from test_aws_reader_lambda import event
from price_capture import capture_price
from brand_capture import capture_brand
from confirmed_product import confirm_product


class ScanIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'THRONE_API_KEY': 'test-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def product(self, **kwargs):
        return dict({'sku': 'sku-1', 'title': 'Shirt', 'url': 'https://brand.test/products/shirt'}, **kwargs)

    def scan(self, items, trusted=True, **kwargs):
        headers = {'authorization': 'Bearer [REDACTED]'} if trusted else {'x-throne-install-id': '12345678-1234-1234-1234-123456789012'}
        result = handler(event('POST', '/v1/read', {'items': items, **kwargs}, headers), None)
        self.assertEqual(result['statusCode'], 200, result['body'])
        return json.loads(result['body'])

    def test_kent_fabric_covered_is_not_a_component_declaration(self):
        recorded = json.loads((Path(__file__).with_name('fixtures')/'kent-breather-brief.json').read_text())
        product = recorded['product']
        row = self.scan([self.product(title=product['title'], body_html=product['body_html'])])['read'][0]
        self.assertEqual(row['terminal_state'], 'scored')
        self.assertEqual(row['shares']['plant'], 100)
        self.assertEqual(row['score'], 5)
        self.assertFalse(row.get('composition_over_100'))

    def test_hyphenated_component_declarations_remain_separate(self):
        for declaration in ('Shell-100% cotton; Lining-100% polyester',
                            'Shell - 100% cotton; Lining - 100% polyester'):
            row = self.scan([self.product(composition=declaration)])['read'][0]
            self.assertEqual(row['terminal_state'], 'partial_composition')
            self.assertIsNone(row['score'])
            self.assertEqual(len(row['component_declarations']), 2)

    def test_cross_sell_does_not_supply_composition(self):
        for value in ['Pair well with our 100% linen hat.', 'Style with a 100% cotton shirt.',
                      '<aside>Composition: 100% cotton</aside>',
                      '<div class="related-products"><h3>Composition</h3>100% cotton</div>']:
            with self.subTest(value=value):
                row = self.scan([self.product(body_html=value)])['read'][0]
                self.assertIsNone(row['score'])
                self.assertEqual(row['band'], 'undisclosed')

    def test_matched_product_schema_material_is_used_and_conflicts_are_retained(self):
        schema = {'@type':'Product','name':'Shirt','material':'100% cotton'}
        row = self.scan([self.product(product_json=schema)])['read'][0]
        self.assertEqual(row['band'], 'plant')
        conflict = self.scan([self.product(composition='100% polyester', product_json=schema)])['read'][0]
        self.assertEqual(conflict['how'], 'evidence-conflict')

    def test_product_spec_after_recommendation_container_is_found(self):
        html = '<div class="related-products">Composition: 100% polyester</div><section><h3>Composition</h3>100% cotton</section>'
        row = self.scan([self.product(page_text=html)])['read'][0]
        self.assertEqual(row['band'], 'plant')
        self.assertEqual(row['shares']['plant'], 100)

    def test_description_and_reverse_percentage_shares(self):
        row = self.scan([self.product(description='Composition: Cotton 95%, Elastane 5%')])['read'][0]
        self.assertEqual(row['shares']['plant'], 95)
        self.assertEqual(row['shares']['synthetic'], 5)

    def test_unknown_fibre_never_disappears(self):
        result = self.scan([self.product(composition='100% cotton, 5% mysteryfibre')])
        row = result['read'][0]
        self.assertIsNone(row['score'])
        self.assertTrue(row['composition_over_100'])
        self.assertFalse(result['brand']['publish_eligible'])

    def test_conflicting_page_declarations_cannot_publish(self):
        row = self.scan([self.product(page_text='Composition: 100% cotton. Our brand uses recycled materials. Composition: 100% polyester')])['read'][0]
        self.assertEqual(row['how'], 'evidence-conflict')
        self.assertIsNone(row['score'])

    def test_labelled_single_ingredient_beauty_is_found(self):
        row = self.scan([self.product(title='Face serum', description='Ingredients: Aqua')])['read'][0]
        self.assertEqual(row['assessment_status'], 'assessed')

    def test_partial_plant_declaration_is_not_a_pure_plant_product(self):
        result = self.scan([self.product(composition='95% cotton')])
        self.assertIsNone(result['brand']['plantPct'])
        self.assertIsNone(result['read'][0]['passes'])
        self.assertIn('partial_composition', result['brand']['publication_blockers'])

    def test_blended_textile_does_not_receive_a_purity_pass(self):
        result = self.scan([self.product(composition='95% cotton, 5% elastane')],
                           catalogue={'complete':True, 'confirmed_total':1})
        self.assertFalse(result['read'][0]['passes'])
        self.assertEqual(result['read'][0]['score'], 4)
        self.assertEqual(result['brand']['score'], 4)

    def test_invalid_totals_stay_unscored(self):
        for comp in ('110% cotton', '100% cotton, 10% polyester', '1000% cotton', '-5% cotton, 105% polyester'):
            with self.subTest(comp=comp):
                data = self.scan([self.product(composition=comp)])
                self.assertIsNone(data['read'][0]['score'])
                self.assertFalse(data['brand']['publish_eligible'])

    def test_complete_components_are_unweighted_not_an_invalid_total(self):
        data = self.scan([self.product(composition='Shell: 100% cotton; Lining: 100% polyester')],
                         catalogue={'complete': True, 'confirmed_total': 1})
        row = data['read'][0]
        self.assertIsNone(row['shares'])
        self.assertIsNone(row['score'])
        self.assertFalse(row['composition_over_100'])
        self.assertIn('partial_composition', data['brand']['publication_blockers'])

    def test_conflicting_sources(self):
        data = self.scan([self.product(composition='100% cotton', body_html='Composition: 100% polyester')])
        self.assertEqual(data['read'][0]['how'], 'evidence-conflict')
        self.assertIsNone(data['brand']['score'])

    def test_mixed_categories_are_read_independently(self):
        data = self.scan([self.product(composition='100% cotton'),
                          self.product(sku='serum', title='Face serum', inci='Aqua'),
                          self.product(sku='choc', title='Cacao bar', ingredients='Cacao, sugar'),
                          self.product(sku='phone', title='iPhone')], department='clothing')
        self.assertEqual([r['department'] for r in data['read']], ['clothing', 'beauty', 'food', 'electronics'])
        self.assertEqual(len(data['department_reports']), 4)
        self.assertIsNone(data['brand']['score'])

    def test_cpt_routing_does_not_fall_back_to_clothing(self):
        for cpt, expected in [('body_bath','beauty'), ('cocoa','food'), ('phone','electronics'), ('tech','electronics'),
                              ('supplements','supplements'), ('pet','pet'), ('bedding','bedding'), ('femcare','femcare')]:
            with self.subTest(cpt=cpt):
                row = self.scan([self.product(title='Product', cpt=cpt)])['read'][0]
                self.assertEqual(row['department'], expected)

    def test_clothing_and_bedding_share_textile_rules(self):
        for cpt, title in [('clothes','Shirt'), ('bedding','Duvet cover')]:
            row = self.scan([self.product(cpt=cpt, title=title, composition='100% cotton')])['read'][0]
            self.assertEqual(row['band'], 'plant')
            self.assertEqual(row['score'], 5)
            self.assertEqual(row['scanner'], 'Universal Throne scanner · textile rules')

    def test_report_destinations_separate_shared_readers(self):
        data = self.scan([
            self.product(cpt='clothes', composition='100% cotton'),
            self.product(sku='bed', cpt='bedding', title='Duvet', composition='100% cotton'),
            self.product(sku='beauty', cpt='beauty', title='Face serum', inci='Aqua'),
            self.product(sku='bath', cpt='body_bath', title='Body wash', inci='Aqua'),
        ], catalogue={'complete': True, 'confirmed_total': 4})
        self.assertEqual([r['report_destination']['cpt'] for r in data['department_reports']],
                         ['clothes', 'bedding', 'beauty', 'body_bath'])
        self.assertEqual([r['report_destination']['category'] for r in data['department_reports']],
                         ['clothing', 'bedding', 'beauty', 'beauty'])

    def test_wrong_or_retired_destination_blocks_publication(self):
        for target in ('data', 'bedding', 'nonexistent'):
            data = self.scan([self.product(composition='100% cotton')], report_cpt=target,
                             catalogue={'complete': True, 'confirmed_total': 1})
            self.assertFalse(data['brand']['publish_eligible'])
            self.assertIn('report_destination_unresolved', data['brand']['publication_blockers'])

    def test_unrecognized_prefix_and_unrelated_discount(self):
        row = self.scan([self.product(composition='5% metallic fibres, 100% cotton')])['read'][0]
        self.assertIsNone(row['score'])
        for composition in ('100% cotton. ' + 'Care instructions for this garment. ' * 2 + '-20% off',
                            '-20% off today. Composition: 100% cotton'):
            row = self.scan([self.product(composition=composition)])['read'][0]
            self.assertEqual(row['band'], 'plant')

    def test_mixed_forward_inverse_component_pairs(self):
        from throne_read import _seg_pairs
        for text in ('Cotton 95%, 5% elastane', '95% cotton, elastane 5%', '95% cotton, 5% elastane'):
            self.assertEqual(_seg_pairs(text), [(95, 'cotton'), (5, 'elastane')])

    def test_kent_copy_matches_owner_template_only_with_verified_result(self):
        data = self.scan([self.product(composition='100% cotton')], brandName='Kent',
                         catalogue={'complete': True, 'confirmed_total': 1})
        copy = data['department_reports'][0]['publication_copy']
        self.assertEqual(copy['status'], 'ready')
        self.assertEqual(copy['fields']['title'], 'Kent')
        self.assertEqual(copy['fields']['excerpt'], 'Kent sells fully natural clothing that we checked fibre by fibre for natural, non toxic materials. See its verified sustainability rating and review to help you buy natural, non toxic clothing.')
        self.assertEqual(copy['fields']['rank_math_description'], 'Kent clothing rated fully natural. See its verified sustainability rating and review to buy natural, non toxic clothing.')
        blend = self.scan([self.product(composition='95% cotton, 5% elastane')], brandName='Other Brand',
                          catalogue={'complete': True, 'confirmed_total': 1})
        self.assertNotIn('fully natural', str(blend['department_reports'][0]['publication_copy']))
        pending = self.scan([self.product(composition='100% cotton')], brandName='Kent')
        self.assertIsNone(pending['department_reports'][0]['publication_copy']['fields'])

    def test_explicit_product_cpt_cannot_override_required_destination(self):
        data = self.scan([self.product(cpt='body_bath', title='Body wash', inci='Aqua')],
                         report_cpt='beauty', catalogue={'complete': True, 'confirmed_total': 1})
        self.assertFalse(data['brand']['publish_eligible'])
        self.assertIn('report_destination_unresolved', data['brand']['publication_blockers'])

    def test_beauty_brand_rate_uses_product_passes(self):
        from department_readers import _brand
        report = _brand([{'natural_pct': 100, 'passes': True},
                         {'natural_pct': 75, 'passes': False}], 'beauty', True)
        self.assertEqual(report['naturalPct'], 50)
        self.assertIn('50% of assessed products', report['headline'])
        self.assertEqual(report['score'], 2)

    def test_unknown_category_returns_an_explanation(self):
        row = self.scan([self.product(title='Mystery product')])['read'][0]
        self.assertEqual(row['department'], 'unknown')
        self.assertEqual(row['evidence_status'], 'category_not_identified')

    def test_promotional_and_nested_product_schema_are_rejected(self):
        for row in [self.product(title='Best clothes this season'),
                    {'title': 'Editorial', 'schema_type': 'ProductCollection'},
                    {'product_json': {'@type': 'ItemList', 'itemListElement': [{'@type': 'Product', 'name': 'Tee', 'offers': {'price': 5}}]}}]:
            self.assertFalse(confirm_product(row)[0])

    def test_duplicates_do_not_inflate_catalogue(self):
        item = self.product(composition='100% cotton')
        data = self.scan([item, item])
        self.assertEqual(data['brand']['catalogue_total'], 1)
        self.assertEqual(data['duplicate_count'], 1)
        conflict = self.scan([item, self.product(composition='100% polyester')])
        self.assertEqual(conflict['read'][0]['how'], 'evidence-conflict')
        self.assertFalse(conflict['brand']['publish_eligible'])

    def test_accessories_and_nontextiles_are_accounted_but_not_reported(self):
        items = [
            self.product(sku='shirt', title='Essential Bra', composition='100% cotton'),
            self.product(sku='bag', title='Essential Laundry Bag',
                         fetch_status='timeout', material='organic cotton'),
            self.product(sku='pads', title='Sustainable Bra Pads | Triangle',
                         fetch_status='timeout', material='100% recycled polyester cover'),
            self.product(sku='lint', title='Lint Roller', fetch_status='timeout'),
        ]
        data = self.scan(items, catalogue={'complete': True, 'confirmed_total': 4})

        self.assertEqual(data['catalogue_read_count'], 4)
        self.assertEqual(
            data['report_read_count'], 1,
            json.dumps({
                'report': [(row.get('sku'), row.get('title'), row.get('terminal_state'),
                            row.get('product_scope'), row.get('report_included'))
                           for row in data.get('read', [])],
                'excluded': data.get('excluded_products', []),
            }, sort_keys=True),
        )
        self.assertEqual([row['sku'] for row in data['read']], ['shirt'])
        self.assertEqual(data['brand']['catalogue_total'], 4)
        self.assertEqual(data['brand']['eligible_total'], 1)
        self.assertEqual(data['brand']['excluded_accessory'], 2)
        self.assertEqual(data['brand']['excluded_nontextile'], 1)
        self.assertEqual(data['brand']['out_of_scope_count'], 3)
        self.assertTrue(data['brand']['publish_eligible'])
        self.assertNotIn('partial_composition', data['brand']['publication_blockers'])
        self.assertNotIn('technical_or_evidence_errors', data['brand']['publication_blockers'])
        excluded = {row['sku']: row['terminal_state'] for row in data['excluded_products']}
        self.assertEqual(excluded, {
            'bag': 'excluded_accessory',
            'pads': 'excluded_accessory',
            'lint': 'excluded_nontextile',
        })

    def test_out_of_scope_products_do_not_affect_textile_price_coverage(self):
        data = self.scan([
            self.product(sku='shirt', title='Shirt', composition='100% cotton',
                         price=25, currency='USD'),
            self.product(sku='bag', title='Laundry Bag', price=None),
            self.product(sku='pads', title='Bra Pads', price=None),
        ], catalogue={'complete': True, 'confirmed_total': 3})
        report = data['department_reports'][0]
        self.assertEqual(report['price_coverage_pct'], 100.0)
        self.assertEqual(data['report_read_count'], 1)

    def test_fetch_failure_is_not_brand_nondisclosure(self):
        row = self.scan([self.product(fetch_status='timeout', composition='100% cotton')])['read'][0]
        self.assertEqual(row['evidence_state'], 'technical_failure')
        self.assertEqual(row['how'], 'unread-timeout')

    def test_publish_gate_requires_enumeration_proof(self):
        item = self.product(composition='100% cotton')
        self.assertFalse(self.scan([item])['brand']['publish_eligible'])
        self.assertTrue(self.scan([item], catalogue={'complete': True, 'confirmed_total': 1})['brand']['publish_eligible'])
        self.assertFalse(self.scan([item], trusted=False, catalogue={'complete': True, 'confirmed_total': 1})['brand']['publish_eligible'])

    def test_browser_groups_cannot_leak_authoritative_scores(self):
        data = self.scan([self.product(collection='A', composition='100% cotton')], trusted=False)
        self.assertEqual(data['groups'], [])
        self.assertTrue(all(r['score'] is None for r in data['department_reports']))

    def test_rounding_cannot_award_a_perfect_catalogue_score(self):
        import textile_policy as policy
        rows = [{'band':'plant', 'how':'own', 'composition':'100% cotton'} for _ in range(2499)]
        rows.append({'band':'synthetic', 'how':'own', 'composition':'100% polyester'})
        self.assertEqual(policy.api_brand_figure(rows)['score'], 4)

    def test_swimwear_uses_ordinary_textile_metric(self):
        data = self.scan([
            self.product(composition='100% cotton'),
            self.product(
                sku='swim',
                title='Bikini',
                composition='80% cotton, 20% elastane',
            ),
        ])
        self.assertEqual(len(data['department_reports']), 1)
        swim = data['read'][1]
        self.assertEqual(swim['score'], 3)
        self.assertEqual(
            swim['scoring_model'],
            'textile-composition-plant-share-v1',
        )
        self.assertIsNone(swim.get('swimwear_score'))

    def test_single_ingredient_beauty_and_unknown_tokens(self):
        self.assertEqual(self.scan([self.product(title='Face serum', inci='Aqua')])['read'][0]['assessment_status'], 'assessed')
        for inci in ('Aqua, mysterychemical', 'Aqua, Vegan', 'Plant based surfactants', 'Aqua, may contain parfum'):
            self.assertIsNone(self.scan([self.product(title='Face serum', inci=inci)])['read'][0]['score'])

    def test_price_currency_preference_and_actual_usd_fallback(self):
        product = {'product_json': {'@type': 'Product', 'offers': [
            {'price': '30', 'priceCurrency': 'USD'}, {'price': '25', 'priceCurrency': 'GBP'}]}}
        self.assertEqual(capture_price(product, 'GBP')['price'], 25)
        self.assertEqual(capture_price(product, 'EUR')['price'], 30)
        self.assertEqual(capture_price({'price': '10'})['currency'], '')
        self.assertEqual(capture_price({'price': 0, 'currency': 'GBP'})['price'], 0)
        for value in ('NaN', 'Infinity', -1, True):
            self.assertIsNone(capture_price({'price': value})['price'])

    def test_variant_offer_range_is_not_one_sku_price(self):
        item = {'product_json':{'@type':'Product','offers':[{'price':10,'priceCurrency':'GBP'}, {'price':20,'priceCurrency':'GBP'}]}}
        result = capture_price(item, 'GBP')
        self.assertIsNone(result['price'])
        self.assertEqual((result['price_min'], result['price_max']), (10,20))

    def test_brand_enrichment_has_sources_and_one_landing_image(self):
        data = capture_brand([{'url': 'https://brand.test/', 'html': '<meta property="og:image" content="/social.jpg"><img class="hero" src="/hero.jpg"><a href="https://instagram.com/brand">Instagram</a><a href="/impact.pdf">Impact report</a><p>Owned by Example Group.</p>'}])
        self.assertEqual(data['featured_image_candidate']['url'], 'https://brand.test/hero.jpg')
        self.assertEqual(data['social_links'][0]['source_url'], 'https://brand.test/')
        self.assertEqual(len(data['report_links']), 1)
        self.assertEqual(len(data['ownership_evidence']), 1)

    def test_unquantified_material_cannot_earn_purity_credit(self):
        data = self.scan([self.product(material='cotton')])
        self.assertIsNone(data['brand']['plantPct'])
        self.assertIsNone(data['read'][0]['passes'])

    def test_beauty_conflicting_declarations_are_unresolved(self):
        row = self.scan([self.product(title='Face serum', inci='Aqua', ingredients='Glycerin')])['read'][0]
        self.assertEqual(row['evidence_status'], 'evidence-conflict')
        self.assertIsNone(row['score'])

    def test_replay_resumes_and_invalidates_changed_evidence(self):
        import tempfile
        from rescore_catalogues import replay
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.jsonl'
            output = Path(directory) / 'results'
            record = {'brand_id':'brand', 'payload':{'items':[self.product(composition='100% cotton')]}}
            source.write_text(json.dumps(record) + '\n')
            self.assertEqual(replay(source, output)['read'], 1)
            self.assertEqual(replay(source, output)['reused'], 1)
            record['payload']['items'][0]['composition'] = '100% polyester'
            source.write_text(json.dumps(record) + '\n')
            self.assertEqual(replay(source, output)['read'], 1)

    def test_aggregate_can_return_a_compact_report(self):
        result = handler(event('POST', '/v1/aggregate', {
            'items':[self.product(composition='100% cotton')], 'include_rows':False,
            'catalogue':{'complete':True, 'confirmed_total':1}}, {'authorization':'Bearer [REDACTED]'}), None)
        self.assertEqual(result['statusCode'], 200)
        data = json.loads(result['body'])
        self.assertEqual(data['read_count'], 1)
        self.assertEqual(data['read'], [])
        self.assertTrue(data['brand']['publish_eligible'])

    def test_compact_report_retains_counts_without_per_product_notes(self):
        result = handler(event('POST', '/v1/aggregate', {
            'items': [self.product(), {'title': 'Best clothes this season', 'url': 'https://brand.test/collections/all'}], 'include_rows': False}, {'authorization': 'Bearer [REDACTED]'}), None)
        data = json.loads(result['body'])
        self.assertEqual(len(data['report_notes']), 1)
        self.assertNotIn('sku', data['report_notes'][0])
        self.assertEqual(data['report_notes_count'], 2)
        self.assertEqual(sum(data['note_counts'].values()), 2)

    def test_replay_exact_byte_limit_accepts_line_endings(self):
        import tempfile
        import rescore_catalogues as module
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.jsonl'
            valid = json.dumps({'brand_id': 'brand', 'payload': {'items': [self.product(composition='100% cotton')]}}).encode()
            for index, ending in enumerate((b'', b'\n', b'\r\n')):
                source.write_bytes(valid + ending)
                with patch.object(module, 'MAX_BODY_BYTES', len(valid)):
                    self.assertEqual(module.replay(source, Path(directory) / str(index))['read'], 1)

    def test_trailing_statistics_do_not_invalidate_composition(self):
        for tail in ('30% less water', 'save 30% off', 'about 30% renewable energy'):
            with self.subTest(tail=tail):
                row = self.scan([self.product(composition='100% cotton, ' + tail)])['read'][0]
                self.assertEqual(row['band'], 'plant')
                self.assertEqual(row['score'], 5)

    def test_equivalent_full_ingredient_labels_do_not_conflict(self):
        row = self.scan([self.product(title='Face serum', inci='Full ingredients: Aqua', ingredients='INCI: Aqua')])['read'][0]
        self.assertEqual(row['assessment_status'], 'assessed')

    def test_oversized_replay_record_does_not_discard_following_brands(self):
        import tempfile
        import rescore_catalogues as replay_module
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.jsonl'
            output = Path(directory) / 'results'
            valid = json.dumps({'brand_id':'brand', 'payload':{'items':[self.product(composition='100% cotton')]}})
            limit = len(valid.encode()) + 1
            source.write_text('x' * (limit * 3) + '\n' + valid + '\n')
            with patch.object(replay_module, 'MAX_BODY_BYTES', limit):
                stats = replay_module.replay(source, output)
            self.assertEqual(stats, {'read':1, 'reused':0, 'failed':1})
            self.assertEqual(json.loads((output / 'replay-summary.json').read_text()), stats)
            self.assertEqual(json.loads((output / 'error-line-1.json').read_text())['line'], 1)

    def test_nonfinite_server_result_is_a_server_error(self):
        with patch('scan_contract.scan', return_value={'result':float('nan')}):
            result = handler(event('POST', '/v1/read', {'items':[self.product()]},
                                   {'authorization':'Bearer [REDACTED]'}), None)
        self.assertEqual(result['statusCode'], 500)
        self.assertEqual(json.loads(result['body'])['error'], 'The reader could not serialize its result.')

    def test_partial_swim_components_receive_no_quantified_credit(self):
        import textile_policy as policy
        row = policy.annotate_row({'product':'Bikini', 'band':'blended', 'how':'component',
                                   'composition':'100% cotton; polyurethane coating (percentage not stated)'})
        self.assertTrue(row['composition_partial'])
        self.assertIsNone(row['swimwear_natural_fibre_pct'])
        self.assertIsNone(row['swimwear_score'])

    def test_failed_cached_plant_row_does_not_earn_plant_credit(self):
        import textile_policy as policy
        result = policy.canonicalize_result({'rows':[{'band':'plant','how':'unread','composition':'100% cotton'}]})
        self.assertEqual(result['pure_plant'], 0)
        self.assertEqual(result['technical_failures'], 1)
        self.assertIsNone(result['score'])

    def test_malformed_payload_is_a_client_error(self):
        result = handler(event('POST', '/v1/read', [], {'authorization':'Bearer [REDACTED]'}), None)
        self.assertEqual(result['statusCode'], 400)


if __name__ == '__main__':
    unittest.main()
