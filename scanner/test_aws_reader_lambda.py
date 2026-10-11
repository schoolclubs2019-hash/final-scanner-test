import json
import os
import unittest

import aws_reader_lambda as L
import throne_api as A


def event(method, path, payload=None, headers=None):
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": headers or {},
        "body": json.dumps(payload) if payload is not None else "",
        "isBase64Encoded": False,
    }


class AwsReaderLambdaTest(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        env = patch.dict(os.environ, {"THRONE_API_KEY": "test-secret"})
        env.start()
        self.addCleanup(env.stop)

    def test_public_standard_matches_scoring_source(self):
        from throne_standard import STANDARD
        result = L.handler(event("GET", "/v1/standard"), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(json.loads(result["body"]), STANDARD)

    def test_health_exposes_fingerprint(self):
        result = L.handler(event("GET", "/v1/health"), None)
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["service"], "aws-lambda")
        self.assertEqual(len(body["reader"]), 16)
        self.assertEqual(body["throne_standard_version"], A.TP.POLICY_VERSION)
        self.assertTrue(body["scanner_version"])
        self.assertTrue(body["reader_version"])

    def test_browser_identity_can_read(self):
        payload = {"department": "clothing", "items": [{
            "sku": "shirt-1", "title": "Shirt", "handle": "shirt",
            "body_html": "Composition: 100% cotton", "page_text": "",
            "tags": "", "url": "https://example.com/products/shirt",
        }]}
        result = L.handler(event("POST", "/v1/read", payload, {
            "x-throne-install-id": "12345678-1234-1234-1234-123456789012",
        }), None)
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["read"][0]["band"], "plant")
        self.assertFalse(body["read"][0]["authoritative"])
        self.assertIsNone(body["brand"]["score"])
        self.assertEqual(body["brand"]["status"], "client_unverified")
        evidence = body["read"][0]["evidence_record"]
        self.assertEqual(evidence["accepted_text"], "Composition: 100% cotton" if False else "100% cotton")
        self.assertEqual(evidence["source_url"], "https://example.com/products/shirt")

    def test_client_score_status_and_reader_hints_are_ignored(self):
        payload = {"department": "clothing", "items": [{
            "sku": "styled-item", "title": "Top",
            "body_html": "Pair this with linen shorts",
            "url": "https://example.com/products/top",
            "band": "plant", "score": 5, "status": "verified",
            "confidence": 100, "verified": True, "reader_how": "plant",
        }]}
        result = L.handler(event("POST", "/v1/read", payload, {
            "x-throne-install-id": "12345678-1234-1234-1234-123456789012",
        }), None)
        row = json.loads(result["body"])["read"][0]
        self.assertEqual(row["band"], "undisclosed")
        self.assertEqual(row["how"], "undisclosed")

    def test_api_brand_figure_separates_composition_from_coverage(self):
        figure = L.brand_figure([
            {"product": "Cotton shirt", "band": "plant", "how": "own", "composition": "100% cotton"},
            {"product": "Unknown shirt", "band": "undisclosed", "how": "undisclosed"},
        ])
        self.assertEqual(figure["plantPct"], 100.0)
        self.assertEqual(figure["composition_coverage_pct"], 50.0)
        self.assertIsNone(figure["score"])
        self.assertEqual(figure["disclosureRate"], 50.0)
        self.assertNotIn("confidence", figure)

    def test_server_bearer_can_read(self):
        payload = {"department": "clothing", "items": [{"sku": "x", "title": "Unknown", "url": "https://example.com/x"}]}
        result = L.handler(event("POST", "/v1/read", payload, {
            "authorization": "Bearer [REDACTED]",
        }), None)
        self.assertEqual(result["statusCode"], 200)

    def test_unauthorised_and_oversized_batches_are_rejected(self):
        self.assertEqual(L.handler(event("POST", "/v1/read", {"items": [{}]}), None)["statusCode"], 401)
        result = L.handler(event("POST", "/v1/read", {"items": [{}] * 301}, {
            "authorization": "Bearer [REDACTED]",
        }), None)
        self.assertEqual(result["statusCode"], 400)

    def test_aggregate_accepts_more_than_a_reader_batch(self):
        items = [{"sku": f"x-{i}", "title": "Cotton", "body_html": "100% cotton", "url": f"https://example.com/{i}"} for i in range(301)]
        result = L.handler(event("POST", "/v1/aggregate", {"items": items}, {
            "authorization": "Bearer [REDACTED]",
        }), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(json.loads(result["body"])["brand"]["assessed"], 301)

    def test_browser_identity_cannot_request_authoritative_aggregate(self):
        payload = {"items": [{"title": "Cotton shirt", "body_html": "100% cotton"}]}
        result = L.handler(event("POST", "/v1/aggregate", payload, {
            "x-throne-install-id": "12345678-1234-1234-1234-123456789012",
        }), None)
        self.assertEqual(result["statusCode"], 403)

    def test_beauty_reader_uses_inci_and_never_textile_rules(self):
        payload = {"items": [{
            "sku": "serum-1", "title": "Botanical serum",
            "description": "Ingredients: Aqua, Glycerin, Aloe Barbadensis Leaf Extract, Phenoxyethanol",
            "url": "https://example.com/serum",
        }]}
        payload["department"] = "beauty"
        result = L.handler(event("POST", "/v1/read", payload, {
            "x-throne-install-id": "12345678-1234-1234-1234-123456789012",
        }), None)
        data = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(data["brand"]["department"], "beauty")
        self.assertEqual(data["brand"]["scanner"], "Universal Throne scanner · beauty rules")
        self.assertEqual(data["read"][0]["assessment_status"], "assessed")
        self.assertIsNone(data["brand"]["score"])
        self.assertNotIn("plantPct", data["brand"])

    def test_food_reader_and_server_aggregate(self):
        payload = {"items": [{
            "sku": "bar-1", "title": "Cacao bar",
            "body_html": "<h3>Ingredients</h3><p>Cacao beans, cane sugar, cocoa butter</p>",
            "url": "https://example.com/bar",
        }]}
        payload["department"] = "food"
        read = L.handler(event("POST", "/v1/read", payload, {
            "x-throne-install-id": "12345678-1234-1234-1234-123456789012",
        }), None)
        self.assertEqual(read["statusCode"], 200)
        self.assertIsNone(json.loads(read["body"])["brand"]["score"])
        aggregate = L.handler(event("POST", "/v1/aggregate", payload, {
            "authorization": "Bearer [REDACTED]",
        }), None)
        data = json.loads(aggregate["body"])
        self.assertEqual(aggregate["statusCode"], 200)
        self.assertEqual(data["brand"]["department"], "food")
        self.assertEqual(data["brand"]["scanner"], "Universal Throne scanner · food rules")
        self.assertIsNone(data["brand"]["score"])
        self.assertEqual(data["read"][0]["assessment_status"], "captured_for_validation")

    def test_homeware_capture_never_publishes_a_score(self):
        result = L.handler(event("POST", "/v1/aggregate", {
            "department": "homeware",
            "items": [{"product_id": 3, "title": "Ceramic bowl", "material": "Stoneware",
                       "price": 28, "url": "https://example.com/products/bowl"}],
        }, {"authorization": "Bearer [REDACTED]"}), None)
        data = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertIsNone(data["brand"]["score"])
        self.assertFalse(data["brand"]["publishing_enabled"])
        self.assertEqual(data["read"][0]["material_evidence"], "Stoneware")

    def test_discovery_rejections_stay_out_of_read_and_denominator(self):
        result = L.handler(event("POST", "/v1/aggregate", {
            "department": "clothing",
            "items": [
                {"product_id": 4, "title": "Cotton shirt", "url": "https://example.com/products/shirt",
                 "body_html": "100% cotton"},
                {"title": "Shop The Look", "url": "https://example.com/collections/looks"},
            ],
        }, {"authorization": "Bearer [REDACTED]"}), None)
        data = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(data["read"]), 1)
        self.assertEqual(data["discovery_candidates_count"], 2)
        self.assertEqual(data["discovery_rejected_count"], 1)
        self.assertEqual(data["brand"]["catalogue_total"], 1)

    def test_hotel_route_captures_evidence_without_guessing(self):
        payload = {"items": [{"product_id": 1, "title": "Garden suite", "url": "https://example.com/rooms/garden"}]}
        payload["department"] = "hotels"
        result = L.handler(event("POST", "/v1/read", payload, {
            "x-throne-install-id": "12345678-1234-1234-1234-123456789012",
        }), None)
        data = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(data["brand"]["department"], "hotels")
        self.assertEqual(data["brand"]["scanner"], "Universal Throne scanner · hotel rules")
        self.assertIsNone(data["brand"]["score"])
        self.assertEqual(data["read"][0]["assessment_status"], "captured_for_review")

    def test_retired_department_routes_are_gone(self):
        retired_route = "/v1/" + "read-" + "beauty"
        result = L.handler(event("POST", retired_route, {"items": [{}]}, {
            "authorization": "Bearer [REDACTED]",
        }), None)
        self.assertEqual(result["statusCode"], 404)

    def test_standalone_reader_requires_key_when_exposed_off_loopback(self):
        A.validate_bind('127.0.0.1', '')
        with self.assertRaisesRegex(ValueError, 'THRONE_API_KEY'):
            A.validate_bind('0.0.0.0', '')
        A.validate_bind('0.0.0.0', 'configured')


if __name__ == "__main__":
    unittest.main()
