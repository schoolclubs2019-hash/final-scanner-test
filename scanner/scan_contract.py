"""Per-SKU orchestration and machine-readable publication preflight."""
from collections import Counter
import json
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from confirmed_product import filter_confirmed_products
from department_readers import route_product, read_department
from department_capabilities import capability
from brand_capture import capture_brand
from price_capture import capture_price
from publication_copy import publication_copy
from throne_api import read_one, brand_figure, brand_groups, scanner_fingerprint
from throne_read import product_scope
import textile_policy as TP
from row_stream import RowStream


# Active product CPTs from the supplied September 2026 CPT map. Place and
# retired data CPTs cannot receive product scan reports.
REPORT_CPTS = {
    'clothes': 'clothing', 'bedding': 'bedding', 'beauty': 'beauty', 'body_bath': 'beauty',
    'food': 'food', 'cocoa': 'food', 'veg-boxes': 'food', 'restaurant': 'food',
    'electronics': 'electronics', 'phone': 'electronics', 'tech': 'electronics',
    **{name: name for name in ('supplements', 'pet', 'jewellery', 'candles', 'homeware',
       'household', 'vehicle', 'airlines', 'femcare', 'toys', 'case', 'nappies', 'product')},
}


def report_destination(item, department, category, requested_cpt=None):
    explicit = item.get('cpt') or requested_cpt
    if not explicit:
        # Preserve a known source vertical before falling back to the broad reader.
        explicit = next((item.get(k) for k in ('category', 'product_type')
                         if item.get(k) in REPORT_CPTS), None)
    cpt = explicit or ('clothes' if department == 'clothing' else department)
    valid = REPORT_CPTS.get(cpt) == department and (not requested_cpt or requested_cpt == cpt)
    return {'cpt': cpt if valid else None, 'category': category,
            'status': 'resolved' if valid else 'report_destination_unresolved'}


def canonical_url(value):
    parsed = urlsplit(value or '')
    # Preserve variant/market identity; discard only known tracking parameters.
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith('utm_') and k not in {'gclid', 'fbclid'}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), urlencode(sorted(query)), ''))


def deduplicate(items):
    unique, identities, reasons = [], {}, Counter()
    for item in items:
        host = urlsplit(item.get('url') or item.get('source_endpoint') or '').netloc.lower()
        product = item.get('platform_product_id') or item.get('product_id')
        variant = item.get('platform_variant_id') or item.get('variant_id') or item.get('sku')
        identity = (host, item.get('market', ''), item.get('locale', ''), str(product or ''), str(variant or ''))
        if not product and not variant:
            identity = (host, canonical_url(item.get('url')))
            if not item.get('url'):
                identity = ('anonymous', len(unique))
        if identity in identities:
            existing = unique[identities[identity]]
            comparable = ('composition', 'material', 'inci', 'ingredients', 'body_html', 'description', 'page_text', 'category', 'product_type')
            if any(existing.get(key) != item.get(key) for key in comparable):
                existing['_identity_conflict'] = True
                reasons['conflicting_duplicate_identity'] += 1
            else:
                reasons['duplicate_identity'] += 1
            continue
        identities[identity] = len(unique)
        unique.append(dict(item))
    return unique, dict(reasons)


def issue_row(item, department, reason):
    return {'sku': item.get('sku', ''), 'title': item.get('title', ''), 'url': item.get('url', ''),
            'department': department, 'band': 'unclear', 'how': reason, 'composition': None,
            'score': None, 'passes': None, 'scoreable': False,
            'assessment_status': 'unresolved', 'evidence_status': reason,
            'evidence_state': 'technical_failure', 'score_reason': reason.replace('_', ' ').replace('-', ' ')}


