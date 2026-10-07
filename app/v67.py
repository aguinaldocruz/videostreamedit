from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

import app.v54 as index_jobs
from app.v11 import connection
from app.v65 import app
from app import scheduled_job_log as job_log

logger = logging.getLogger("uvicorn.error")
schedule_thread: threading.Thread | None = None
FREQUENCY_DAYS = {"daily": 1, "every_other_day": 2, "weekly": 7}


class IndexSchedule(BaseModel):
    frequency: Literal["disabled", "daily", "every_other_day", "weekly"] = "disabled"
    time: str = "03:00"


def valid_job(job: str) -> None:
    if job not in (*index_jobs.JOBS, "subtitle_detection", "voice_detection"):
        raise HTTPException(404, "Unknown scheduled job")


def parse_time(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Schedule time must use HH:MM")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(400, "Schedule time must use HH:MM")
    return hour, minute


def next_scheduled(frequency: str, time_value: str, last_run: str | None) -> str | None:
    if frequency == "disabled":
        return None
    hour, minute = parse_time(time_value)
    now = datetime.now().astimezone()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if last_run:
        previous = datetime.fromisoformat(last_run).astimezone()
        candidate = previous.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=FREQUENCY_DAYS[frequency])
    while candidate <= now:
        if not last_run and candidate.date() == now.date():
            break
        candidate += timedelta(days=FREQUENCY_DAYS[frequency])
    return candidate.isoformat(timespec="minutes")


def schedule_data(job: str) -> dict:
    valid_job(job)
    with connection() as db:
        row = db.execute("SELECT frequency,time_of_day,last_run FROM index_job_schedule WHERE job=?", (job,)).fetchone()
    frequency = row["frequency"] if row else "disabled"
    time_value = row["time_of_day"] if row else "03:00"
    last_run = row["last_run"] if row else None
    return {"job": job, "frequency": frequency, "time": time_value, "last_run": last_run, "next_run": next_scheduled(frequency, time_value, last_run)}


def schedule_due(frequency: str, time_value: str, last_run: str | None, now: datetime) -> bool:
    hour, minute = parse_time(time_value)
    if (now.hour, now.minute) < (hour, minute):
        return False
    if not last_run:
        return True
    previous = datetime.fromisoformat(last_run).astimezone()
    return (now.date() - previous.date()).days >= FREQUENCY_DAYS[frequency]


def run_scheduler() -> None:
    logger.info("index_scheduler event=worker_started")
    while True:
        now = datetime.now().astimezone()
        try:
            # Final-Version retirement happens on the transition to final,
            # not on every scheduler tick. Workers also recheck eligibility.
            # In particular, do not mutate workflow tables while other startup
            # handlers may still be initializing their schema.
            with connection() as db:
                schedules = [dict(row) for row in db.execute("SELECT job,frequency,time_of_day,last_run FROM index_job_schedule WHERE frequency!='disabled'")]
        except Exception as exc:
            logger.warning("index_scheduler event=schedule_read_failed error=%s", str(exc).replace("\n", " ")[-500:])
            threading.Event().wait(30)
            continue
        for schedule in schedules:
            job = schedule["job"]
            try:
                if not schedule_due(schedule["frequency"], schedule["time_of_day"], schedule["last_run"], now):
                    continue
                if job in index_jobs.JOBS and index_jobs.status(job)["running"]:
                    continue
                queue_scheduled_job(job, source='schedule')
                with connection() as db:
                    db.execute("UPDATE index_job_schedule SET last_run=?,updated_at=CURRENT_TIMESTAMP WHERE job=?", (now.isoformat(timespec="seconds"), job))
                logger.info("index_scheduler event=scheduled_check_queued job=%s frequency=%s", job, schedule["frequency"])
            except Exception as exc:
                logger.warning("index_scheduler event=scheduled_check_failed job=%s error=%s", job, str(exc).replace("\n", " ")[-500:])
        threading.Event().wait(30)


