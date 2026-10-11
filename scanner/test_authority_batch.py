import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from authority_batch import domain_affine_shards, load_queue, prepare, scan_one
from scanner_pipeline import SCANNER_VERSION, pipeline_fingerprint
from scanner_store import Store
from throne_read import READER_VERSION
from throne_standard import VERSION


def brand(brand_id, name, domain, count=1):
    return {
        'brand_id': str(brand_id),
        'htt_id': 'htt-' + str(brand_id),
        'brand': name,
        'slug': name.casefold().replace(' ', '-'),
        'domain': domain,
        'cpt': 'clothes',
        'department': 'clothing',
        'adapter': 'auto',
        'census_unit': 'product',
        'existing_catalogue_count': count,
    }


class AuthorityBatchTest(unittest.TestCase):
    def test_domain_affinity_never_splits_a_hostname(self):
        records = [
            brand(1, 'One', 'https://same.test/a', 100),
            brand(2, 'Two', 'https://www.same.test/b', 100),
            brand(3, 'Three', 'https://other.test', 50),
            brand(4, 'Four', 'https://third.test', 25),
        ]
        shards, _ = domain_affine_shards(records, 2)
        owners = {}
        for index, shard in enumerate(shards):
            for row in shard:
                host = row['domain'].split('/')[2].removeprefix('www.')
                owners.setdefault(host, set()).add(index)
        self.assertTrue(all(len(value) == 1 for value in owners.values()))
        same = [row for shard in shards for row in shard if 'same.test' in row['domain']]
        self.assertEqual(len(same), 2)

    def test_each_shard_runs_smaller_catalogues_first(self):
        records = [
            brand(1, 'Large', 'https://large.test', 1000),
            brand(2, 'Small', 'https://small.test', 2),
            brand(3, 'Medium', 'https://medium.test', 50),
        ]
        shards, _ = domain_affine_shards(records, 1)
        self.assertEqual([row['brand'] for row in shards[0]], ['Small', 'Medium', 'Large'])

    def test_prepare_creates_independent_durable_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / 'queue.json'
            queue.write_text(json.dumps({'queue': [
                brand(1, 'One', 'https://one.test'),
                brand(2, 'Two', 'https://two.test'),
                brand(3, 'Three', 'https://three.test'),
                brand(4, 'Four', 'https://four.test'),
            ]}))
            result = prepare(queue, root / 'state', workers=2)
            self.assertEqual(result['total'], 4)
            counts = []
            for name in ('W1', 'W2'):
                store = Store(root / 'state' / f'{name}.db')
                try:
                    counts.append(len(store.queue()))
                finally:
                    store.close()
            self.assertEqual(sum(counts), 4)
            with self.assertRaisesRegex(ValueError, 'already contains durable work'):
                prepare(queue, root / 'state', workers=2)

    def test_load_queue_rejects_duplicate_brand_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'queue.json'
            path.write_text(json.dumps({'queue': [
                brand(1, 'One', 'https://one.test'),
                brand(1, 'Again', 'https://again.test'),
            ]}))
            with self.assertRaisesRegex(ValueError, 'Duplicate queue brand_id'):
                load_queue(path)

    def test_running_checkpoint_is_resumed_not_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            try:
                row = brand(1, 'One', 'https://one.test')
                store.enqueue([row])
                rid = store.create(row, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
                store.update(rid, status='RUNNING')

                def finish(store_arg, run_id, http, renderer=None, retry=False, manual=None):
                    self.assertTrue(retry)
                    store_arg.update(
                        run_id,
                        status='SCANNER_COMPLETE',
                        scanner_status='SCANNER_COMPLETE',
                        publish_eligible=False,
                        error='fixture',
                    )
                    return store_arg.run(run_id)

                with patch('authority_batch.PublicHTTP', return_value=object()), \
                     patch('authority_batch.run_brand', side_effect=finish):
                    result = scan_one(store, row, rid)
                self.assertEqual(result['scanner_status'], 'SCANNER_COMPLETE')
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
