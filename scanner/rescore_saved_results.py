#!/usr/bin/env python3
"""Recalculate legacy saved catalogue JSON locally; never fetch or publish.

Outputs are provisional comparisons against stored extractions. Missing original
HTML, product identity and old component estimates remain explicit limitations.
"""
import argparse
from collections import Counter
import csv
import fcntl
import gzip
import hashlib
import io
import json
from pathlib import Path
import socket

from aws_reader_lambda import clean_items
from confirmed_product import confirm_product, filter_confirmed_products
from scan_contract import scan, deduplicate
from rescore_catalogues import atomic_json
from throne_api import scanner_fingerprint
from throne_standard import VERSION


def saved_item(row):
    item = {key: row[key] for key in (
        'sku', 'product_id', 'variant_id', 'handle', 'url', 'composition', 'material',
        'body_html', 'description', 'page_text', 'ingredients', 'inci', 'category',
        'product_type', 'price', 'price_min', 'price_max', 'currency', 'market', 'locale',
        'product_json', 'commerce_json', 'availability', 'source_endpoint',
    ) if key in row}
    item['title'] = row.get('title') or row.get('product') or row.get('name') or ''
    item['composition'] = row.get('composition') or row.get('fabric') or ''
    item['fetchedAt'] = row.get('fetchedAt') or row.get('scanned_at') or ''
    variants = row.get('variant_prices')
    if isinstance(variants, list) and variants:
        # Preserve a real saved variant identifier as product evidence. Do not
        # manufacture SKU IDs from array positions or expand product rows by size.
        variant = next((v for v in variants if isinstance(v, dict) and v.get('variant_id')), None)
        if variant and not (item.get('product_id') or item.get('variant_id') or item.get('sku')):
            item['variant_id'] = variant['variant_id']
    how = str(row.get('how') or '')
    if how.startswith('unread') or how in ('evidence-conflict', 'missing-source-url', 'invalid-classification'):
        item['fetch_status'] = 'unread'
    if how == 'components-equal-split' and not any(row.get(k) for k in ('body_html', 'description', 'page_text')):
        # A flattened average cannot recover the original component weights.
        item['fetch_status'] = 'unread'
        item['_legacy_component_estimate'] = True
    return item


def safe_cell(value):
    text = str(value if value is not None else '')
    return "'" + text if text.startswith(('=', '+', '-', '@')) else text


