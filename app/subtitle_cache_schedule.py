"""Time-bounded incremental scheduler for complete text-subtitle caching."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.pg_compat import connect
from app.subtitle_cache import CACHE_FORMAT_VERSION, complete_pending_media, failed_media, invalidate_media, missing_cache_paths, next_pending_media, pending_revision, prioritize_final_media, record_media_failure, retry_failed_media
from app.subtitle_cache_worker import cache_media, ordered_catalog_candidates
from app.v2 import app


logger = logging.getLogger("uvicorn.error")
LOCK_ID = 731846291
FREQUENCY_DAYS = {"daily": 1, "every_other_day": 2, "weekly": 7}
worker_thread: threading.Thread | None = None
timer_thread: threading.Thread | None = None
shutdown_event = threading.Event()
stop_event = threading.Event()
thread_lock = threading.Lock()
remaining_work_lock = threading.Lock()
remaining_work_snapshot = (0.0, 0)


class CacheSchedule(BaseModel):
    frequency: Literal["disabled", "daily", "every_other_day", "weekly"] = "disabled"
    time: str = "03:00"
    hours: int = Field(default=1, ge=0, le=168)
    minutes: int = Field(default=0, ge=0, le=59)


def _parse_clock(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Schedule time must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Schedule time must use HH:MM")
    return hour, minute


def _due(row: dict, now: datetime) -> bool:
    if row["frequency"] == "disabled":
        return False
    hour, minute = _parse_clock(row["time_of_day"])
    if (now.hour, now.minute) < (hour, minute):
        return False
    last_run = row["last_run"]
    if not last_run:
        return True
    previous = datetime.fromisoformat(str(last_run)).astimezone()
    return (now.date() - previous.date()).days >= FREQUENCY_DAYS[row["frequency"]]


def _next_run(row: dict) -> str | None:
    if row["frequency"] == "disabled":
        return None
    now = datetime.now().astimezone()
    hour, minute = _parse_clock(row["time_of_day"])
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if row["last_run"]:
        previous = datetime.fromisoformat(str(row["last_run"])).astimezone()
        candidate = previous.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=FREQUENCY_DAYS[row["frequency"]])
    elif candidate <= now:
        candidate += timedelta(days=1)
    while candidate <= now:
        candidate += timedelta(days=FREQUENCY_DAYS[row["frequency"]])
    return candidate.isoformat(timespec="minutes")


def ensure_schema() -> None:
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS subtitle_cache_schedule (
                id INTEGER PRIMARY KEY CHECK(id=1),
                frequency TEXT NOT NULL DEFAULT 'disabled',
                time_of_day TEXT NOT NULL DEFAULT '03:00',
                run_minutes INTEGER NOT NULL DEFAULT 60,
                last_run TEXT,
                cursor_offset INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO subtitle_cache_schedule(id) VALUES(1);
            CREATE TABLE IF NOT EXISTS subtitle_cache_run (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                run_minutes INTEGER NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                current_path TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                cursor_offset INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS subtitle_cache_run_recent ON subtitle_cache_run(started_at DESC);
        """)


def _configuration() -> dict:
    with connect() as db:
        return dict(db.execute("SELECT frequency,time_of_day,run_minutes,last_run,cursor_offset FROM subtitle_cache_schedule WHERE id=1").fetchone())


def _remaining_work_count() -> int:
    """Count actionable cache misses, not all catalog media or cursor rows.

    The setup screen polls every five seconds. A short-lived snapshot avoids
    repeating this catalog-wide database count on every poll while keeping
    the displayed work percentage responsive.
    """
    global remaining_work_snapshot
    now = time.monotonic()
    if now - remaining_work_snapshot[0] < 10:
        return remaining_work_snapshot[1]
    with remaining_work_lock:
        now = time.monotonic()
        if now - remaining_work_snapshot[0] < 10:
            return remaining_work_snapshot[1]
        with connect() as db:
            catalog = db.execute("""
                SELECT count(*) AS n FROM plex_media p
                LEFT JOIN subtitle_cache_media c ON c.path=p.path
                LEFT JOIN media_stream_index_state s ON s.path=p.path
                LEFT JOIN subtitle_cache_failure f ON f.path=p.path
                WHERE f.path IS NULL
                  AND (c.path IS NULL OR c.format_version!=? OR c.expected_tracks!=c.cached_tracks
                       OR c.cached_tracks!=(SELECT count(*) FROM subtitle_cache_track t WHERE t.path=p.path))
                  AND (s.path IS NULL OR EXISTS (
                       SELECT 1 FROM media_stream_index i WHERE i.path=p.path
                         AND i.stream_type IN ('subtitle','external')))
            """, (CACHE_FORMAT_VERSION,)).fetchone()["n"]
            pending_outside_catalog = db.execute("""
                SELECT count(*) AS n FROM subtitle_cache_pending q
                LEFT JOIN plex_media p ON p.path=q.path
                LEFT JOIN subtitle_cache_failure f ON f.path=q.path
                WHERE p.path IS NULL AND f.path IS NULL
            """).fetchone()["n"]
        remaining_work_snapshot = (now, int(catalog) + int(pending_outside_catalog))
        return remaining_work_snapshot[1]


