"""Crash injection: recover a prepared durable scanner manifest without re-enqueueing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production_worker import prepare_manifest, checkpoint_json as durable_checkpoint
from scan_policy import normalize_policy
from test_authority_batch import brand


class ManifestRecoveryTest(unittest.TestCase):
    def test_crash_after_shards_prepared_before_manifest_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            records = [brand(1, 'First', 'https://first.example'),
                       brand(2, 'Second', 'https://second.example')]
            config = {'execution_policy': normalize_policy({
                'workers': 1, 'first_pass_seconds': 60, 'revisit_seconds': 90,
                'first_inline_wait_seconds': 10, 'revisit_inline_wait_seconds': 10})}
            def crash_before_manifest(path, value):
                if Path(path).name == 'launch-manifest.json':
                    raise RuntimeError('simulated crash after complete shard preparation')
                return durable_checkpoint(path, value)
            with patch('production_worker.checkpoint_json', side_effect=crash_before_manifest):
                with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                    prepare_manifest(None, state, records, 1, config, render=False)
            self.assertTrue((state / 'launch-intent.json').is_file())
            self.assertTrue((state / 'W1.db').is_file())
            self.assertFalse((state / 'launch-manifest.json').exists())
            repaired = prepare_manifest(None, state, records, 1, config, render=False)
            self.assertEqual(repaired['request']['workers'], 1)
            self.assertEqual(set(repaired['assignments']), {'W1'})
            self.assertEqual(json.loads((state / 'launch-manifest.json').read_text()), repaired)
            self.assertEqual(prepare_manifest(None, state, records, 1, config, render=False), repaired)
