import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from production_worker import coordinate_bulk
from scanner_store import Store
from scanner_pipeline import SCANNER_VERSION, pipeline_fingerprint
from throne_read import READER_VERSION
from throne_standard import VERSION
from scan_policy import begin_visit, normalize_policy, pause_visit, ScanPaused
from test_authority_batch import brand
from test_execution_policy import Clock


class DeferredCoordinatorTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'config.json'
        self.policy = normalize_policy({'workers': 2, 'first_pass_seconds': 100, 'revisit_seconds': 200,
            'first_inline_wait_seconds': 0, 'revisit_inline_wait_seconds': 0,
            'revisit_delay_seconds': 10, 'max_coordinator_wait_seconds': 30})
        self.config.write_text(json.dumps({'execution_policy': self.policy}))
        (self.root / 'queue-digest').write_text('fixture-queue')
        self.clock, self.calls = Clock(), []
        for index in range(1, 3):
            store = Store(self.root / f'W{index}.db')
            store.enqueue([{**brand(index, f'Brand {index}', f'https://brand{index}.test'),
                            'execution_policy': self.policy}])
            store.close()

    def checkpoint(self, name):
        (self.root / f'{name}.first-pass.json').write_text(json.dumps({
            'scanner_fingerprint': pipeline_fingerprint(), 'execution_policy': self.policy,
            'completed_at': self.clock()}))

    def sweep(self, names, phase, number):
        self.calls.append((tuple(names), phase, number))
        for name in names:
            if phase == 'revisit':
                self.assertTrue(all((self.root / f'W{i}.first-pass.json').exists() for i in (1, 2)))
            store = Store(self.root / f'{name}.db')
            try:
                row, rid = store.queue()[0]
                if rid is None:
                    rid = store.create(row, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
                visit = store.run(rid).get('scan_visit') or {}
                if phase == 'revisit' and visit.get('number', 0) >= number:
                    continue
                budget = begin_visit(store, rid, phase, self.clock, self.clock.sleep)
                if budget is not None:
                    pause_visit(store, rid, ScanPaused(), self.clock)
            finally:
                store.close()
            if phase == 'first':
                self.checkpoint(name)
        return True

    def run_coordinator(self, sweep=None, sleep=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return coordinate_bulk(self.root, self.config, sweep=sweep or self.sweep,
                                   clock=self.clock, sleep=sleep or self.clock.sleep)

    def test_global_barrier_then_three_bounded_revisits_report_incomplete(self):
        report = self.run_coordinator()
        self.assertEqual(self.calls[0], (('W1', 'W2'), 'first', None))
        self.assertEqual([call[2] for call in self.calls[1:]], [1, 2, 3])
        self.assertEqual(report['wait_seconds_reserved'], 30)
        self.assertEqual(report['stop_reason'], 'revisit_ceiling')
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertTrue(all(row['disposition'] == 'held' for row in report['remaining']))
        self.assertTrue(all(row['scan_visit']['number'] == 3 for row in report['remaining']))
        self.assertTrue((self.root / 'deferred-report.json').exists())
        before = list(self.calls)
        again = self.run_coordinator()
        self.assertEqual(self.calls, before)
        self.assertEqual(again['wait_seconds_reserved'], 30)

    def test_completed_first_pass_with_no_remaining_work_is_complete(self):
        def complete(names, phase, number):
            self.assertEqual(phase, 'first')
            self.sweep(names, phase, number)
            for name in names:
                store = Store(self.root / f'{name}.db')
                try:
                    rid = store.queue()[0][1]
                    store.update(rid, fully_complete=True, status='COMPLETE',
                                 scanner_status='SCANNER_COMPLETE', htt_publish_status='PASS')
                finally:
                    store.close()
            return True
        report = self.run_coordinator(sweep=complete)
        self.assertTrue(report['first_pass_complete'])
        self.assertEqual(report['status'], 'COMPLETE')
        self.assertEqual(report['remaining'], [])
        self.assertFalse(self.clock.sleeps)

    def test_failed_first_shard_prevents_every_revisit(self):
        def partial(names, phase, number):
            self.assertEqual(phase, 'first')
            self.sweep(['W1'], phase, number)
            return False
        report = self.run_coordinator(sweep=partial)
        self.assertFalse(report['first_pass_complete'])
        self.assertEqual(report['stop_reason'], 'worker_error')
        self.assertEqual(report['status'], 'ERROR')
        self.assertFalse(self.clock.sleeps)
        self.calls.clear()
        self.run_coordinator()
        self.assertEqual(self.calls[0], (('W2',), 'first', None))

    def test_future_cooldown_beyond_wait_budget_remains_deferred(self):
        self.policy['max_coordinator_wait_seconds'] = 5
        self.config.write_text(json.dumps({'execution_policy': self.policy}))
        report = self.run_coordinator()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report['stop_reason'], 'coordinator_wait_budget')
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertEqual(report['wait_seconds_reserved'], 0)
        self.assertTrue(all(row['next_eligible_at'] == 1010 for row in report['remaining']))
        self.assertFalse(self.clock.sleeps)

    def test_interrupted_reserved_wait_is_not_granted_again(self):
        self.policy['max_coordinator_wait_seconds'] = 10
        self.config.write_text(json.dumps({'execution_policy': self.policy}))
        def interrupt(seconds):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_coordinator(sleep=interrupt)
        saved = json.loads((self.root / 'bulk-schedule.json').read_text())
        self.assertTrue(saved['first_pass_complete'])
        self.assertEqual(saved['wait_seconds_reserved'], 10)
        self.clock.at = 1004
        report = self.run_coordinator()
        self.assertEqual(self.clock.sleeps, [6])
        self.assertEqual(report['wait_seconds_reserved'], 10)
        self.assertEqual(report['stop_reason'], 'coordinator_wait_budget')
        self.assertTrue(all(row['scan_visit']['number'] == 1 for row in report['remaining']))

    def test_changed_policy_cannot_replace_saved_coordinator(self):
        self.run_coordinator()
        changed = {**self.policy, 'max_revisit_passes': 4}
        self.config.write_text(json.dumps({'execution_policy': changed}))
        with self.assertRaisesRegex(ValueError, 'cannot change'):
            self.run_coordinator()

    def test_worker_no_progress_stops_instead_of_spinning(self):
        def no_progress(names, phase, number):
            if phase == 'first':
                return self.sweep(names, phase, number)
            return True
        report = self.run_coordinator(sweep=no_progress)
        self.assertEqual(report['stop_reason'], 'worker_no_progress')
        self.assertEqual(report['status'], 'ERROR')


if __name__ == '__main__':
    unittest.main()


class CoordinatorScaleBenchmark(unittest.TestCase):
    def test_large_multi_shard_deferred_report_and_revisit_rounds(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = normalize_policy({
                'workers': 4, 'max_revisit_passes': 3, 'first_pass_seconds': 100,
                'revisit_seconds': 200, 'first_inline_wait_seconds': 0,
                'revisit_inline_wait_seconds': 0, 'max_coordinator_wait_seconds': 0,
            })
            config = root / 'config.json'
            config.write_text(json.dumps({'execution_policy': policy}))
            (root / 'queue-digest').write_text('synthetic-queue-160')
            for shard in range(1, 5):
                name = f'W{shard}'
                store = Store(root / f'{name}.db')
                try:
                    records = [
                        {**brand(shard * 1000 + i, f'Brand {shard}-{i}',
                                 f'https://brand-{shard}-{i}.test'),
                         'execution_policy': policy}
                        for i in range(40)
                    ]
                    store.enqueue(records)
                finally:
                    store.close()
                (root / f'{name}.first-pass.json').write_text(json.dumps({
                    'scanner_fingerprint': pipeline_fingerprint(),
                    'execution_policy': policy, 'completed_at': 123}))
            start = time.perf_counter()
            report = coordinate_bulk(root, config, sweep=lambda *_: self.fail('No network worker expected'),
                                     clock=lambda: 123, sleep=lambda _: self.fail('No waiting expected'))
            elapsed = time.perf_counter() - start
            self.assertEqual(len(report['remaining']), 160)
            self.assertEqual(report['revisit_round'], 4)
            self.assertEqual(report['status'], 'INCOMPLETE')
            self.assertLess(elapsed, 15, 'Deferred revisit scheduler throughput regressed')