def _final_uncached_paths(db) -> list[str]:
    """Reconcile Final Revision items that moved before the saved cursor."""
    rows = db.execute("""
        SELECT p.path FROM plex_media p
        LEFT JOIN media_notes n ON n.entity_type=CASE WHEN p.kind='movie' THEN 'movie' ELSE 'tv' END
          AND n.entity_key=CASE WHEN p.kind='movie' THEN p.path ELSE 'episode:'||p.path END
        LEFT JOIN media_notes sn ON p.kind='episode' AND sn.entity_type='tv'
          AND sn.entity_key=p.library_key||':'||COALESCE(p.show_title,'Unknown show')
        LEFT JOIN subtitle_cache_media c ON c.path=p.path
        WHERE (COALESCE(n.final_version,0)=1 OR COALESCE(sn.final_version,0)=1)
          AND (c.path IS NULL OR c.format_version!=? OR c.expected_tracks!=c.cached_tracks)
          AND (NOT EXISTS (SELECT 1 FROM media_stream_index_state s WHERE s.path=p.path)
               OR EXISTS (SELECT 1 FROM media_stream_index i WHERE i.path=p.path
                          AND i.stream_type IN ('subtitle','external')))
          AND NOT EXISTS (SELECT 1 FROM subtitle_cache_failure f WHERE f.path=p.path)
    """, (CACHE_FORMAT_VERSION,)).fetchall()
    return [str(row["path"]) for row in rows]


