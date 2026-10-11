"""Rendered composition uses the same positive evidence and canonical reader gates."""
import json
import unittest
from unittest.mock import Mock

from catalogue_discovery import capture, Response, FetchError
from scan_contract import read_products
from test_pipeline import product

URL = 'https://brand.test/products/p1'


def schema(**values):
    data = {'@type': 'Product', 'url': URL, 'name': 'Merino bra', 'sku': 'BRA-1', **values}
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


class RenderedCompositionTest(unittest.TestCase):
    def read(self, html, rendered, **overrides):
        renderer = Mock(return_value=rendered)
        item = capture(product(title='Merino bra', body_html='', **overrides),
                       Response(URL, html, {}), renderer, 'clothing')
        renderer.assert_called_once_with(URL)
        return item, read_products([item], {'department': 'clothing'}, True)[0][0]

    def test_rendered_additional_property_is_read_like_static_schema(self):
        rendered = schema(additionalProperty=[{'name': 'Fabric', 'value': '85% merino wool, 15% nylon'}])
        item, row = self.read('<main>Loading</main>', rendered)
        static = capture(product(title='Merino bra', body_html=''), Response(URL, rendered, {}), None, 'clothing')
        static_row = read_products([static], {'department': 'clothing'}, True)[0][0]
        self.assertEqual((row['terminal_state'], row['score'], row['composition']),
                         (static_row['terminal_state'], static_row['score'], static_row['composition']))
        self.assertEqual(row['terminal_state'], 'scored')
        self.assertTrue(any(s['source'] == 'rendered:jsonld:Fabric' for s in item['evidence_sources']))

    def test_partial_static_declaration_gets_bounded_deeper_read(self):
        item, row = self.read(schema(material='85% merino wool'),
                              schema(material='85% merino wool, 15% nylon'))
        self.assertEqual(row['terminal_state'], 'scored')
        self.assertEqual(row['composition'], '85% merino wool, 15% nylon')
        self.assertTrue(any(s['raw_original'] == '85% merino wool' for s in item['evidence_sources']))

    def test_still_partial_and_conflicting_composition_remain_unscored(self):
        for material, state in [('85% merino wool', 'partial_composition'),
                                ('110% merino wool', 'evidence_conflict')]:
            with self.subTest(material=material):
                _, row = self.read(schema(material='85% merino wool'), schema(material=material))
                self.assertEqual(row['terminal_state'], state)
                self.assertIsNone(row['score'])

    def test_invalid_deeper_declaration_preserves_original_and_conflicting_source(self):
        for rendered in (schema(material='110% merino wool'),
                         schema(additionalProperty=[{'name': 'Fabric', 'value': '85% merino wool, 30% nylon'}]),
                         '<main><details><summary>Materials</summary>110% merino wool</details></main>'):
            with self.subTest(rendered=rendered):
                item, row = self.read(schema(material='85% merino wool'), rendered)
                self.assertEqual(item['material'], '85% merino wool')
                self.assertEqual(row['terminal_state'], 'evidence_conflict')
                self.assertEqual(row['how'], 'invalid-composition-total')
                self.assertIsNone(row['score'])
                self.assertTrue(any(source['raw_original'] == '85% merino wool'
                                    for source in item['evidence_sources']))
                self.assertTrue(any('110%' in source['raw_original'] or '30% nylon' in source['raw_original']
                                    for source in item['evidence_sources']))

    def test_schema_free_rendered_identity_routes_category_before_scoring(self):
        html = ('<meta property="og:type" content="product"><meta property="og:url" content="' + URL + '">'
                '<meta property="og:title" content="Merino bra"><meta property="product:retailer_item_id" content="BRA-1">'
                '<main><details><summary>Materials</summary>85% merino wool, 15% nylon</details></main>')
        renderer = Mock(return_value=html)
        item = capture({'product_id': '1', 'url': URL}, Response(URL, '<main>Loading</main>', {}), renderer, 'clothing')
        row = read_products([item], {'department': 'clothing'}, True)[0][0]
        self.assertEqual(item['title'], 'Merino bra')
        self.assertEqual(row['terminal_state'], 'scored')
        renderer.assert_called_once_with(URL)

    def test_rendered_ambiguous_identity_is_not_overwritten(self):
        renderer = Mock(return_value=schema(sku='A') + schema(sku='B'))
        with self.assertRaisesRegex(FetchError, 'ambiguous_product_identity'):
            capture(product(body_html=''), Response(URL, '', {}), renderer, 'clothing')


if __name__ == '__main__':
    unittest.main()
