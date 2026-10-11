#!/usr/bin/env python3
"""Resume local evidence replay without fetching websites or publishing reports.

Input: JSONL records {"brand_id": "stable-id", "payload": {"items": [...], ...}}.
Each result is atomically checkpointed. An unchanged input and scanner fingerprint
reuse the result; changed evidence or rules trigger a new read. Memory is bounded
by one catalogue (the same request limits as the Lambda adapter).
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from aws_reader_lambda import clean_items, MAX_AGGREGATE_ITEMS, MAX_BODY_BYTES
from scan_contract import scan
from throne_api import scanner_fingerprint


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def replay(input_path, output_path, trusted=False):
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    stats = {'read': 0, 'reused': 0, 'failed': 0}
    with (output_path / '.replay.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another replay holds the output-directory lock; no duplicate replay started.") from None
        with Path(input_path).open('rb') as source:
            line_number = 0
            while True:
                raw = source.readline(MAX_BODY_BYTES + 3)
                if not raw:
                    break
                line_number += 1
                if len(raw.rstrip(b'\r\n')) > MAX_BODY_BYTES:
                    stats['failed'] += 1
                    atomic_json(output_path / f'error-line-{line_number}.json', {
                        'line': line_number, 'error': 'Catalogue exceeds the reader request limit.'})
                    # Drain only this physical line, in bounded chunks, then resume.
                    while raw and not raw.endswith(b'\n'):
                        raw = source.readline(MAX_BODY_BYTES + 3)
                    continue
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict) or not isinstance(record.get('brand_id'), str) or not record['brand_id']:
                        raise ValueError('Each record requires a stable brand_id.')
                    payload = record.get('payload')
                    if not isinstance(payload, dict):
                        raise ValueError('Each record requires a payload object.')
                    encoded = json.dumps(payload, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()
                    key = hashlib.sha256(scanner_fingerprint().encode() + str(trusted).encode() + record['brand_id'].encode() + b'\0' + encoded).hexdigest()
                    destination = output_path / (key + '.json')
                    if destination.exists():
                        try:
                            cached = json.loads(destination.read_text())
                        except (ValueError, OSError):
                            cached = {}
                        if isinstance(cached, dict) and cached.get('cache_key') == key and isinstance(cached.get('result'), dict):
                            stats['reused'] += 1
                            continue
                    result = scan(clean_items(payload.get('items'), MAX_AGGREGATE_ITEMS), payload, trusted)
                    atomic_json(destination, {'brand_id': record['brand_id'], 'cache_key': key, 'result': result})
                    stats['read'] += 1
                except (ValueError, TypeError) as error:
                    stats['failed'] += 1
                    atomic_json(output_path / f'error-line-{line_number}.json', {'line': line_number, 'error': str(error)})
        atomic_json(output_path / 'replay-summary.json', stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--server-verified', action='store_true', help='Input was verified by the trusted server crawler; never use for browser captures.')
    args = parser.parse_args()
    result = replay(args.input, args.output, args.server_verified)
    print(json.dumps(result))
    return 1 if result['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
