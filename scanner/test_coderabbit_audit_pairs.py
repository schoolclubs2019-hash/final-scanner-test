"""CodeRabbit regression tests for captured first-party commerce-pair audit evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from catalogue_audit import export_audit
from scanner_store import Store


class CommercePairAuditTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.db = self.folder / 'state.db'
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.run = self.store.create({'brand_id': '7', 'htt_id': '42', 'brand': 'Audit',
                                      'domain': 'https://merchant.example', 'cpt': 'clothing'},
                                     'fingerprint', 'scanner', 'reader', 'standard')
        self.run_id = self.run['scan_run_id']
        self.key = 'commerce-pair:https://merchant.example/products/shirt'

    def save_pair(self, pair):
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO pages(run_id,url,data) VALUES(?,?,?)',
                                  (self.run_id, self.key,
                                   json.dumps({'pair': pair, 'context_id': 'anon-market',
                                               'checked_at': '2026-10-11T00:00:00Z'})))

    def pair(self):
        return [
            {'url': 'https://merchant.example/cart.js', 'text': '{"currency":"USD"}',
             'headers': {'content-type': 'application/json', 'set-cookie': 'SECRET_COOKIE'}},
            {'url': 'https://merchant.example/products/shirt.js', 'text': '{"id":123}',
             'headers': {'content-type': 'application/json', 'authorization': 'SECRET_TOKEN'}},
        ]

    def test_complete_pair_exports_both_and_strips_secret_headers(self):
        self.save_pair(self.pair())
        result = export_audit(self.db, self.run_id, self.folder / 'valid')
        docs = [d for d in result['documents'] if d.get('cache_key') == self.key]
        self.assertEqual({d['kind'] for d in docs}, {'commerce_cart', 'commerce_product'})
        self.assertEqual(len(docs), 2)
        for doc in docs:
            self.assertNotIn('set-cookie', doc['headers'])
            self.assertNotIn('authorization', doc['headers'])

    def test_partial_pair_is_rejected(self):
        self.save_pair(self.pair()[:1])
        with self.assertRaises(ValueError):
            export_audit(self.db, self.run_id, self.folder / 'incomplete')

    def test_non_json_commercial_capture_is_rejected(self):
        invalid = self.pair()
        invalid[1]['text'] = 'not valid JSON'
        self.save_pair(invalid)
        with self.assertRaises(ValueError):
            export_audit(self.db, self.run_id, self.folder / 'invalid')
