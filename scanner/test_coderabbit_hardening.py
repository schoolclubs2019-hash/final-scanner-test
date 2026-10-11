"""Regression checks for CodeRabbit durability and brand-isolation fixes."""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from authority_batch import worker
from checkpoint_io import checkpoint_json
from commerce_capture import CommerceEvidenceError, shopify_ajax_capture


class JSONResponse:
    def __init__(self, url, payload):
        self.url = url
        self.text = json.dumps(payload)
    def json(self):
        return json.loads(self.text)


class HardeningTests(unittest.TestCase):
    def test_malformed_commerce_payloads_are_reported_not_crashes(self):
        item = {'url': 'https://example.org/products/shirt', 'product_id': '12'}
        product_url = item['url'] + '.js'
        cart_url = 'https://example.org/cart.js'
        for product, cart in (([], {}), ({}, []), ('not-object', {}), ({}, 'not-object')):
            with self.subTest(product=product, cart=cart):
                with self.assertRaisesRegex(CommerceEvidenceError, 'commercial_payload_schema_invalid'):
                    shopify_ajax_capture(item, JSONResponse(product_url, product),
                                         JSONResponse(cart_url, cart), '2026-10-11', 'market')

    def test_checkpoint_overwrite_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            checkpoint_json(path, {'value': 1})
            checkpoint_json(path, {'value': 'révisé', 'list': [1, 2]})
            self.assertEqual(json.loads(path.read_text()), {'value': 'révisé', 'list': [1, 2]})
            self.assertFalse(path.with_name(path.name + '.tmp').exists())

    def test_brand_exception_does_not_starve_next_brand(self):
        brands = [
            ({'brand': 'Broken', 'brand_id': '1', 'htt_id': '1', 'domain': 'https://example.org'}, None),
            ({'brand': 'Working', 'brand_id': '2', 'htt_id': '2', 'domain': 'https://example.net'}, None),
        ]
        store = Mock()
        store.queue.return_value = brands
        store.lock.return_value = contextlib.nullcontext()
        saved = {'brand': 'Working', 'brand_id': '2', 'scan_run_id': 'receipt', 'status': 'SCANNER_COMPLETE'}
        with patch('authority_batch.Store', return_value=store), \
             patch('authority_batch.scan_one', side_effect=[ValueError('first brand failed'), saved]) as scan, \
             patch('authority_batch.export_visit_audit', return_value=saved), \
             patch('authority_batch.queue_run', return_value=(None, None)):
            rows = worker('/tmp/isolated', 'W1', None, render=False, publish=False)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['worker_error'], 'ValueError')
        self.assertFalse(rows[1].get('worker_error'))
