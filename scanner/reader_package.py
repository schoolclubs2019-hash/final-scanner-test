#!/usr/bin/env python3
"""Build and import-check the exact reader ZIP without source mtime drift."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from throne_api import scanner_fingerprint
from throne_standard import STANDARD_PATH


FILES = (
    'product_evidence.py', 'price_capture.py', 'brand_capture.py', 'publication_copy.py',
    'row_stream.py', 'scan_contract.py', 'aws_reader_lambda.py', 'confirmed_product.py',
    'text_normalizer.py', 'department_capabilities.py', 'department_readers.py',
    'throne_api.py', 'throne_read.py', 'throne_standard.py', 'textile_policy.py',
    'inci_read.py', 'ingredient_extract.py', 'ingredient_read.py',
    'inci_data/catalog.yml', 'inci_data/hazards.yml',
)


def build(output):
    source = Path(__file__).resolve().parent
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = {name: source / name for name in FILES}
    entries['throne_standard.json'] = STANDARD_PATH
    temporary = output.with_suffix('.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in sorted(entries.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
        # Import from the ZIP in isolation, with no repository modules or Standard
        # path available to hide a missing dependency in the deployed package.
        with tempfile.TemporaryDirectory(prefix='htt-reader-check-') as directory:
            with zipfile.ZipFile(temporary) as archive:
                archive.extractall(directory)
            code = ('import sys,json; sys.path.insert(0,sys.argv[1]); '
                    'from aws_reader_lambda import handler; '
                    'print(handler({"rawPath":"/v1/health",'
                    '"requestContext":{"http":{"method":"GET"}}},None)["body"])')
            health = json.loads(subprocess.check_output(
                [sys.executable, '-I', '-B', '-c', code, directory], cwd=directory, text=True))
        if health.get('reader') != scanner_fingerprint() or health.get('ok') is not True:
            raise ValueError('Packaged reader fingerprint does not match source')
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    content_hash = hashlib.sha256(output.read_bytes()).digest()
    manifest = {'sha256': content_hash.hex(), 'CodeSha256': base64.b64encode(content_hash).decode(),
                'reader_health': health}
    output.with_suffix('.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    print(json.dumps(build(parser.parse_args().output), sort_keys=True))
