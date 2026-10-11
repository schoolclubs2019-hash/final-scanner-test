"""Opt-in, durable scan visits. Publication never runs inside a visit budget."""
import math
import time

DEFAULT_POLICY = {
    'http_timeout_seconds': 30,
    'http_retries': 2,
    'authority_retry_limit': 2,
    'authority_probe_retry_limit': 1,
    'authority_backoff_base_seconds': 5,
    'authority_backoff_cap_seconds': 300,
    'browser_timeout_ms': 30000,
    'first_pass_seconds': 900,
    'revisit_seconds': 3600,
    'first_inline_wait_seconds': 30,
    'revisit_inline_wait_seconds': 120,
    'revisit_delay_seconds': 900,
    'max_revisit_passes': 3,
    'max_coordinator_wait_seconds': 3600,
    'workers': 4,
}
_LIMITS = {
    'http_timeout_seconds': (1, 300), 'http_retries': (0, 10),
    'browser_timeout_ms': (1000, 300000),
    'authority_retry_limit': (1, 12), 'authority_probe_retry_limit': (0, 12),
    'authority_backoff_base_seconds': (1, 300), 'authority_backoff_cap_seconds': (1, 3600),
    'first_pass_seconds': (1, 86400), 'revisit_seconds': (1, 86400),
    'first_inline_wait_seconds': (0, 3600), 'revisit_inline_wait_seconds': (0, 3600),
    'revisit_delay_seconds': (1, 86400), 'max_revisit_passes': (0, 10),
    'max_coordinator_wait_seconds': (0, 86400), 'workers': (1, 32),
}
PAUSE_REASONS = {'site_pass_budget', 'domain_backoff', 'worker_backoff', 'disk_pressure'}


