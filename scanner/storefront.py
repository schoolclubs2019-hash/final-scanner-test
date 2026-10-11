"""Storefront host families, bounded by the vendored Public Suffix List.

Private suffixes matter: en.github.io and fr.github.io are separate owners.
No DNS/IP similarity or shared CDN is accepted as storefront identity proof.
"""
from functools import lru_cache
from pathlib import Path
import re


@lru_cache(maxsize=1)
def suffix_rules():
    return frozenset(line.strip().encode('idna').decode('ascii').casefold()
                     for line in Path(__file__).with_name('public_suffix_list.dat').read_text().splitlines()
                     if line.strip() and not line.startswith('//'))


def registrable_domain(host):
    labels = host.rstrip('.').encode('idna').decode('ascii').casefold().split('.')
    rules = suffix_rules()
    suffix_size = 1
    for index in range(len(labels)):
        suffix = '.'.join(labels[index:])
        size = len(labels) - index
        if '!' + suffix in rules:
            suffix_size = size - 1
            break
        if suffix in rules:
            suffix_size = max(suffix_size, size)
        if index > 0 and '*.' + suffix in rules:
            suffix_size = max(suffix_size, size + 1)
    if len(labels) <= suffix_size:
        return None
    return '.'.join(labels[-suffix_size - 1:])


def storefront_family(host):
    if not host:
        return ''
    host = host.rstrip('.').encode('idna').decode('ascii').casefold()
    owner = registrable_domain(host)
    if owner is None:
        return host
    labels = host.split('.')
    minimum = len(owner.split('.'))
    while len(labels) > minimum and (labels[0] in {'www', 'shop', 'store'}
                                     or re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?', labels[0])):
        labels.pop(0)
    return '.'.join(labels)