def recalculate(directory, output):
    directory, output = Path(directory), Path(output)
    files = sorted(directory.glob('*.json'))
    if not files:
        raise ValueError('No saved catalogue JSON files were found.')
    output.mkdir(parents=True, exist_ok=True)
    summary = {'mode': 'offline_saved_evidence', 'standard_version': VERSION,
               'scanner_fingerprint': scanner_fingerprint(), 'external_requests': 0,
               'live_publication_performed': False, 'brands': 0, 'input_rows': 0,
               'saved_compositions': 0, 'states': {}, 'failed_files': [], 'catalogues': []}
    states = Counter()
    sku_path = output / 'offline-sku-comparison.csv.gz'
    brand_path = output / 'offline-brand-comparison.csv'
    sku_tmp, brand_tmp = sku_path.with_suffix('.pending'), brand_path.with_suffix('.pending')
    sku_fields = ['brand', 'saved_file', 'row', 'product', 'url', 'saved_composition', 'previous_band',
                  'recalculated_band', 'provisional_sku_score', 'department', 'outcome', 'reason', 'source_checked_at']
    brand_fields = ['brand', 'saved_file', 'saved_rows', 'confirmed_unique_products', 'rejected_rows',
                    'duplicates', 'previous_score', 'provisional_department_scores', 'publish_eligible']
    with (output / '.saved-replay.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another saved-catalogue replay holds this output directory.') from None
        with gzip.open(sku_tmp, 'wt', encoding='utf-8', newline='') as sku_file, brand_tmp.open('w', encoding='utf-8', newline='') as brand_file:
            sku_writer = csv.DictWriter(sku_file, fieldnames=sku_fields); sku_writer.writeheader()
            brand_writer = csv.DictWriter(brand_file, fieldnames=brand_fields); brand_writer.writeheader()
            for path in files:
                try:
                    raw = path.read_bytes()
                    saved = json.loads(raw)
                    original = saved.get('rows') if isinstance(saved, dict) else None
                    if not isinstance(original, list):
                        raise ValueError('No saved product rows.')
                    ready, rejected, records = [], {}, []
                    for index, row in enumerate(original):
                        if not isinstance(row, dict):
                            rejected[index] = 'invalid_saved_row'; records.append({}); continue
                        records.append(row)
                        try:
                            item = saved_item(row)
                            cleaned = clean_items([item])[0]
                            cleaned['_saved_index'] = index
                            if item.get('_legacy_component_estimate'):
                                cleaned['_legacy_component_estimate'] = True
                            accepted, reason = confirm_product(cleaned)
                            if not accepted:
                                rejected[index] = reason
                            ready.append(cleaned)
                        except (ValueError, TypeError) as error:
                            rejected[index] = 'invalid_saved_evidence: ' + str(error)
                    confirmed, _ = filter_confirmed_products(ready)
                    unique, _ = deduplicate(confirmed)
                    # Calculate the supplied evidence, but deliberately withhold
                    # catalogue attestation. Export only labelled provisional scores.
                    result = scan(ready, {'department': saved.get('department') or 'auto',
                                          'catalogue': {'complete': False}}, trusted=True)
                    if len(unique) != len(result['read']):
                        raise ValueError('Scanned row count does not match confirmed saved identities.')
                    assessed = {item['_saved_index']: (item, row) for item, row in zip(unique, result['read'])}
                    sku_buffer = io.StringIO(newline='')
                    buffered_writer = csv.DictWriter(sku_buffer, fieldnames=sku_fields)
                    file_states, file_compositions = Counter(), 0
                    for index, source in enumerate(records):
                        composition = source.get('composition') or source.get('fabric') or ''
                        file_compositions += bool(composition)
                        item, row = assessed.get(index, ({}, {}))
                        if index in rejected:
                            state, reason = 'not_confirmed', rejected[index]
                        elif index not in assessed:
                            state, reason = 'duplicate', 'duplicate_identity'
                        elif item.get('_legacy_component_estimate'):
                            state, reason = 'unresolved', 'original_component_evidence_missing'
                        else:
                            state = 'recalculated' if row.get('score') is not None else 'unresolved'
                            reason = row.get('evidence_status') or row.get('how') or 'unknown'
                        file_states[state] += 1
                        values = {'brand': saved.get('brand') or path.stem, 'saved_file': path.name, 'row': index + 1,
                                  'product': source.get('product') or source.get('title') or '', 'url': source.get('url') or '',
                                  'saved_composition': composition, 'previous_band': source.get('band') or '',
                                  'recalculated_band': row.get('band'), 'provisional_sku_score': row.get('score'),
                                  'department': row.get('department'), 'outcome': state, 'reason': reason,
                                  'source_checked_at': source.get('scanned_at') or saved.get('scanned_at') or ''}
                        buffered_writer.writerow({key: safe_cell(value) for key, value in values.items()})
                    scores = [{'department': r['department'], 'category': r['category'],
                               'provisional_score': r.get('score') if r.get('score') is not None else r.get('observedScore'),
                               'assessed': r.get('assessed'), 'status': r.get('status')} for r in result['department_reports']]
                    catalogue = {'file': path.name, 'input_sha256': hashlib.sha256(raw).hexdigest(),
                                 'saved_rows': len(original), 'department_results': scores}
                    values = {'brand': saved.get('brand') or path.stem, 'saved_file': path.name, 'saved_rows': len(original),
                              'confirmed_unique_products': len(result['read']), 'rejected_rows': len(rejected),
                              'duplicates': result['duplicate_count'], 'previous_score': saved.get('score'),
                              'provisional_department_scores': json.dumps(scores, separators=(',', ':')), 'publish_eligible': False}
                    brand_buffer = io.StringIO(newline='')
                    csv.DictWriter(brand_buffer, fieldnames=brand_fields).writerow(
                        {key: safe_cell(value) for key, value in values.items()})
                except (ValueError, TypeError, OSError) as error:
                    summary['failed_files'].append({'file': path.name, 'error': str(error)})
                    continue
                # Commit both fully serialized records. Output I/O failures abort
                # the run, leaving pending files instead of a partial final report.
                brand_file.write(brand_buffer.getvalue())
                sku_file.write(sku_buffer.getvalue())
                summary['brands'] += 1
                summary['input_rows'] += len(original)
                summary['saved_compositions'] += file_compositions
                summary['catalogues'].append(catalogue)
                states.update(file_states)
                print(json.dumps({'file': path.name, 'rows': len(original), 'processed_brands': summary['brands']}), flush=True)
        sku_tmp.replace(sku_path); brand_tmp.replace(brand_path)
        summary['states'] = dict(states)
        atomic_json(output / 'offline-rescore-summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    # Enforce the offline promise even if a future dependency adds a network call.
    def deny_network(*_args, **_kwargs):
        raise RuntimeError('Network access is prohibited during saved-evidence replay.')
    socket.socket.connect = deny_network
    socket.create_connection = deny_network
    result = recalculate(args.directory, args.output)
    print(json.dumps({k: v for k, v in result.items() if k != 'catalogues'}))
    return 1 if result['failed_files'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
