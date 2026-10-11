import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from authority_batch import scan_one, worker
from production_worker import canary_record
from scan_policy import (DEFAULT_POLICY, ScanPaused, VisitBudget, begin_visit,
                         normalize_policy, pause_visit, revisit_state)
from scanner_pipeline import SCANNER_VERSION, pipeline_fingerprint
from scanner_store import Store
from throne_read import READER_VERSION
from throne_standard import VERSION
from test_authority_batch import brand


class Clock:
    def __init__(self):
        self.at, self.sleeps = 1000.0, []
    def __call__(self):
        return self.at
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.at += seconds


class PolicyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'W1.db')
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.policy = normalize_policy({'first_pass_seconds': 100, 'revisit_seconds': 200,
            'first_inline_wait_seconds': 20, 'revisit_inline_wait_seconds': 30,
            'revisit_delay_seconds': 10, 'workers': 1})
        self.row = {**brand(1, 'One', 'https://one.test'), 'execution_policy': self.policy}
        self.store.enqueue([self.row])
        self.rid = self.store.create(self.row, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
        for target, value in [('scan_policy.time.time', self.clock), ('scan_policy.time.sleep', self.clock.sleep)]:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def begin(self, phase='first'):
        return begin_visit(self.store, self.rid, phase)

    def complete(self, store, rid, http, renderer=None, retry=False, budget=None):
        self.assertIsNotNone(budget)
        store.update(rid, status='SCANNER_COMPLETE', scanner_status='SCANNER_COMPLETE', publish_eligible=False)
        return store.run(rid)

    def test_policy_is_opt_in_and_strict(self):
        self.assertIsNone(normalize_policy(None))
        self.assertEqual(normalize_policy({}), DEFAULT_POLICY)
        for value in ({'workers': True}, {'http_retries': -1}, {'first_pass_seconds': 1.5},
                      {'http_timeout_seconds': float('inf')}, {'typo': 1}, {'workers': 33},
                      {'first_pass_seconds': 5}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_policy(value)

    def test_canary_explicit_record_and_cli_override_configured_retry_default(self):
        policy = {**self.policy, 'authority_retry_limit': 4}
        self.assertEqual(canary_record({}, execution_policy=policy)['authority_retry_limit'], 4)
        self.assertEqual(canary_record({'authority_retry_limit': 5}, execution_policy=policy)['authority_retry_limit'], 5)
        self.assertEqual(canary_record({'authority_retry_limit': 5}, 6, policy)['authority_retry_limit'], 6)

    def test_restart_keeps_original_deadline_and_number(self):
        original = self.begin().visit
        self.clock.at += 40
        resumed = self.begin('revisit')
        self.assertEqual(original, resumed.visit)
        self.assertEqual(resumed.timeout(90), 60)
        self.clock.at += 60
        with self.assertRaises(ScanPaused):
            resumed.check()
        self.assertEqual(self.store.run(self.rid)['scan_visit']['number'], 0)

    def test_due_skip_and_pause_preserve_failure_counters(self):
        self.begin()
        self.store.update(self.rid, worker_attempts=2, worker_retry_at=1100,
                          authority_retry={'url': 'https://one.test/p', 'attempts': 4})
        paused = pause_visit(self.store, self.rid, ScanPaused('domain_backoff', 1200))
        self.assertEqual(paused['worker_attempts'], 2)
        self.assertEqual(paused['authority_retry']['attempts'], 4)
        self.assertEqual(revisit_state(paused, self.clock())[0], 'waiting')
        self.assertIsNone(self.begin('revisit'))
        self.assertEqual(self.store.run(self.rid)['scan_visit']['number'], 0)
        self.clock.at = 1200
        self.assertEqual(self.begin('revisit').visit['number'], 1)
        self.assertEqual(self.store.run(self.rid)['worker_attempts'], 2)

    def test_inline_wait_allowance_is_per_wait_with_cumulative_metric(self):
        budget = self.begin()
        budget.wait_until(1010)
        budget.wait_until(1030)
        with self.assertRaises(ScanPaused) as raised:
            self.begin().wait_until(1051)
        self.assertEqual(raised.exception.eligible_at, 1051)
        self.assertEqual(self.clock(), 1030)
        self.assertEqual(self.store.run(self.rid)['scan_visit']['inline_wait_used'], 30)

    def test_interrupted_sleep_reuses_reserved_wait(self):
        budget = self.begin()
        budget.sleep = lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            budget.wait_until(1020)
        self.clock.at = 1005
        self.begin().wait_until(1020)
        self.assertEqual(self.store.run(self.rid)['scan_visit']['inline_wait_used'], 20)
        self.assertEqual(self.clock(), 1020)

    def test_wait_must_fit_before_visit_deadline(self):
        budget = self.begin()
        with self.assertRaises(ScanPaused):
            budget.wait_until(1100)
        self.assertEqual(self.clock(), 1000)
        self.assertEqual(self.store.run(self.rid)['scan_visit']['inline_wait_used'], 0)

    def test_revisits_have_a_persisted_ceiling(self):
        self.begin()
        for number in range(1, 4):
            pause_visit(self.store, self.rid, ScanPaused())
            self.clock.at += 10
            self.assertEqual(self.begin('revisit').visit['number'], number)
        final = pause_visit(self.store, self.rid, ScanPaused())
        self.clock.at += 10
        self.assertEqual(revisit_state(final, self.clock())[0], 'held')
        self.assertIsNone(self.begin('revisit'))

    def test_request_settings_are_saved_and_applied(self):
        changed = {**self.row, 'execution_policy': {**self.policy, 'http_timeout_seconds': 90}}
        with patch('authority_batch.PublicHTTP', return_value=object()) as http, \
             patch('authority_batch.run_brand', side_effect=self.complete):
            result = scan_one(self.store, changed, self.rid)
        self.assertEqual(http.call_args.kwargs['timeout'], 30)
        self.assertEqual(http.call_args.kwargs['retries'], 2)
        self.assertIsInstance(http.call_args.kwargs['budget'], VisitBudget)
        self.assertEqual(result['execution_policy'], self.policy)
        self.assertEqual(result['scan_visit']['state'], 'FINISHED')

    def test_budget_pause_does_not_spend_failure_attempt(self):
        def exhaust(store, rid, *args, budget=None, **kwargs):
            self.clock.at += 100
            budget.check()
        with patch('authority_batch.PublicHTTP', return_value=object()), \
             patch('authority_batch.run_brand', side_effect=exhaust):
            result = scan_one(self.store, self.row, self.rid)
        self.assertEqual(result['defer_reason'], 'site_pass_budget')
        self.assertEqual(result.get('worker_attempts', 0), 0)
        with patch('authority_batch.PublicHTTP') as http:
            again = scan_one(self.store, self.row, self.rid, phase='revisit')
        http.assert_not_called()
        self.assertEqual(again['scan_visit']['number'], 0)

    def test_worker_restart_cannot_grant_fresh_time(self):
        with patch('authority_batch.PublicHTTP', return_value=object()), \
             patch('authority_batch.run_brand', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                scan_one(self.store, self.row, self.rid)
        original = self.store.run(self.rid)['scan_visit']
        self.clock.at += 100
        with patch('authority_batch.PublicHTTP') as http:
            result = scan_one(self.store, self.row, self.rid)
        http.assert_not_called()
        self.assertEqual(result['scan_visit']['deadline_at'], original['deadline_at'])
        self.assertEqual(result['scan_visit']['number'], 0)

    def test_true_transient_retry_is_counted_once_across_pause(self):
        row = {**self.row, 'execution_policy': {**self.policy, 'first_inline_wait_seconds': 0}}
        self.rid = self.store.create(row, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
        def fail(store, rid, *args, **kwargs):
            store.update(rid, status='RETRY', error='timeout')
            return store.run(rid)
        with patch('authority_batch.PublicHTTP', return_value=object()), \
             patch('authority_batch.run_brand', side_effect=fail):
            result = scan_one(self.store, self.row, self.rid)
        self.assertEqual(result['worker_attempts'], 1)
        self.assertEqual(result['defer_reason'], 'worker_backoff')
        self.clock.at = result['scan_next_eligible_at']
        with patch('authority_batch.PublicHTTP', return_value=object()), \
             patch('authority_batch.run_brand', side_effect=self.complete):
            done = scan_one(self.store, self.row, self.rid, phase='revisit')
        self.assertEqual(done['worker_attempts'], 1)
        self.assertEqual(done['scan_visit']['number'], 1)

    def test_first_sweep_reaches_other_brands_before_waiting(self):
        second = {**brand(2, 'Two', 'https://two.test'), 'execution_policy': self.policy}
        self.store.enqueue([second])
        config = self.root / 'config.json'
        config.write_text(json.dumps({'execution_policy': self.policy}))
        seen = []
        def run(store, rid, *args, **kwargs):
            seen.append(store.run(rid)['brand_id'])
            if seen[-1] == '1':
                raise ScanPaused('domain_backoff', self.clock() + 300)
            return self.complete(store, rid, *args, **kwargs)
        with patch('authority_batch.PublicHTTP', return_value=object()), \
             patch('authority_batch.run_brand', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            results = worker(self.root, 'W1', config)
        self.assertEqual(seen, ['1', '2'])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(results), 2)
        self.assertTrue((self.root / 'W1.first-pass.json').exists())


if __name__ == '__main__':
    unittest.main()
