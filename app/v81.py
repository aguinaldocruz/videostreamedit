from __future__ import annotations

import logging
import shutil
import threading

from fastapi import Query

import app.v54 as preview_jobs
import app.v63 as preview_cache
import app.v65 as generic_queue
import app.v67 as index_schedule
import app.v68 as plex_schedule
import app.v80 as index_queues
from app.v2 import probe
from app.v11 import connection
from app.v28 import authorized_import_file
from app.v80 import app


logger = logging.getLogger("uvicorn.error")
monitor_thread: threading.Thread | None = None
last_risks: tuple[str, ...] = ()


def performance_snapshot() -> dict:
    with connection() as db:
        catalog = db.execute("SELECT count(*) FROM plex_media").fetchone()[0]
        core_pending = db.execute("SELECT count(*) FROM index_task_queue WHERE job='core' AND status IN ('pending','running')").fetchone()[0]
        failed = db.execute("SELECT count(*) FROM index_task_queue WHERE status='failed'").fetchone()[0]
        generic_failed = db.execute("SELECT count(*) FROM task_queue WHERE status='failed'").fetchone()[0]
        cache_bytes = db.execute("SELECT coalesce(sum(size),0) FROM preview_cache_files").fetchone()[0]
        unified_indexed = db.execute("SELECT count(distinct path) FROM media_stream_index").fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_stream_index'").fetchone() else 0
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    limit = preview_cache.cache_limit()
    risks = []
    if journal_mode.lower() != "wal":
        risks.append("SQLite WAL mode is not active")
    if catalog and core_pending > max(500, catalog // 2):
        risks.append(f"Core index backlog is high ({core_pending} of {catalog} media)")
    if failed:
        risks.append(f"{failed} index queue items require attention")
    if generic_failed:
        risks.append(f"{generic_failed} media-operation jobs require attention")
    if limit and cache_bytes >= limit * 0.9:
        risks.append("Preview cache is above 90% of its limit")
    return {"catalog": catalog, "unified_indexed": unified_indexed, "core_pending": core_pending, "index_failed": failed, "task_failed": generic_failed, "cache_bytes": cache_bytes, "cache_limit": limit, "journal_mode": journal_mode, "risks": risks}


def monitor_performance() -> None:
    global last_risks
    logger.info("performance_monitor event=worker_started")
    while True:
        try:
            risks = tuple(performance_snapshot()["risks"])
            if risks != last_risks:
                if risks:
                    logger.warning("performance_monitor event=risk_detected details=%s", " | ".join(risks))
                else:
                    logger.info("performance_monitor event=healthy")
                last_risks = risks
        except Exception as exc:
            logger.warning("performance_monitor event=check_failed error=%s", str(exc).replace("\n", " ")[:500])
        threading.Event().wait(600)


def initialize_performance_release() -> None:
    global monitor_thread
    reset_cache = False
    with connection() as db:
        applied = db.execute("SELECT 1 FROM feature_migrations WHERE name='on_demand_indexes_v1'").fetchone()
        if not applied:
            db.execute("DELETE FROM index_task_queue WHERE job IN ('subtitles','previews')")
            db.execute("DELETE FROM index_task_queue WHERE job='core' AND status IN ('succeeded','cancelled')")
            db.execute("DELETE FROM subtitle_extended_index")
            db.execute("DELETE FROM subtitle_extended_media")
            db.execute("DELETE FROM preview_cache_index")
            db.execute("DELETE FROM preview_cache_files")
            db.execute("DELETE FROM external_sidecar_index_state WHERE job!='core'")
            db.execute("UPDATE index_job_schedule SET frequency='disabled',last_run=NULL WHERE job IN ('subtitles','previews')")
            db.execute("DELETE FROM task_queue WHERE status IN ('succeeded','cancelled')")
            db.execute("INSERT INTO feature_migrations(name) VALUES('on_demand_indexes_v1')")
            reset_cache = True
    if reset_cache:
        shutil.rmtree(preview_jobs.CACHE_DIR, ignore_errors=True)
        preview_jobs.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("performance_migration event=on_demand_indexes_enabled subtitle_queue=cleared preview_queue=cleared cache=cleared")
    preview_cache.enforce_lru()
    generic_queue.start_task_queue_worker()
    index_queues.start_index_queue_workers()
    index_schedule.start_index_scheduler()
    plex_schedule.start_plex_scheduler()
    if not monitor_thread or not monitor_thread.is_alive():
        monitor_thread = threading.Thread(target=monitor_performance, name="vse-performance-monitor", daemon=True)
        monitor_thread.start()


@app.get("/api/v81/stream-preview/info")
def stream_preview_info(path: str, type_index: int = Query(default=0, ge=0), external_path: str | None = None) -> dict:
    media = authorized_import_file(path)
    try:
        duration = float((probe(media).get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    folder = preview_jobs.cache_folder(str(media))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "duration.txt").write_text(str(duration), encoding="ascii")
    return {"duration": duration}


@app.get("/api/v81/setup/performance")
def setup_performance() -> dict:
    return performance_snapshot()