def read_products(items, payload, trusted):
    requested_cpt = payload.get('report_cpt')
    if requested_cpt is not None and not isinstance(requested_cpt, str):
        raise ValueError('report_cpt must be a string.')
    confirmed, rejected = filter_confirmed_products(items)
    unique, duplicates = deduplicate(confirmed)
    rows, partitions = [], {}
    requested_department = payload.get('department')
    if str(requested_department or '').strip().lower() in {'', 'auto'} and requested_cpt in REPORT_CPTS:
        requested_department = REPORT_CPTS[requested_cpt]
    for item in unique:
        department, routing = route_product(item, requested_department)
        failure = str(item.get('fetch_status') or '').lower()
        # Product scope is independent of material evidence. Apply it to
        # textile-routed products and to otherwise-unroutable catalogue
        # accessories/non-merchandise so they remain accounted without becoming
        # textile report rows or technical failures.
        scope, _ = product_scope(
            item.get('title') or '',
            item.get('handle') or '',
            item.get('product_type') or '',
            item.get('category') or '',
        )
        if department not in {'clothing', 'bedding', 'unknown'}:
            scope = 'textile'
        if item.get('_identity_conflict'):
            row = issue_row(item, department, 'evidence-conflict')
        elif scope in {'accessory', 'nontextile'}:
            # Scope is an identity decision and must happen before evidence
            # completeness/fetch handling. Out-of-scope catalogue products stay
            # accounted but can never become textile technical failures.
            row = read_one(item)
            failure = ''
        elif failure and failure not in {'ok', 'success', 'read', 'fetched'}:
            row = issue_row(item, department, 'unread-' + failure)
        elif not (item.get('url') or item.get('source_endpoint')):
            row = issue_row(item, department, 'missing-source-url')
        elif department == 'unknown':
            row = issue_row(item, department, routing)
        elif department in {'clothing', 'bedding'}:
            row = read_one(item)
        else:
            row = read_department([item], department, trusted=trusted)['read'][0]
        from product_evidence import component_evidence
        row['evidence_sources'] = item.get('evidence_sources', [])
        row['raw_original'] = item.get('raw_original', {})
        row['normalized'] = item.get('normalized', {})
        row['components'] = component_evidence(item)
        row['terminal_state'] = terminal_state(row, failure)
        row.update(capture_price(item, payload.get('default_currency', '')))
        row.update({'department': department, 'routing_basis': routing, 'authoritative': trusted,
                    'product_id': item.get('product_id') or item.get('platform_product_id', ''), 'variant_id': item.get('variant_id') or item.get('platform_variant_id', ''),
                    'gtin': item.get('gtin', ''), 'market': item.get('market', ''), 'locale': item.get('locale', '')})
        row['scanner'] = 'Universal Throne scanner · ' + ('textile' if department in {'clothing', 'bedding'} else department) + ' rules'
        if not trusted:
            row.update({'score': None, 'passes': None, 'swimwear_score': None, 'scoreable': False})
            row['evidence_state'] = 'client_unverified'
        rows.append(row)
        group = department
        row['report_destination'] = report_destination(item, department, group, requested_cpt)
        partitions.setdefault((row['report_destination']['cpt'], group), []).append(row)

    stats = {'discovery_candidates_count': len(items), 'discovery_rejected_count': sum(rejected.values()),
             'discovery_rejected_reasons': rejected, 'duplicate_count': sum(duplicates.values()),
             'duplicate_reasons': duplicates}
    return rows, stats


