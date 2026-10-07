"""Bounded, readable scheduled-run logs, independent of Docker console logs."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, Query

from app.pg_compat import connect
from app.v2 import app

logger = logging.getLogger('uvicorn.error')
JOBS = {'subtitle_cache': 'Cache subtitles', 'core': 'Core stream metadata',
        'subtitles': 'Subtitle inspection', 'subtitle_detection': 'Subtitle language detection',
        'voice_detection': 'Voice language detection', 'plex_sync': 'Plex library sync',
        'backup': 'System backup', 'preflight_cleanup': 'Preflight history cleanup'}
RUN_LIMIT = 10
ENTRY_LIMIT = 5000


def clean(value, limit=1600):
    text = str(value or '')
    text = re.sub(r'(?i)(postgres(?:ql)?://)[^/@\s]+@', r'\1[credentials hidden]@', text)
    text = re.sub(r'(?i)((?:password|token|api[_-]?key)\s*[=:]\s*)[^\s&]+', r'\1[hidden]', text)
    return text[:limit]


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


@app.on_event('startup')
def initialize_scheduled_logs():
    with connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS scheduled_job_run (
                run_id TEXT PRIMARY KEY, job TEXT NOT NULL, source TEXT NOT NULL,
                status TEXT NOT NULL, task_id BIGINT, started_at TEXT NOT NULL,
                finished_at TEXT, summary TEXT NOT NULL DEFAULT '',
                entry_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS scheduled_job_run_latest ON scheduled_job_run(job,started_at DESC);
            CREATE TABLE IF NOT EXISTS scheduled_job_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES scheduled_job_run(run_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL, level TEXT NOT NULL,
                message TEXT NOT NULL, path TEXT NOT NULL DEFAULT '',
                queue_kind TEXT NOT NULL DEFAULT '', task_id BIGINT
            );
            CREATE INDEX IF NOT EXISTS scheduled_job_event_run ON scheduled_job_event(run_id,id DESC);
        ''')
        db.execute("UPDATE scheduled_job_run SET status='interrupted',finished_at=? "
                   "WHERE status IN ('pending','running') AND task_id IS NULL", (now(),))


def begin(job, source='manual', *, task_id=None, run_id=None, status='pending'):
    identity = run_id or (f'task-{task_id}' if task_id is not None else uuid.uuid4().hex)
    try:
        with connect() as db:
            inserted = db.execute('INSERT OR IGNORE INTO scheduled_job_run(run_id,job,source,status,task_id,started_at) VALUES(?,?,?,?,?,?)',
                                  (identity, job, source, status, task_id, now())).rowcount
            if inserted:
                record(identity, f'{JOBS[job]} requested · {source}', db=db)
            db.execute('DELETE FROM scheduled_job_run WHERE job=? AND status NOT IN (\'pending\',\'running\') '
                       'AND run_id NOT IN (SELECT run_id FROM scheduled_job_run WHERE job=? ORDER BY started_at DESC LIMIT ?)',
                       (job, job, RUN_LIMIT))
    except Exception:
        logger.exception('scheduled_log event=begin_failed job=%s', job)
    return identity


def running(run_id, message='Processing started'):
    try:
        with connect() as db:
            db.execute("UPDATE scheduled_job_run SET status='running',finished_at=NULL WHERE run_id=?", (run_id,))
            record(run_id, message, db=db)
    except Exception:
        logger.exception('scheduled_log event=start_failed')


def record(run_id, message, *, level='info', path='', queue_kind='', task_id=None, db=None):
    record_many(run_id, [{'message': message, 'level': level, 'path': path,
                         'queue_kind': queue_kind, 'task_id': task_id}], db=db)


def record_many(run_id, entries, *, db=None):
    if not entries:
        return
    if db is None:
        try:
            with connect() as owned:
                _insert_entries(owned, run_id, entries)
        except Exception:
            logger.exception('scheduled_log event=entry_failed')
        return
    # A log/storage error must not roll back the caller's successful cache
    # counters or index work, nor poison its PostgreSQL transaction.
    db.execute('SAVEPOINT scheduled_log_guard')
    try:
        _insert_entries(db, run_id, entries)
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT scheduled_log_guard')
        logger.exception('scheduled_log event=entry_failed')
    finally:
        db.execute('RELEASE SAVEPOINT scheduled_log_guard')


def _insert_entries(db, run_id, entries):
    stamp = now()
    db.executemany('INSERT INTO scheduled_job_event(run_id,created_at,level,message,path,queue_kind,task_id) VALUES(?,?,?,?,?,?,?)',
                   [(run_id, stamp, entry.get('level', 'info'), clean(entry['message']),
                     clean(entry.get('path', ''), 4000), entry.get('queue_kind', ''), entry.get('task_id')) for entry in entries])
    count = db.execute('UPDATE scheduled_job_run SET entry_count=entry_count+? WHERE run_id=? RETURNING entry_count',
                       (len(entries), run_id)).fetchone()
    # Keep live logs bounded too, not just after a long catalog pass finishes.
    if count and count['entry_count'] > ENTRY_LIMIT and count['entry_count'] % 100 < len(entries):
        db.execute('DELETE FROM scheduled_job_event WHERE run_id=? AND id NOT IN '
                   '(SELECT id FROM scheduled_job_event WHERE run_id=? ORDER BY id DESC LIMIT ?)', (run_id, run_id, ENTRY_LIMIT))


def finish(run_id, status, summary):
    try:
        with connect() as db:
            record(run_id, summary, level='error' if status=='failed' else 'info', db=db)
            db.execute('UPDATE scheduled_job_run SET status=?,finished_at=?,summary=? WHERE run_id=?',
                       (status, now(), clean(summary, 4000), run_id))
            db.execute('DELETE FROM scheduled_job_event WHERE run_id=? AND id NOT IN '
                       '(SELECT id FROM scheduled_job_event WHERE run_id=? ORDER BY id DESC LIMIT ?)', (run_id, run_id, ENTRY_LIMIT))
            db.execute('DELETE FROM scheduled_job_run WHERE job=(SELECT job FROM scheduled_job_run WHERE run_id=?) '
                       "AND status NOT IN ('pending','running') AND run_id NOT IN "
                       '(SELECT run_id FROM scheduled_job_run WHERE job=(SELECT job FROM scheduled_job_run WHERE run_id=?) '
                       'ORDER BY started_at DESC LIMIT ?)', (run_id, run_id, RUN_LIMIT))
    except Exception:
        logger.exception('scheduled_log event=finish_failed')


def _task_states(db, entries):
    for kind, table in [('task', 'task_queue'), ('index', 'index_task_queue')]:
        ids = sorted({entry['task_id'] for entry in entries if entry['queue_kind']==kind and entry['task_id'] is not None})
        if not ids:
            continue
        marks = ','.join('?' for _ in ids)
        states = {row['id']: dict(row) for row in db.execute(
            f'SELECT id,status,error FROM {table} WHERE id IN ({marks})', ids).fetchall()}
        for entry in entries:
            if entry['queue_kind']==kind and entry['task_id'] in states:
                entry['item_status'] = states[entry['task_id']]['status']
                entry['error'] = clean(states[entry['task_id']]['error'])


@app.get('/api/scheduled-tasks/{job}/latest-log')
def latest_log(job: str, before: int | None = Query(default=None, ge=1),
               run_id: str | None = None, limit: int = Query(default=100, ge=1, le=200)):
    if job not in JOBS:
        raise HTTPException(404, 'Unknown scheduled task')
    with connect() as db:
        row = db.execute('SELECT * FROM scheduled_job_run WHERE job=? ' + ('AND run_id=? ' if run_id else '') +
                         'ORDER BY started_at DESC LIMIT 1', (job, run_id) if run_id else (job,)).fetchone()
        if not row:
            return {'job': job, 'title': JOBS[job], 'run': None, 'entries': [], 'more': False}
        run = dict(row)
        if job=='subtitle_cache':
            cache = db.execute('SELECT status,current_path,processed,skipped,failed FROM subtitle_cache_run WHERE run_id=?', (run['run_id'],)).fetchone()
            if cache:
                run['status'] = cache['status']
                run['current_message'] = f"{cache['processed']} media cached · {cache['skipped']} skipped · {cache['failed']} failed" + (f" · Current media: {cache['current_path']}" if cache['current_path'] else '')
        # The durable queue remains authoritative, including cancellation,
        # retry, restart recovery, and failures before a handler is entered.
        if run['task_id'] is not None:
            task = db.execute('SELECT status,error,progress_message FROM task_queue WHERE id=?', (run['task_id'],)).fetchone()
            if task:
                run['status'] = task['status']
                run['current_message'] = clean(task['progress_message'])
                run['error'] = clean(task['error'])
        entries = [dict(item) for item in db.execute('SELECT * FROM scheduled_job_event WHERE run_id=? ' +
                   ('AND id<? ' if before else '') + 'ORDER BY id DESC LIMIT ?',
                   (run['run_id'], before, limit+1) if before else (run['run_id'], limit+1)).fetchall()]
        more = len(entries)>limit
        entries = entries[:limit]
        _task_states(db, entries)
    return {'job': job, 'title': JOBS[job], 'run': run, 'entries': entries, 'more': more,
            'before': entries[-1]['id'] if entries else None,
            'truncated': run['entry_count'] > ENTRY_LIMIT, 'entry_limit': ENTRY_LIMIT}