def _worker(run_id: str, budget_minutes: int) -> None:
    lease = None
    status = "completed"
    error = ""
    try:
        lease = connect()
        locked = lease.raw.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_ID,)).fetchone()["locked"]
        lease.raw.commit()
        if not locked:
            raise RuntimeError("Another subtitle cache worker already owns the run lease")
        with connect() as db:
            db.execute("UPDATE subtitle_cache_run SET status='running' WHERE run_id=?", (run_id,))
        deadline = time.monotonic() + budget_minutes * 60
        offset = int(_configuration()["cursor_offset"])
        failed_paths = {item["path"] for item in failed_media()}
        while not shutdown_event.is_set() and not stop_event.is_set():
            if time.monotonic() >= deadline:
                status = "time_limit"
                break
            pending_path = next_pending_media(int(time.time()))
            if pending_path:
                requested_revision = pending_revision(pending_path)
                with connect() as db:
                    db.execute("UPDATE subtitle_cache_run SET current_path=? WHERE run_id=?", (pending_path, run_id))
                try:
                    if not Path(pending_path).is_file():
                        revision = pending_revision(pending_path)
                        invalidate_media(pending_path)
                        complete_pending_media(pending_path, revision)
                        outcome = "skipped"
                    else:
                        result = cache_media(
                            Path(pending_path),
                            remaining_seconds=lambda: 0 if stop_event.is_set() or shutdown_event.is_set()
                            else max(0, deadline - time.monotonic()),
                            low_priority=True,
                        )
                        outcome = "skipped" if result["status"] in {"cached", "no_subtitles"} else "processed"
                except InterruptedError:
                    status = "time_limit" if time.monotonic() >= deadline else "cancelled"
                    break
                except Exception as exc:
                    outcome = "failed"
                    error = f"{pending_path}: {str(exc).replace(chr(10), ' ')[:400]}"
                    if record_media_failure(pending_path, error, requested_revision):
                        failed_paths.add(pending_path)
                    logger.warning("subtitle_cache event=priority_media_failed path=%s error=%s", pending_path, error[-400:])
                with connect() as db:
                    db.execute(f"UPDATE subtitle_cache_run SET {outcome}={outcome}+1,last_error=?,current_path='' WHERE run_id=?",
                               (error, run_id))
                continue
            paths = ordered_catalog_candidates(limit=100, offset=offset)
            if not paths:
                with connect() as db:
                    db.execute("UPDATE subtitle_cache_schedule SET cursor_offset=0 WHERE id=1")
                break
            uncached = missing_cache_paths(paths)
            if not uncached:
                offset += len(paths)
                with connect() as db:
                    db.execute("UPDATE subtitle_cache_schedule SET cursor_offset=? WHERE id=1", (offset,))
                    db.execute("UPDATE subtitle_cache_run SET skipped=skipped+?,cursor_offset=?,current_path='' WHERE run_id=?", (len(paths), offset, run_id))
                if len(paths) < 100:
                    with connect() as db:
                        db.execute("UPDATE subtitle_cache_schedule SET cursor_offset=0 WHERE id=1")
                    break
                continue
            page_completed = True
            for path in paths:
                if shutdown_event.is_set() or stop_event.is_set() or time.monotonic() >= deadline:
                    page_completed = False
                    break
                if path in failed_paths or path not in uncached:
                    offset += 1
                    with connect() as db:
                        db.execute("UPDATE subtitle_cache_schedule SET cursor_offset=? WHERE id=1", (offset,))
                        db.execute("UPDATE subtitle_cache_run SET skipped=skipped+1,cursor_offset=? WHERE run_id=?", (offset, run_id))
                    continue
                with connect() as db:
                    db.execute("UPDATE subtitle_cache_run SET current_path=? WHERE run_id=?", (path, run_id))
                try:
                    result = cache_media(
                        Path(path),
                        remaining_seconds=lambda: 0 if stop_event.is_set() or shutdown_event.is_set()
                        else max(0, deadline - time.monotonic()),
                        low_priority=True,
                    )
                    outcome = "skipped" if result["status"] in {"cached", "no_subtitles"} else "processed"
                except InterruptedError:
                    status = "time_limit" if time.monotonic() >= deadline else "cancelled"
                    page_completed = False
                    break
                except Exception as exc:
                    outcome = "failed"
                    error = f"{path}: {str(exc).replace(chr(10), ' ')[:400]}"
                    if record_media_failure(path, error):
                        failed_paths.add(path)
                    logger.warning("subtitle_cache event=media_failed path=%s error=%s", path, error[-400:])
                offset += 1
                with connect() as db:
                    db.execute("UPDATE subtitle_cache_schedule SET cursor_offset=? WHERE id=1", (offset,))
                    db.execute(f"UPDATE subtitle_cache_run SET {outcome}={outcome}+1,cursor_offset=?,last_error=?,current_path='' WHERE run_id=?", (offset, error, run_id))
            if page_completed and len(paths) < 100:
                with connect() as db:
                    db.execute("UPDATE subtitle_cache_schedule SET cursor_offset=0 WHERE id=1")
                break
            if not page_completed:
                break
        if stop_event.is_set() or shutdown_event.is_set():
            status = "cancelled"
        elif time.monotonic() >= deadline and status == "completed":
            status = "time_limit"
    except Exception as exc:
        status = "failed"
        error = str(exc).replace("\n", " ")[:500]
        logger.exception("subtitle_cache event=run_failed id=%s", run_id)
    finally:
        try:
            with connect() as db:
                db.execute("UPDATE subtitle_cache_run SET status=?,finished_at=?,current_path='',last_error=? WHERE run_id=?",
                           (status, datetime.now(timezone.utc).isoformat(timespec="microseconds"), error, run_id))
        except Exception:
            logger.exception("subtitle_cache event=run_status_save_failed id=%s", run_id)
        if lease:
            try:
                lease.raw.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
                lease.raw.commit()
            except Exception:
                logger.exception("subtitle_cache event=lease_release_failed id=%s", run_id)
            finally:
                lease.close()
        stop_event.clear()


def start_run(manual: bool = True) -> dict:
    global worker_thread
    with thread_lock:
        if worker_thread and worker_thread.is_alive():
            raise HTTPException(409, "Subtitle cache is already running")
        config = _configuration()
        budget = int(config["run_minutes"])
        run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with connect() as db:
            final_uncached = _final_uncached_paths(db)
            db.execute("INSERT INTO subtitle_cache_run(run_id,status,started_at,run_minutes,cursor_offset) VALUES(?,'pending',?,?,?)",
                       (run_id, now, budget, int(config["cursor_offset"])))
            db.execute("UPDATE subtitle_cache_schedule SET last_run=? WHERE id=1", (now,))
            db.execute("DELETE FROM subtitle_cache_run WHERE run_id NOT IN "
                       "(SELECT run_id FROM subtitle_cache_run ORDER BY started_at::timestamptz DESC LIMIT 250) "
                       "AND status NOT IN ('pending','running')")
        if final_uncached:
            prioritized = prioritize_final_media(final_uncached)
            logger.info("subtitle_cache event=final_revision_prioritized media=%d", prioritized)
        stop_event.clear()
        worker_thread = threading.Thread(target=_worker, args=(run_id, budget), name="vse-subtitle-cache", daemon=True)
        worker_thread.start()
    return {"run_id": run_id, "status": "pending", "run_minutes": budget}