def aggregate_rows(rows, payload, trusted, stats=None, compact=False):
    """The same canonical aggregate for HTTP requests and persisted worker rows.

    Internal only: never accept untrusted client-supplied scored rows over HTTP.
    """
    stats = stats or {}
    rejected = stats.get('discovery_rejected_reasons', {})
    duplicates = stats.get('duplicate_reasons', {})
    excluded_states = {'excluded_nontextile', 'excluded_accessory'}
    def reportable(row):
        return row.get('report_included') is not False and row.get('terminal_state') not in excluded_states
    report_rows = rows.where(reportable) if isinstance(rows, RowStream) else [row for row in rows if reportable(row)]
    partitions = {}
    for row in report_rows:
        destination = row['report_destination']
        key = (destination['cpt'], destination['category'])
        if isinstance(report_rows, RowStream):
            if key not in partitions:
                partitions[key] = report_rows.where(lambda r, k=key: (r['report_destination']['cpt'], r['report_destination']['category']) == k)
        else:
            partitions.setdefault(key, []).append(row)
    reports = []
    for (target_cpt, group), group_rows in partitions.items():
        department = group_rows[0]['department']
        cap = capability(department)
        if department in {'clothing', 'bedding'}:
            figure = brand_figure(group_rows)
        elif department == 'unknown':
            figure = {'score': None, 'status': 'category_unresolved', 'assessed': 0}
        else:
            from department_readers import _brand
            figure = _brand(group_rows, department, trusted)
        figure.update({'department': department, 'category': group, 'catalogue_total': len(group_rows),
                       'eligible_total': figure.get('eligibleTextile', len(group_rows)), 'capability_state': cap['state'],
                       'publishing_enabled': cap['publishing'],
                       'scanner': ('Universal Throne scanner · ' + ('hotel' if department == 'hotels' else department) +
                                   (' evidence capture' if department == 'homeware' else ' rules'))})
        if not trusted:
            figure.update({'score': None, 'status': 'client_unverified', 'statusLabel': 'Evidence pending server verification'})
        figure['report_destination'] = dict(group_rows[0]['report_destination'])
        reports.append(figure)
    if len(reports) == 1:
        brand = dict(reports[0])
    else:
        brand = {'department': 'mixed' if len(report_rows) else 'unknown', 'score': None,
                 'status': 'separate_department_results' if len(report_rows) else 'no_confirmed_products',
                 'catalogue_total': len(report_rows), 'eligible_total': len(report_rows)}
    brand.update(stats)
    # Keep out-of-scope products visible for catalogue reconciliation without
    # putting them back into the textile report or its scoring denominator.
    scope_counts = Counter(row.get('terminal_state') for row in rows)
    excluded_accessory = scope_counts['excluded_accessory']
    excluded_nontextile = scope_counts['excluded_nontextile']
    out_of_scope = excluded_accessory + excluded_nontextile
    brand.update({
        'catalogue_total': len(rows),
        'catalogue_products_identified': len(rows),
        'eligible_total': len(report_rows),
        'excluded_accessory': excluded_accessory,
        'excluded_nontextile': excluded_nontextile,
        'out_of_scope_count': out_of_scope,
    })
    if isinstance(brand.get('counts'), dict):
        brand['counts'] = dict(brand['counts'])
        brand['counts'].update({
            'excluded': out_of_scope,
            'excluded_accessory': excluded_accessory,
            'excluded_nontextile': excluded_nontextile,
        })
    notes = []
    note_counts = Counter()
    for row in report_rows:
        reason = (row.get('evidence_status') or row.get('how') or 'unknown')
        if row.get('composition_over_100'):
            reason = 'composition_over_100'
        elif row.get('composition_partial'):
            reason = 'partial_composition'
        if row.get('score') is None or row.get('composition_partial') or row.get('how') == 'components-equal-split':
            note_counts[reason] += 1
            if not compact:
                notes.append({'sku': row.get('sku'), 'url': row.get('url'), 'department': row['department'],
                          'code': reason, 'detail': row.get('score_reason') or reason.replace('_', ' ')})
    for reason, count in rejected.items():
        note_counts[reason] += 1
        if not compact:
            notes.append({'code': reason, 'count': count, 'detail': 'Non-product discovery candidates excluded.'})

    blockers = []
    if any(not r.get('terminal_state') for r in rows):
        blockers.append('unaccounted_products')
    manifest = payload.get('catalogue') or {}
    if not isinstance(manifest, dict):
        raise ValueError('catalogue must be an object.')
    expected = manifest.get('confirmed_total')
    if not trusted:
        blockers.append('server_verification_required')
    if manifest.get('complete') is not True or type(expected) is not int or expected != len(rows):
        blockers.append('catalogue_completeness_unverified')
    if any(r['report_destination']['status'] != 'resolved' for r in report_rows):
        blockers.append('report_destination_unresolved')
    if not len(report_rows):
        blockers.append('no_confirmed_products')
    if any(r.get('status') != 'verified' or r.get('score') is None for r in reports):
        blockers.append('unresolved_or_unvalidated_department')
    if any(r.get('evidence_state') == 'technical_failure' for r in report_rows):
        blockers.append('technical_or_evidence_errors')
    if any(isinstance(r.get('independent_review'),dict) and r['independent_review'].get('pass') is False for r in report_rows):
        blockers.append('independent_evidence_review_failed')
    if any(r.get('how') == 'components-equal-split' for r in report_rows):
        blockers.append('component_weights_estimated')
    if any(r.get('composition_partial') for r in report_rows):
        blockers.append('partial_composition')
    if duplicates.get('conflicting_duplicate_identity'):
        blockers.append('conflicting_duplicate_identity')
    for field in ('market', 'locale'):
        if len({r[field] for r in rows if r.get(field)}) > 1:
            blockers.append('mixed_' + field)
    if len({urlsplit(r.get('url') or '').hostname for r in rows if r.get('url')}) > 1:
        blockers.append('mixed_source_hosts')
    if blockers and trusted:
        for figure in [brand, *reports]:
            if figure.get('status') == 'verified':
                figure['observedScore'] = figure.get('score')
                figure['score'] = None
                figure['status'] = 'catalogue_unverified' if 'catalogue_completeness_unverified' in blockers else 'publication_blocked'
                figure['scoreLabel'] = None
                figure['statusLabel'] = 'Catalogue completeness not verified' if 'catalogue_completeness_unverified' in blockers else 'Publication checks not met'
    brand['publish_eligible'] = not blockers
    brand['publication_blockers'] = blockers
    for report in reports:
        report['publish_eligible'] = not blockers
        report['publication_copy'] = publication_copy(payload.get('brandName'), report)
    excluded_products = [] if compact else [
        {'sku': row.get('sku'), 'title': row.get('title'), 'url': row.get('url'),
         'product_scope': row.get('product_scope'), 'scope_reason': row.get('scope_reason'),
         'terminal_state': row.get('terminal_state')}
        for row in rows if row.get('terminal_state') in excluded_states
    ]
    return {'read': [] if compact else report_rows, 'read_count': len(report_rows),
            'report_read_count': len(report_rows), 'catalogue_read_count': len(rows),
            'excluded_products': excluded_products, 'brand': brand, 'department_reports': reports,
            'groups': brand_groups(report_rows) if not compact and not blockers and len(reports) == 1 and brand.get('department') == 'clothing' else [],
            'report_notes': notes, 'note_counts': dict(note_counts), 'report_notes_count': sum(note_counts.values()),
            'brand_capture': capture_brand(payload.get('brand_pages')),
            'scanner_fingerprint': scanner_fingerprint(), 'throne_standard_version': TP.POLICY_VERSION, **stats}