def normalize_policy(value):
    """None preserves the existing execution model; {} opts into all defaults."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError('Invalid execution_policy keys')
    policy = {**DEFAULT_POLICY, **value}
    for key, (minimum, maximum) in _LIMITS.items():
        item = policy[key]
        if type(item) is not int or not minimum <= item <= maximum:
            raise ValueError(f'execution_policy.{key} must be an integer from {minimum} to {maximum}')
    if policy['authority_probe_retry_limit'] > policy['authority_retry_limit']:
        raise ValueError('Authority probe retry limit cannot exceed normal retry limit')
    if policy['authority_backoff_cap_seconds'] < policy['authority_backoff_base_seconds']:
        raise ValueError('Authority backoff cap cannot be smaller than its base')
    for phase in ('first', 'revisit'):
        duration = 'first_pass_seconds' if phase == 'first' else 'revisit_seconds'
        if policy[f'{phase}_inline_wait_seconds'] > policy[duration]:
            raise ValueError('Inline wait allowance cannot exceed its visit duration')
    return policy


def with_policy(record, policy):
    """Normalize fresh input only; callers must never replace a saved run policy."""
    selected = normalize_policy(record.get('execution_policy', policy))
    return {**record, **({'execution_policy': selected} if selected is not None else {})}


class ScanPaused(Exception):
    def __init__(self, reason='site_pass_budget', eligible_at=0):
        if reason not in PAUSE_REASONS or not math.isfinite(float(eligible_at)):
            raise ValueError('Invalid scan pause')
        self.reason, self.eligible_at = reason, max(0, float(eligible_at))
        super().__init__(reason)


class VisitBudget:
    """Cooperative wall-clock deadline, persisted before the first operation.

    A timeout bounds a blocking transport operation, not DNS/browser startup or
    the entire site. Checkpoints stop the scan without losing saved evidence.
    """
    def __init__(self, store, run_id, clock=None, sleep=None):
        self.store, self.run_id = store, run_id
        self.clock, self.sleep = clock or time.time, sleep or time.sleep
        run = store.run(run_id)
        self.policy = normalize_policy(run.get('execution_policy'))
        if self.policy is None or (run.get('scan_visit') or {}).get('state') != 'ACTIVE':
            raise ValueError('VisitBudget requires an active saved visit')
        self.visit = dict(run['scan_visit'])

    def remaining(self):
        return max(0, float(self.visit['deadline_at']) - self.clock())

    def check(self):
        if self.remaining() <= 0:
            raise ScanPaused()

    def timeout(self, seconds):
        self.check()
        return min(float(seconds), self.remaining())

    def wait_until(self, deadline, reason='domain_backoff'):
        self.check()
        now = self.clock()
        remaining = max(0, float(deadline) - now)
        if not remaining:
            return
        phase = 'first' if self.visit['phase'] == 'first' else 'revisit'
        allowance = self.policy[f'{phase}_inline_wait_seconds']
        # Reserve before sleeping. Restarting reuses the same reservation.
        reserved = float(self.visit.get('wait_reserved_until', 0))
        additional = max(0, float(deadline) - max(now, reserved))
        used = float(self.visit.get('inline_wait_used', 0))
        # Per-wait allowance; the saved visit deadline bounds total elapsed time.
        if remaining >= self.remaining() or remaining > allowance:
            raise ScanPaused(reason, deadline)
        if additional:
            self.visit.update(inline_wait_used=used + additional, wait_reserved_until=float(deadline))
            self.store.update(self.run_id, scan_visit=dict(self.visit))
        while (remaining := float(deadline) - self.clock()) > 0:
            self.check()
            self.sleep(min(30, remaining, self.remaining()))
        self.check()


def begin_visit(store, run_id, phase='first', clock=None, sleep=None):
    clock = clock or time.time
    if phase not in {'first', 'revisit'}:
        raise ValueError('Invalid scan visit phase')
    run = store.run(run_id)
    policy = normalize_policy(run.get('execution_policy'))
    if policy is None:
        return None
    visit = run.get('scan_visit')
    if visit and visit.get('state') == 'ACTIVE':
        return VisitBudget(store, run_id, clock, sleep)
    if visit:
        if (phase != 'revisit' or visit.get('state') != 'PAUSED'
                or visit.get('number', 0) >= policy['max_revisit_passes']
                or float(run.get('scan_next_eligible_at', 0)) > clock()):
            return None
        number = int(visit['number']) + 1
    else:
        if phase != 'first':
            return None
        number = 0
    now = clock()
    duration = policy['first_pass_seconds' if phase == 'first' else 'revisit_seconds']
    visit = {'number': number, 'phase': phase, 'state': 'ACTIVE',
             'started_at': now, 'deadline_at': now + duration, 'inline_wait_used': 0}
    store.update(run_id, scan_visit=visit, scan_next_eligible_at=0)
    return VisitBudget(store, run_id, clock, sleep)


def pause_visit(store, run_id, pause, clock=None):
    clock = clock or time.time
    run = store.run(run_id)
    policy = normalize_policy(run['execution_policy'])
    visit = dict(run['scan_visit'])
    visit.update(state='PAUSED', ended_at=clock(), pause_reason=pause.reason)
    eligible = max(clock() + policy['revisit_delay_seconds'], pause.eligible_at)
    store.update(run_id, status='DEFERRED', defer_reason=pause.reason, error=pause.reason,
                 scan_visit=visit, scan_next_eligible_at=eligible)
    return store.run(run_id)


def finish_visit(store, run_id, clock=None):
    clock = clock or time.time
    run = store.run(run_id)
    visit = dict(run['scan_visit'])
    visit.update(state='FINISHED', ended_at=clock())
    store.update(run_id, scan_visit=visit, scan_next_eligible_at=0)
    return store.run(run_id)


def revisit_state(run, now=None):
    """Public scheduler projection; a due check never changes counters/state."""
    now = time.time() if now is None else now
    policy = normalize_policy(run.get('execution_policy'))
    visit = run.get('scan_visit') or {}
    if policy is None or run.get('scanner_status') == 'SCANNER_COMPLETE' or run.get('htt_publish_status') == 'PASS':
        return 'held', None
    if visit.get('state') == 'ACTIVE':
        return 'due', now
    if (visit.get('state') != 'PAUSED' or run.get('defer_reason') not in PAUSE_REASONS
            or visit.get('number', 0) >= policy['max_revisit_passes']):
        return 'held', None
    eligible = float(run.get('scan_next_eligible_at', 0))
    return ('due' if eligible <= now else 'waiting'), eligible
