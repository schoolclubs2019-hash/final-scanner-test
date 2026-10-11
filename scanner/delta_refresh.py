"""Evidence freshness and cross-run delta planning, following Standard incremental policy."""
import json
from datetime import datetime, timezone, timedelta
from scanner_store import encode

DEFAULT_EVIDENCE_MAX_AGE_DAYS = 30


def fresh(item, max_age_days, at=None):
    if not 0 < max_age_days <= 365:
        raise ValueError('evidence_max_age_days must be >0 and <=365')
    try:
        captured = datetime.fromisoformat(item['fetchedAt'].replace('Z', '+00:00'))
        current = at or datetime.now(timezone.utc)
        return timedelta(0) <= current - captured <= timedelta(days=max_age_days)
    except (KeyError, ValueError, TypeError):
        return False


def prepare_delta(store, run_id, force=False):
    run = store.run(run_id)
    previous = None if force else store.previous_snapshot(run)
    previous_id = previous['scan_run_id'] if previous else None
    ttl = float(run.get('evidence_max_age_days', DEFAULT_EVIDENCE_MAX_AGE_DAYS))
    if not 0 < ttl <= 365:
        raise ValueError('Invalid evidence freshness policy')
    counts = dict.fromkeys(('UNCHANGED','NEW','CHANGED','REMOVED','REQUIRES_RECHECK'), 0)
    for product in store.iter_products(run_id):
        key, item = product['identity'], product['item']
        old = store.db.execute('SELECT discovery_hash,item,result FROM products WHERE run_id=? AND identity=?', (previous_id, key)).fetchone() if previous_id else None
        state, reuse = 'NEW', False
        if old:
            old_hash, old_item, old_result = old
            old_item = json.loads(old_item)
            state = 'CHANGED' if old_hash != product['discovery_hash'] else 'REQUIRES_RECHECK'
            # Sitemap identity alone cannot prove unchanged content: require a
            # platform object, explicit source revision or sitemap lastmod.
            versioned = item.get('commerce_json') or item.get('source_revision') or item.get('lastmod') or item.get('manual_evidence_verified')
            if state != 'CHANGED' and not item.get('_identity_conflict') and versioned and old_result and fresh(old_item, ttl):
                if previous['scanner_fingerprint'] == run['scanner_fingerprint']:
                    state, reuse = 'UNCHANGED', True
                else:
                    # Unexpired raw evidence can be rescored under the new reader.
                    item = {**old_item, '_reuse_raw_evidence': True}
        counts[state] += 1
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO changes VALUES(?,?,?,?,?)', (run_id, key, state, old_item.get('url') if old else None, item['url']))
            if reuse:
                store.db.execute('UPDATE products SET item=?,result=?,state=?,delta=?,reused_from=? WHERE run_id=? AND identity=?',
                                 (encode(old_item), old_result, json.loads(old_result)['terminal_state'], state, previous_id, run_id, key))
            else:
                store.db.execute('UPDATE products SET item=?,delta=? WHERE run_id=? AND identity=?', (encode(item), state, run_id, key))
    if previous_id:
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO changes SELECT ?,p.identity,'REMOVED',json_extract(p.item,'$.url'),NULL FROM products p WHERE p.run_id=? AND NOT EXISTS(SELECT 1 FROM products n WHERE n.run_id=? AND n.identity=p.identity)", (run_id, previous_id, run_id))
        counts['REMOVED'] = store.db.execute("SELECT COUNT(*) FROM changes WHERE run_id=? AND state='REMOVED'", (run_id,)).fetchone()[0]
    store.update(run_id, previous_scan_run_id=previous_id, delta_counts=counts, delta_prepared=True,
                 required_fresh_reads=counts['NEW']+counts['CHANGED']+counts['REQUIRES_RECHECK'], reused_evidence=counts['UNCHANGED'])
    return previous