def terminal_state(row, failure=''):
    how = row.get('how', '')
    if failure and failure not in {'ok', 'success', 'read', 'fetched'}:
        return failure if failure in {'blocked', 'timeout', 'reader_error', 'http_404', 'product_removed'} else 'technical_failure'
    # Scope exclusions outrank department routing. An accessory/non-textile can
    # legitimately have no report department; that must never turn it back into
    # a category-unresolved textile row.
    state = row.get('evidence_state') or TP.row_state(row)
    if state in {'excluded_nontextile', 'excluded_accessory'}:
        return state
    if how == 'evidence-conflict':
        return 'evidence_conflict'
    if row.get('department') == 'unknown' or how in {'ambiguous_product_category', 'category_not_identified'}:
        return 'category_unresolved'
    if row.get('composition_over_100'):
        return 'evidence_conflict'
    if row.get('composition_partial'):
        return 'partial_composition'
    if how == 'components-equal-split':
        return 'manual_review_required'
    if row.get('assessment_status') == 'assessed' and row.get('score') is not None:
        return 'scored'
    if how == 'not-disclosed':
        return 'undisclosed'
    if state == 'scoreable' and row.get('score') is not None:
        return 'scored'
    if state == 'undisclosed':
        return state
    return 'technical_failure' if state == 'technical_failure' else 'manual_review_required'


def scan(items, payload, trusted):
    rows, stats = read_products(items, payload, trusted)
    return aggregate_rows(rows, payload, trusted, stats)
