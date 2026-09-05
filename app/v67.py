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


logger = logging.getLogger("uvicorn.error")
schedule_thread: threading.Thread | None = None
FREQUENCY_DAYS = {"daily": 1, "every_other_day": 2, "weekly": 7}


class IndexSchedule(BaseModel):
    frequency: Literal["disabled", "daily", "every_other_day", "weekly"] = "disabled"
    time: str = "03:00"


def valid_job(job: str) -> None:
    if job not in index_jobs.JOBS:
        raise HTTPException(404, "Unknown indexing job")


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
        with connection() as db:
            schedules = [dict(row) for row in db.execute("SELECT job,frequency,time_of_day,last_run FROM index_job_schedule WHERE frequency!='disabled'")]
        for schedule in schedules:
            job = schedule["job"]
            try:
                if not schedule_due(schedule["frequency"], schedule["time_of_day"], schedule["last_run"], now):
                    continue
                if index_jobs.states[job]["running"]:
                    continue
                index_jobs.start(job)
                with connection() as db:
                    db.execute("UPDATE index_job_schedule SET last_run=?,updated_at=CURRENT_TIMESTAMP WHERE job=?", (now.isoformat(timespec="seconds"), job))
                state = index_jobs.status(job)
                logger.info("index_scheduler event=scheduled_check_started job=%s pending=%d frequency=%s", job, state.get("total", 0), schedule["frequency"])
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
            INSERT OR IGNORE INTO index_job_schedule(job) VALUES('previews');
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
