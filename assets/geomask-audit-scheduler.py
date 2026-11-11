#!/usr/bin/env python3
"""Run the per-path audit schedules that are due.

Invoked by one cron entry every five minutes. For each configured path whose time has
come, run the isolated check; in ``check-and-apply`` mode also apply the resulting pool
through the same preview/apply path the panel uses, so both entry points share one
implementation and one transaction owner.

Nothing here widens a filter: ``apply_preview`` refuses an empty pool and refuses a
report whose subscription no longer matches the live config.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent

def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

audit = _load('node_audit', 'geomask-node-audit.py')
apply = _load('node_apply', 'geomask-apply.py')

# A path is run at most once per local day, and only after its configured minute.
STATE = Path(os.environ.get('GM_AUDIT_STATE', '/etc/geomask/node-audit/state'))
RUNS = STATE / 'schedule-runs.json'
TOLERANCE_MINUTES = 360

def _now():
    return time.localtime()

def _minutes(hhmm):
    try:
        hh, mm = hhmm.split(':')
        return int(hh) * 60 + int(mm)
    except Exception:
        return None

def _load_runs():
    try:
        return json.loads(RUNS.read_bytes())
    except Exception:
        return {}

def _save_runs(data):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = RUNS.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, RUNS)

def due(entry, now):
    """True when this path's schedule should fire now."""
    schedule = entry.get('schedule') or {}
    if not schedule.get('enabled'):
        return False
    target = _minutes(schedule.get('time') or '')
    if target is None:
        return False
    current = now.tm_hour * 60 + now.tm_min
    delta = current - target
    if delta < 0 or delta > TOLERANCE_MINUTES:
        return False
    today = '%04d-%02d-%02d' % (now.tm_year, now.tm_mon, now.tm_mday)
    return entry.get('_last_run') != today

def _source_revision():
    """Digest of the live mihomo config; matches what a check records."""
    return audit.digest(audit.CONFIG.read_bytes())

def run_one(entry):
    ident = entry['id']
    mode = (entry.get('schedule') or {}).get('mode', 'check-only')
    result = {'path': ident, 'mode': mode}

    started = audit.start(ident, actor='cron')
    if not started.get('ok'):
        return dict(result, ok=False, stage='start', error=started.get('error'))

    job_id = started.get('job_id')
    deadline = time.monotonic() + 900
    report = None
    while time.monotonic() < deadline:
        report = audit.audit_job_get(job_id).get('job')
        if report and report.get('status') in ('complete', 'error', 'stale', 'cancelled'):
            break
        time.sleep(3)
    if not report:
        return dict(result, ok=False, stage='check', error='no job state')
    if report.get('status') != 'complete':
        return dict(result, ok=False, stage='check', status=report.get('status'),
                    error=report.get('error'))
    result['check'] = report.get('counts')

    if mode != 'check-and-apply':
        return dict(result, ok=True, applied=False)

    try:
        source_revision = _source_revision()
        preview = apply.preview_apply(ident, report.get('job_id'),
                                      report.get('settings_revision'), source_revision)
        applied = apply.apply_preview(ident, preview['preview_id'],
                                      report.get('settings_revision'))
        return dict(result, ok=True, applied=True,
                    node_count=applied.get('node_count'), tx_id=applied.get('tx_id'))
    except Exception as exc:
        return dict(result, ok=False, stage='apply', applied=False,
                    error=str(exc) if isinstance(exc, (apply.ApplyError, audit.AuditError))
                    else type(exc).__name__)

def main():
    now = _now()
    runs = _load_runs()
    try:
        settings = audit.get()
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__}))
        return 1

    today = '%04d-%02d-%02d' % (now.tm_year, now.tm_mon, now.tm_mday)
    results = []
    for entry in settings.get('paths', []):
        entry['_last_run'] = (runs.get(entry['id']) or {}).get('date')
        if not due(entry, now):
            continue
        outcome = run_one(entry)
        results.append(outcome)
        runs[entry['id']] = {'date': today, 'at': time.strftime('%F %T'),
                             'result': outcome}
        _save_runs(runs)

    # Expired applied pools are dropped on the same tick.
    try:
        expired = apply.handle_expired_pools()
    except Exception as exc:
        expired = {'ok': False, 'error': type(exc).__name__}

    print(json.dumps({'ok': True, 'ran': results, 'expired': expired}, ensure_ascii=False))
    return 0

if __name__ == '__main__':
    sys.exit(main())
