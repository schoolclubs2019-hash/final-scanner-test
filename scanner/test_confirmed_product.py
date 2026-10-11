import unittest

from confirmed_product import confirm_product, filter_confirmed_products


class ConfirmedProductGateTest(unittest.TestCase):
    def test_listing_pages_are_rejected_without_product_evidence(self):
        titles = (
            "Top Picks",
            "Women's Top Picks",
            "Men's Top Picks",
            "Shop The Look",
            "Ropa deportiva y de entrenamiento para mujeres",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertFalse(confirm_product({
                    "title": title,
                    "url": "https://example.com/collections/activewear",
                    "handle": title.lower().replace(" ", "-"),
                })[0])

    def test_category_landing_url_is_rejected(self):
        self.assertFalse(confirm_product({
            "title": "Activewear",
            "url": "https://example.com/category/activewear",
        })[0])

    def test_platform_product_and_variant_ids_are_positive_evidence(self):
        for field in ("product_id", "variant_id"):
            with self.subTest(field=field):
                self.assertTrue(confirm_product({
                    field: "product-123",
                    "title": "Cotton tee",
                })[0])

    def test_sku_and_product_evidence_are_accepted(self):
        self.assertTrue(confirm_product({
            "sku": "TEE-123",
            "title": "Cotton tee",
            "url": "https://example.com/products/cotton-tee",
            "body_html": "100% cotton",
        })[0])

    def test_product_json_ld_is_accepted(self):
        self.assertTrue(confirm_product({
            "title": "Cotton tee",
            "url": "https://example.com/products/cotton-tee",
            "product_json": {"@type": "Product", "name": "Cotton tee"},
        })[0])

    def test_verified_detail_url_with_commerce_evidence_is_accepted(self):
        self.assertTrue(confirm_product({
            "title": "Cotton tee",
            "url": "https://example.com/products/cotton-tee",
            "price": 28,
            "availability": "InStock",
        })[0])

    def test_handle_and_unknown_url_alone_are_rejected(self):
        self.assertFalse(confirm_product({
            "title": "Cotton tee",
            "handle": "cotton-tee",
            "url": "https://example.com/cotton-tee",
        })[0])

    def test_rejected_rows_are_counted_separately(self):
        accepted, reasons = filter_confirmed_products([
            {"product_id": 1, "title": "Cotton tee", "url": "https://example.com/products/tee"},
            {"title": "Top Picks", "url": "https://example.com/collections/tops"},
        ])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(sum(reasons.values()), 1)


if __name__ == "__main__":
    unittest.main()