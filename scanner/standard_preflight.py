#!/usr/bin/env python3
"""Refresh HTT's existing Standard page, then prove its active WordPress version.

The verified HTT renderer updates htt_throne_standard_version when serving
/the-throne-standard/?format=json. Reader health alone does not update that
option. All network requests here are credential-free, bounded public GETs.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode, urlsplit
import uuid

from htt_adapters import base_url
from htt_publish import DEFAULT_API, DEFAULT_READER, production_config
from publication_parity import ContractError, StagingTransport
from throne_standard import STANDARD, STANDARD_PATH, VERSION


class StandardReturnedHTML(ContractError):
    """A public Standard endpoint served a page instead of its JSON contract."""


class StandardTransport(StagingTransport):
    """Credential-free Standard reads with a distinct, bounded HTML failure."""
    def request(self, url):
        if urlsplit(url).scheme != 'https':
            raise ContractError('Standard synchronization requires HTTPS')
        # Reuse the allowlist, URL guard, no-redirect and 8 MB response limit.
        # This deliberately exposes neither a write body nor authentication.
        raw = super().request(url, response_format='html')
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            if isinstance(raw, str) and re.match(
                    r'(?is)(?:<!--.*?-->\s*)*(?:<!doctype\s+html\b|<(?:html|head|body)\b)',
                    raw.lstrip('\ufeff \t\r\n')):
                raise StandardReturnedHTML('Standard endpoint returned HTML') from None
            raise ContractError('Standard endpoint returned invalid JSON') from None


def same_json(actual, expected):
    """Compare the complete JSON value, allowing PHP's 100.0 -> 100 encoding."""
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(same_json(actual[key], value) for key, value in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(same_json(a, b) for a, b in zip(actual, expected)))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(expected, (int, float)):
        return (isinstance(actual, (int, float))
                and (not isinstance(actual, float) or math.isfinite(actual))
                and (not isinstance(expected, float) or math.isfinite(expected))
                and actual == expected)
    return type(actual) is type(expected) and actual == expected


def verify_source_commit(commit):
    """The caller has validated CI; bind the helper to that exact source tree."""
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('A full validated release commit is required')
    root = Path(__file__).resolve().parents[2]
    marker = root / '.deployed-commit'
    if marker.is_file():
        if marker.read_text().strip() != commit:
            raise ValueError('Standard refresh commit differs from the verified Azure release')
        return
    try:
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                       text=True, stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'],
                                        text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        raise ValueError('Standard refresh requires a verified release or committed checkout') from None
    if head != commit or dirty:
        raise ValueError('Standard refresh requires the exact clean validated checkout')


def check(commit, config=None, attempts=6, delay=5, transport=None):
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not 1 <= attempts <= 24 or not 0 <= delay <= 30:
        raise ValueError('Require a full commit SHA, 1-24 attempts, and 0-30 seconds between attempts')
    config = config or {}
    api = base_url(os.environ.get('HTT_API_URL') or config.get('bases', {}).get('htt_api') or DEFAULT_API)
    reader = base_url(os.environ.get('HTT_READER_URL') or config.get('bases', {}).get('reader') or DEFAULT_READER)
    if any(urlsplit(url).scheme != 'https' for url in (api, reader)):
        raise ContractError('Standard synchronization requires HTTPS')
    transport = transport or StandardTransport(production_config(config, api, reader))
    parsed = urlsplit(api)
    site = parsed.scheme + '://' + parsed.netloc
    result = {'status': 'FAIL', 'commit': commit, 'expected_version': VERSION,
              'standard_sha256': hashlib.sha256(STANDARD_PATH.read_bytes()).hexdigest(),
              'reader_standard': 'NOT_CHECKED', 'htt_standard': 'NOT_CHECKED',
              'htt_current_standard': 'NOT_CHECKED', 'attempts': 0}
    for attempt in range(attempts):
        result.update(attempts=attempt + 1, reader_standard='NOT_CHECKED',
                      htt_standard='NOT_CHECKED', htt_current_standard='NOT_CHECKED')
        try:
            upstream = transport.request(reader + '/v1/standard')
        except StandardReturnedHTML:
            result.update(reader_standard='RETURNED_HTML', reason='READER_STANDARD_RETURNED_HTML')
            return result
        except ContractError:
            result.update(reader_standard='UNAVAILABLE', reason='READER_STANDARD_UNAVAILABLE')
        else:
            if not same_json(upstream, STANDARD):
                # Never refresh HTT against another deployment's rules.
                result.update(reader_standard='MISMATCH', reason='READER_STANDARD_MISMATCH')
                return result
            result['reader_standard'] = 'PASS'
            # A fresh nonce avoids reusing a previous cached renderer response
            # for this same release. StandardTransport also sends no-cache/no-store.
            query = urlencode({'scanner_release': commit, 'scanner_check': uuid.uuid4().hex})
            try:
                public = transport.request(site + '/the-throne-standard/?format=json&' + query)
            except StandardReturnedHTML:
                result.update(htt_standard='RETURNED_HTML', reason='HTT_STANDARD_RETURNED_HTML')
                return result
            except ContractError:
                result.update(htt_standard='UNAVAILABLE', reason='HTT_STANDARD_UNAVAILABLE')
            else:
                if not same_json(public, STANDARD):
                    result.update(htt_standard='MISMATCH', reason='HTT_STANDARD_MISMATCH')
                    return result
                result['htt_standard'] = 'PASS'
                try:
                    listing = transport.request(api + '/brands/branwyn?' + query)
                except ContractError:
                    result.update(htt_current_standard='UNAVAILABLE', reason='HTT_CURRENT_STANDARD_UNAVAILABLE')
                else:
                    current = listing.get('currentThroneStandardVersion') if isinstance(listing, dict) else None
                    if current == VERSION:
                        result.update(status='PASS', htt_current_standard='PASS', reason=None)
                        return result
                    result.update(htt_current_standard='MISMATCH', reason='HTT_CURRENT_STANDARD_MISMATCH')
        if attempt + 1 < attempts:
            time.sleep(delay)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--attempts', type=int, default=6)
    parser.add_argument('--delay', type=float, default=5)
    args = parser.parse_args(argv)
    verify_source_commit(args.commit)
    config = json.loads(args.config.read_text()) if args.config else {}
    result = check(args.commit, config, args.attempts, args.delay)
    encoded = json.dumps(result, sort_keys=True, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + '.tmp')
        temporary.write_text(encoded)
        temporary.replace(args.output)
    print(encoded, end='', flush=True)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
