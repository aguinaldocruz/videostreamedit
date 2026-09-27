from __future__ import annotations

import logging
import os
import threading
import time

from fastapi import Query

import app.v65 as generic_queue
import app.v67 as index_schedule
import app.v68 as plex_schedule
import app.v80 as index_queues
from app.v2 import probe
from app.v11 import connection
from app.v28 import authorized_import_file
from app.v80 import app
from app.postgres_store import workflow_storage_status

logger = logging.getLogger("uvicorn.error")
monitor_thread: threading.Thread | None = None
last_risks: tuple[str, ...] = ()
monitor_shutdown = threading.Event()


def performance_snapshot() -> dict:
    with connection() as db:
        catalog = db.execute("SELECT count(*) FROM plex_media").fetchone()[0]
        core_pending = db.execute("SELECT count(*) FROM index_task_queue WHERE job='core' AND status IN ('pending','running')").fetchone()[0]
        failed = db.execute("SELECT count(*) FROM index_task_queue WHERE status='failed'").fetchone()[0]
        generic_failed = db.execute("SELECT count(*) FROM task_queue WHERE status='failed'").fetchone()[0]
        unified_indexed = db.execute("SELECT count(*) FROM media_stream_index_state s WHERE EXISTS (SELECT 1 FROM plex_media p WHERE p.path=s.path)").fetchone()[0]
    risks = []
    if catalog and core_pending > max(500, catalog // 2):
        risks.append(f"Core index backlog is high ({core_pending} of {catalog} media)")
    if failed:
        risks.append(f"{failed} index queue items require attention")
    if generic_failed:
        risks.append(f"{generic_failed} media-operation jobs require attention")
    storage = workflow_storage_status()
    if storage["status"] == "blocked":
        risks.append(f"Workflow staging blocked: only {storage['free_gb']:.2f} GiB free")
    elif storage["status"] == "warning":
        risks.append(f"Workflow staging disk reserve warning: {storage['free_gb']:.2f} GiB free")
    return {"catalog": catalog, "unified_indexed": unified_indexed, "core_pending": core_pending, "index_failed": failed, "task_failed": generic_failed, "journal_mode": "postgres", "storage": storage, "risks": risks}


def monitor_performance() -> None:
    global last_risks
    logger.info("performance_monitor event=worker_started")
    health_interval = max(30, int(os.getenv("INDEX_WORKFLOW_HEALTH_INTERVAL_SECONDS", "60")))
    while not monitor_shutdown.is_set():
        try:
            snapshot = performance_snapshot()
            repaired = index_queues.reconcile_index_workflow_stages()
            from app.job_safety import reconcile_orphans, reconcile_terminal_groups
            retired = reconcile_orphans() + reconcile_terminal_groups()
            if retired:
                logger.info('workflow event=abandoned_groups_retired count=%s', retired)
            if monitor_shutdown.is_set():
                break
            index_queues.start_index_queue_workers()
            for condition in index_queues.conditions.values():
                with condition:
                    condition.notify_all()
            if any(repaired.values()):
                logger.warning("index_queue event=runtime_workflow_repair reopened=%d terminal=%d orphaned=%d", repaired["reopened"], repaired["terminal"], repaired["orphaned"])
            risks = tuple(snapshot["risks"])
            if risks != last_risks:
                (logger.warning if risks else logger.info)("performance_monitor event=%s %s", "risk_detected" if risks else "healthy", " | ".join(risks))
                last_risks = risks
        except Exception as exc:
            logger.warning("performance_monitor event=check_failed error=%s", str(exc).replace("\n", " ")[:500])
        monitor_shutdown.wait(health_interval)


@app.on_event("shutdown")
def shutdown_performance_monitor() -> None:
    monitor_shutdown.set()
    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=2)


def initialize_performance_release() -> None:
    global monitor_thread
    generic_queue.start_task_queue_worker()
    index_queues.start_index_queue_workers()
    index_schedule.start_index_scheduler()
    plex_schedule.start_plex_scheduler()
    if not monitor_thread or not monitor_thread.is_alive():
        monitor_shutdown.clear()
        monitor_thread = threading.Thread(target=monitor_performance, name="vse-performance-monitor", daemon=True)
        monitor_thread.start()


@app.get("/api/v81/stream-preview/info")
def stream_preview_info(path: str, type_index: int = Query(default=0, ge=0), external_path: str | None = None) -> dict:
    media = authorized_import_file(path)
    try:
        duration = float((probe(media).get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {"duration": duration, "cache": False, "message": "Media review uses direct streaming; no preview cache is stored."}


@app.get("/api/v81/setup/performance")
def setup_performance() -> dict:
    return performance_snapshot()
