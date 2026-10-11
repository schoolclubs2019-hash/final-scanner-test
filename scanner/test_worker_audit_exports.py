import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import production_worker
from authority_batch import export_visit_audit, verify_saved_audit, with_audit_policy, worker
from catalogue_audit import export_audit, normalize_audit_policy, verify_export
from scan_policy import ScanPaused, begin_visit, normalize_policy, pause_visit
from scanner_pipeline import SCANNER_VERSION, pipeline_fingerprint
from scanner_store import Store
from throne_read import READER_VERSION
from throne_standard import VERSION
from test_authority_batch import brand


class WorkerAuditExportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'W1.db')
        self.addCleanup(self.store.close)
        self.row = brand(1, 'One', 'https://one.test')

    def create(self, row=None):
        row = row or self.row
        self.store.enqueue([row])
        return self.store.create(row, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']

    def test_absent_policy_preserves_legacy_and_explicit_config_is_normalized(self):
        self.assertNotIn('sitemap_audit_policy', with_audit_policy(self.row))
        fresh = with_audit_policy(self.row, {'mode': 'cross_check'})
        self.assertEqual(fresh['sitemap_audit_policy'], normalize_audit_policy({'mode': 'cross_check'}))
        rid = self.create()
        with patch('authority_batch.export_audit') as export:
            result = export_visit_audit(self.store, rid, self.root)
        export.assert_not_called()
        self.assertNotIn('audit_export_status', result)

    def test_paused_execution_policy_exports_unique_verified_inventory_snapshots(self):
        rid = self.create({**self.row, 'execution_policy': normalize_policy({})})
        self.store.update(rid, status='DEFERRED', defer_reason='site_pass_budget', worker_attempts=2,
                          scan_visit={'number': 0, 'state': 'PAUSED'})
        first = export_visit_audit(self.store, rid, self.root)
        second = export_visit_audit(self.store, rid, self.root)
        self.assertNotEqual(first['audit_export_path'], second['audit_export_path'])
        for run in (first, second):
            path = self.root / run['audit_export_path']
            self.assertTrue((path / 'manifest.json').is_file())
            self.assertTrue((path / 'products.jsonl').is_file())
            self.assertTrue((path / 'SHA256SUMS').is_file())
            self.assertEqual(run['audit_export_status'], 'PASS')
            self.assertEqual(run['audit_export_catalogue_status'], 'INCOMPLETE')
            self.assertEqual(run['status'], 'DEFERRED')
            self.assertEqual(run['worker_attempts'], 2)
            self.assertFalse(run.get('publish_eligible'))
            proof = verify_saved_audit(self.store, run)
            self.assertEqual(proof['manifest_sha256'], run['audit_export_manifest_sha256'])

    def test_export_uses_saved_byte_limit_and_verifies_before_recording_pass(self):
        rid = self.create(with_audit_policy(self.row, {'max_export_bytes': 1048576}))
        with patch('authority_batch.export_audit', wraps=export_audit) as exporter, \
             patch('authority_batch.verify_export', wraps=verify_export) as verifier:
            result = export_visit_audit(self.store, rid, self.root)
        self.assertEqual(result['audit_export_status'], 'PASS')
        self.assertEqual(exporter.call_args.kwargs['max_export_bytes'], 1048576)
        self.assertEqual(verifier.call_args.kwargs['max_export_bytes'], 1048576)
        self.assertEqual(verifier.call_args.kwargs['expected_run_id'], rid)
        self.assertEqual(verifier.call_args.kwargs['expected_fingerprint'], result['scanner_fingerprint'])

    def test_failure_is_explicit_and_sanitized_without_changing_scan_truth(self):
        rid = self.create(with_audit_policy(self.row, {}))
        self.store.update(rid, status='DEFERRED', defer_reason='domain_backoff')
        with patch('authority_batch.export_audit', side_effect=ValueError('credential-like private detail')):
            result = export_visit_audit(self.store, rid, self.root)
        self.assertEqual(result['audit_export_status'], 'FAIL')
        self.assertEqual(result['audit_export_error'], 'ValueError')
        self.assertNotIn('credential-like', json.dumps(result))
        self.assertEqual(result['status'], 'DEFERRED')
        self.assertEqual(result['defer_reason'], 'domain_backoff')
        self.assertIsNone(result['audit_export_path'])
        self.assertIsNone(result['audit_export_manifest_sha256'])

    def test_verifier_failure_cannot_be_recorded_as_export_pass(self):
        rid = self.create(with_audit_policy(self.row, {}))
        with patch('authority_batch.verify_export', side_effect=ValueError('changed capture')):
            result = export_visit_audit(self.store, rid, self.root)
        self.assertEqual(result['audit_export_status'], 'FAIL')
        self.assertIsNone(result['audit_export_manifest_sha256'])

    def test_attestation_verifies_actual_bytes_and_bound_manifest_hash(self):
        rid = self.create(with_audit_policy(self.row, {}))
        result = export_visit_audit(self.store, rid, self.root)
        self.assertEqual(result['audit_export_status'], 'PASS')
        bad_hash = {**result, 'audit_export_manifest_sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'manifest hash changed'):
            verify_saved_audit(self.store, bad_hash)
        (self.root / result['audit_export_path'] / 'products.jsonl').write_text('tampered\n')
        with self.assertRaises(ValueError):
            verify_saved_audit(self.store, result)

    def test_snapshot_paths_refuse_traversal_absolute_and_symlink(self):
        rid = self.create(with_audit_policy(self.row, {}))
        result = export_visit_audit(self.store, rid, self.root)
        output = self.root / result['audit_export_path']
        alias = self.root / 'alias'
        alias.symlink_to(output, target_is_directory=True)
        for value in ('../outside', str(output), 'alias'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verify_saved_audit(self.store, {**result, 'audit_export_path': value})

    def test_worker_saved_policy_is_not_replaced_by_new_configuration(self):
        row = with_audit_policy(self.row, {'mode': 'captured_only'})
        rid = self.create(row)
        self.store.update(rid, status='SCANNER_COMPLETE', scanner_status='SCANNER_COMPLETE', publish_eligible=False)
        config = self.root / 'config.json'
        config.write_text(json.dumps({'sitemap_audit_policy': {'mode': 'cross_check'}}))
        with contextlib.redirect_stdout(io.StringIO()):
            results = worker(self.root, 'W1', config)
        saved = self.store.run(rid)
        self.assertEqual(saved['sitemap_audit_policy']['mode'], 'captured_only')
        self.assertEqual(results[0]['audit_export_status'], 'PASS')
        self.assertTrue((self.root / results[0]['audit_export_path'] / 'manifest.json').exists())

    def run_canary(self, fail_export=False):
        queue, config = self.root / 'queue.json', self.root / 'config.json'
        queue.write_text(json.dumps([self.row]))
        config.write_text(json.dumps({'execution_policy': {}, 'sitemap_audit_policy': {'mode': 'cross_check'}}))
        def paused(store, row, rid, *args, **kwargs):
            self.assertEqual(row['sitemap_audit_policy'], normalize_audit_policy({'mode': 'cross_check'}))
            rid = store.create(row, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
            begin_visit(store, rid)
            store.update(rid, worker_attempts=2,
                         authority_retry={'url': row['domain'], 'attempts': 1, 'state': 'BACKOFF'})
            return pause_visit(store, rid, ScanPaused('site_pass_budget'))
        argv = ['production_worker.py', 'canary', '--queue', str(queue), '--config', str(config),
                '--state', str(self.root), '--brand-id', '1']
        output = io.StringIO()
        with patch('sys.argv', argv), patch('production_worker.production_preflight'), \
             patch('production_worker.scan_one', side_effect=paused), \
             patch('htt_publish.assert_publishable') as assert_publishable, \
             patch('production_worker.deliver_canary') as delivery, contextlib.redirect_stdout(output):
            if fail_export:
                with patch('authority_batch.export_audit', side_effect=OSError('private text')):
                    with self.assertRaisesRegex(RuntimeError, 'AUDIT EXPORT FAILED'):
                        production_worker.main()
            else:
                self.assertEqual(production_worker.main(), 2)
                self.canary_receipt = json.loads(output.getvalue())
                self.assertEqual(self.canary_receipt['status'], 'INCOMPLETE')
                self.assertEqual(self.canary_receipt['run_status'], 'DEFERRED')
            assert_publishable.assert_not_called()
            delivery.assert_not_called()
        self.assertFalse((self.root / 'canary-proof.json').exists())
        return json.loads((self.root / 'canary-result.json').read_text())

    def test_attestation_cannot_hide_failed_opted_in_export(self):
        rid = self.create({**self.row, 'execution_policy': normalize_policy({})})
        self.store.update(rid, audit_export_status='FAIL')
        with patch('htt_publish.assert_publishable'):
            with self.assertRaisesRegex(ValueError, 'successful saved-evidence audit export'):
                production_worker.attest_canary(self.store, rid, {'execution_policy': normalize_policy({})})

    def test_actual_canary_path_exports_before_reporting_pause(self):
        result = self.run_canary()
        self.assertEqual(result['audit_export_status'], 'PASS')
        self.assertEqual(result['status'], 'DEFERRED')
        self.assertTrue((self.root / result['audit_export_path'] / 'manifest.json').is_file())

    def test_not_yet_due_canary_resume_returns_same_saved_pause_without_new_budget(self):
        first = self.run_canary()
        rid = first['scan_run_id']
        self.assertEqual(self.canary_receipt['scan_run_id'], rid)
        self.assertEqual(self.canary_receipt['defer_reason'], 'site_pass_budget')
        self.assertEqual(self.canary_receipt['scan_next_eligible_at'], first['scan_next_eligible_at'])
        store = Store(self.root / 'canary.db')
        try:
            before = store.run(rid)
        finally:
            store.close()
        argv = ['production_worker.py', 'canary', '--queue', str(self.root / 'queue.json'),
                '--config', str(self.root / 'config.json'), '--state', str(self.root), '--brand-id', '1']
        output = io.StringIO()
        with patch('sys.argv', argv), patch('production_worker.production_preflight'), \
             patch('authority_batch.PublicHTTP') as http, \
             patch('htt_publish.assert_publishable') as assert_publishable, \
             patch('production_worker.deliver_canary') as delivery, contextlib.redirect_stdout(output):
            self.assertEqual(production_worker.main(), 2)
        http.assert_not_called()
        assert_publishable.assert_not_called()
        delivery.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt['scan_run_id'], rid)
        self.assertEqual(receipt['status'], 'INCOMPLETE')
        store = Store(self.root / 'canary.db')
        try:
            after = store.run(rid)
        finally:
            store.close()
        for key in ('scan_visit', 'scan_next_eligible_at', 'worker_attempts', 'authority_retry'):
            self.assertEqual(after.get(key), before.get(key), key)
        self.assertEqual(after['scan_visit']['number'], 0)
        self.assertEqual(after['worker_attempts'], 2)
        self.assertFalse((self.root / 'canary-proof.json').exists())

    def test_actual_canary_path_records_export_failure_before_stopping(self):
        result = self.run_canary(fail_export=True)
        self.assertEqual(result['audit_export_status'], 'FAIL')
        self.assertEqual(result['audit_export_error'], 'OSError')
        self.assertNotIn('private text', json.dumps(result))

    def test_attestation_rejects_configured_policy_not_executed_by_saved_run(self):
        rid = self.create({**with_audit_policy(self.row, {'mode': 'captured_only'}),
                           'execution_policy': normalize_policy({})})
        for config in ({'execution_policy': {'http_timeout_seconds': 45}},
                       {'sitemap_audit_policy': {'mode': 'cross_check'}}):
            with self.subTest(config=config), patch('htt_publish.assert_publishable'):
                with self.assertRaisesRegex(ValueError, 'fresh canary is required'):
                    production_worker.attest_canary(self.store, rid, config)

    def test_saved_opt_in_policies_cannot_be_removed_or_nulled(self):
        execution = normalize_policy({})
        audit = normalize_audit_policy({'mode': 'cross_check'})
        rid = self.create({**self.row, 'execution_policy': execution, 'sitemap_audit_policy': audit})
        configs = ({'sitemap_audit_policy': audit},
                   {'execution_policy': None, 'sitemap_audit_policy': audit},
                   {'execution_policy': execution},
                   {'execution_policy': execution, 'sitemap_audit_policy': None})
        for config in configs:
            with self.subTest(config=config), patch('htt_publish.assert_publishable'):
                with self.assertRaisesRegex(ValueError, 'fresh canary is required'):
                    production_worker.attest_canary(self.store, rid, config)
        self.assertEqual(self.store.run(rid)['execution_policy'], execution)
        self.assertEqual(self.store.run(rid)['sitemap_audit_policy'], audit)

    def test_policy_mismatch_does_not_overwrite_existing_runtime_config(self):
        queue, config = self.root / 'queue.json', self.root / 'config.json'
        queue.write_text(json.dumps([self.row]))
        config.write_text(json.dumps({'execution_policy': {'http_timeout_seconds': 45}}))
        original = '{"preserve":"saved runtime configuration"}'
        (self.root / 'runtime-config.json').write_text(original)
        store = Store(self.root / 'canary.db')
        try:
            row = {**self.row, 'execution_policy': normalize_policy({})}
            store.enqueue([row])
            store.create(row, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)
        finally:
            store.close()
        argv = ['production_worker.py', 'canary', '--queue', str(queue), '--config', str(config),
                '--state', str(self.root), '--brand-id', '1']
        with patch('sys.argv', argv), patch('production_worker.production_preflight') as preflight, \
             patch('production_worker.scan_one') as scan:
            with self.assertRaisesRegex(ValueError, 'fresh canary is required'):
                production_worker.main()
        preflight.assert_not_called()
        scan.assert_not_called()
        self.assertEqual((self.root / 'runtime-config.json').read_text(), original)


if __name__ == '__main__':
    unittest.main()