@app.on_event("startup")
def initialize_index_schedules() -> None:
    global schedule_thread
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS index_job_schedule (
                job TEXT PRIMARY KEY,
                frequency TEXT NOT NULL DEFAULT 'disabled',
                time_of_day TEXT NOT NULL DEFAULT '03:00',
                last_run TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO index_job_schedule(job) VALUES('core');
            INSERT OR IGNORE INTO index_job_schedule(job) VALUES('subtitles');
            INSERT OR IGNORE INTO index_job_schedule(job) VALUES('subtitle_detection');
            INSERT OR IGNORE INTO index_job_schedule(job) VALUES('voice_detection');
        """)


def start_index_scheduler() -> None:
    global schedule_thread
    if not schedule_thread or not schedule_thread.is_alive():
        schedule_thread = threading.Thread(target=run_scheduler, name="vse-index-scheduler", daemon=True)
        schedule_thread.start()


@app.get("/api/v67/setup/index/{job}/schedule")
def get_index_schedule(job: str) -> dict:
    return schedule_data(job)


@app.put("/api/v67/setup/index/{job}/schedule")
def update_index_schedule(job: str, request: IndexSchedule) -> dict:
    valid_job(job)
    hour, minute = parse_time(request.time)
    baseline = None
    if request.frequency != "disabled":
        now = datetime.now().astimezone()
        interval = FREQUENCY_DAYS[request.frequency]
        baseline_time = now if (now.hour, now.minute) >= (hour, minute) else now - timedelta(days=interval)
        baseline = baseline_time.isoformat(timespec="seconds")
    with connection() as db:
        db.execute(
            "INSERT INTO index_job_schedule(job,frequency,time_of_day,last_run,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(job) DO UPDATE SET frequency=excluded.frequency,time_of_day=excluded.time_of_day,last_run=excluded.last_run,updated_at=CURRENT_TIMESTAMP",
            (job, request.frequency, request.time, baseline),
        )
    logger.info("index_scheduler event=schedule_changed job=%s frequency=%s time=%s", job, request.frequency, request.time)
    return schedule_data(job)


def queue_scheduled_job(job: str, source: str = 'manual') -> dict:
    """Run saved settings now, even when recurrence is disabled; never inline."""
    import app.v65 as tasks
    if job in index_jobs.JOBS:
        task_type = 'index_check_prepare'
    elif job in {'subtitle_detection', 'voice_detection', 'preflight_cleanup'}:
        task_type = 'scheduled_task_dispatch'
    elif job == 'subtitle_cache':
        from app.subtitle_cache_schedule import start_run
        return start_run(manual=source=='manual')
    elif job == 'plex_sync':
        from app.v68 import queue_logged_plex_check
        return queue_logged_plex_check(source)
    elif job == 'backup':
        from app.v99_backup import _start
        return _start('create', source=source)
    else:
        raise HTTPException(404, 'Unknown scheduled task')
    task = tasks.enqueue(task_type, {'job': job, '_schedule_source': source},
                         f'{job_log.JOBS[job]} · run now' if source=='manual' else f'Scheduled {job_log.JOBS[job]}', deduplicate=True)
    job_log.begin(job, source, task_id=task['id'])
    return {'accepted': True, 'task_id': task['id'], 'status': task['status']}


def dispatch_scheduled_job(task_id: int, payload: dict) -> dict:
    import app.v65 as tasks
    job = str(payload.get('job') or '')
    if job not in {'subtitle_detection', 'voice_detection', 'preflight_cleanup'}:
        raise ValueError('Unknown scheduled dispatcher task')
    run_id = job_log.begin(job, payload.get('_schedule_source', 'manual'), task_id=task_id)
    job_log.running(run_id)
    try:
        if job=='preflight_cleanup':
            from app.preflight_dispatcher import _cleanup_if_due
            result = _cleanup_if_due(force=True, log_id=run_id)
            tasks.update_progress(task_id, 1, 1, f"Cleanup finished; {result['deleted']} old requests removed")
            return result
        family = 'subtitle' if job=='subtitle_detection' else 'audio'
        from app.v80 import flush_deferred_language_detection
        with connection() as db:
            rows = db.execute('SELECT path FROM deferred_language_detection WHERE detection_json LIKE ? '
                              'ORDER BY requested_at,path LIMIT 250', (f'%"{family}_%',)).fetchall()
        queued = skipped = failed = 0
        for number, row in enumerate(rows, 1):
            path = str(row['path'])
            tasks.update_progress(task_id, number-1, len(rows), 'Dispatching '+path)
            try:
                result = flush_deferred_language_detection(path, family)
                if result.get('queued'):
                    queued += 1
                    child = result.get('voice_task_id')
                    kind = 'task'
                    if family=='subtitle':
                        with connection() as db:
                            item = db.execute("SELECT id FROM index_task_queue WHERE job='subtitles' AND path=? "
                                              "AND status IN ('pending','running') ORDER BY id DESC LIMIT 1", (path,)).fetchone()
                        child, kind = (item['id'] if item else None), 'index'
                    job_log.record(run_id, 'Detection queued; processing results appear here as the worker advances', path=path, queue_kind=kind, task_id=child)
                else:
                    skipped += 1
                    job_log.record(run_id, 'No eligible deferred detection'+(' · Final Revision' if result.get('skipped')=='final_version' else ''), path=path)
            except Exception as exc:
                failed += 1
                job_log.record(run_id, str(exc), level='error', path=path)
        tasks.update_progress(task_id, len(rows), len(rows), f'{queued} media dispatched; {skipped} skipped; {failed} failed')
        summary = f'{len(rows)} media checked · {queued} detection requests queued · {skipped} skipped · {failed} failed. Detection runs in its own queue, not in this dispatch step.'
        if failed:
            raise RuntimeError(summary)
        job_log.finish(run_id, 'completed', summary)
        return {'checked': len(rows), 'queued': queued, 'skipped': skipped, 'failed': failed}
    except Exception as exc:
        job_log.finish(run_id, 'failed', str(exc))
        raise


import app.v65 as _tasks
_tasks.TASK_HANDLERS['scheduled_task_dispatch'] = dispatch_scheduled_job


@app.post('/api/scheduled-tasks/{job}/run-now')
def run_scheduled_job_now(job: str) -> dict:
    return queue_scheduled_job(job)