def _timer() -> None:
    while not shutdown_event.wait(30):
        try:
            if _due(_configuration(), datetime.now().astimezone()):
                start_run(manual=False)
        except HTTPException:
            pass
        except Exception as exc:
            logger.warning("subtitle_cache event=schedule_tick_failed error=%s", str(exc).replace("\n", " ")[:300])


@app.on_event("startup")
def start_subtitle_cache_scheduler() -> None:
    global timer_thread
    ensure_schema()
    with connect() as db:
        db.execute("UPDATE subtitle_cache_run SET status='interrupted',finished_at=CURRENT_TIMESTAMP,current_path='' WHERE status IN ('pending','running')")
    shutdown_event.clear()
    if not timer_thread or not timer_thread.is_alive():
        timer_thread = threading.Thread(target=_timer, name="vse-subtitle-cache-schedule", daemon=True)
        timer_thread.start()


@app.on_event("shutdown")
def stop_subtitle_cache_scheduler() -> None:
    shutdown_event.set()
    stop_event.set()
    if worker_thread and worker_thread.is_alive():
        worker_thread.join(timeout=60)


@app.get("/api/subtitle-cache/schedule")
def get_schedule() -> dict:
    config = _configuration()
    with connect() as db:
        row = db.execute("SELECT * FROM subtitle_cache_run ORDER BY started_at::timestamptz DESC,run_id DESC LIMIT 1").fetchone()
        total = db.execute("SELECT count(*) AS n FROM plex_media").fetchone()["n"]
        priority_pending = db.execute("SELECT count(*) AS n FROM subtitle_cache_pending").fetchone()["n"]
        failed_media_count = db.execute("SELECT count(*) AS n FROM subtitle_cache_failure").fetchone()["n"]
    return {"frequency": config["frequency"], "time": config["time_of_day"],
            "hours": config["run_minutes"] // 60, "minutes": config["run_minutes"] % 60,
            "cursor_offset": config["cursor_offset"], "total_media": total,
            "remaining_work": _remaining_work_count(),
            "priority_pending": priority_pending, "failed_media_count": failed_media_count, "next_run": _next_run(config),
            "latest_run": dict(row) if row else None}


@app.put("/api/subtitle-cache/schedule")
def update_schedule(request: CacheSchedule) -> dict:
    try:
        _parse_clock(request.time)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    budget = request.hours * 60 + request.minutes
    if budget < 1:
        raise HTTPException(400, "Run duration must be at least one minute")
    baseline = None
    if request.frequency != "disabled":
        now = datetime.now().astimezone()
        hour, minute = _parse_clock(request.time)
        interval = FREQUENCY_DAYS[request.frequency]
        baseline_time = now if (now.hour, now.minute) >= (hour, minute) else now - timedelta(days=interval)
        baseline = baseline_time.isoformat(timespec="seconds")
    with connect() as db:
        db.execute("UPDATE subtitle_cache_schedule SET frequency=?,time_of_day=?,run_minutes=?,last_run=? WHERE id=1",
                   (request.frequency, request.time, budget, baseline))
    return get_schedule()


@app.get("/api/subtitle-cache/failures")
def get_failures() -> dict:
    return {"items": failed_media()}


@app.post("/api/subtitle-cache/failures/retry")
def retry_failures() -> dict:
    return {"queued": retry_failed_media()}


@app.post("/api/subtitle-cache/failures/retry-one")
def retry_one_failure(path: str) -> dict:
    queued = retry_failed_media(path)
    if not queued:
        raise HTTPException(404, "This media is no longer in the failed subtitle-cache list")
    return {"queued": queued, "path": path}


@app.post("/api/subtitle-cache/run-now")
def run_now() -> dict:
    return start_run(manual=True)


@app.post("/api/subtitle-cache/stop")
def stop_run() -> dict:
    if not worker_thread or not worker_thread.is_alive():
        raise HTTPException(409, "Subtitle cache is not running")
    stop_event.set()
    return {"stopping": True, "message": "Finishing or discarding the current subtitle before stopping"}
