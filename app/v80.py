from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v54 as legacy
from app.v11 import connection
from app.v79 import app


logger = logging.getLogger("uvicorn.error")
JOBS = ("core", "subtitles", "previews")
conditions = {job: threading.Condition() for job in JOBS}
threads: dict[str, threading.Thread] = {}
_legacy_status = legacy.status


class IndexRequest(BaseModel):
    path: str
    indexes: list[str] = Field(default_factory=lambda: list(JOBS))
    reason: str = "Media changed"


def ensure_queue_tables() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS index_task_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, path TEXT NOT NULL,
                reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS index_task_queue_work ON index_task_queue(job,status,id);
            CREATE TABLE IF NOT EXISTS index_queue_settings (job TEXT PRIMARY KEY,paused INTEGER NOT NULL DEFAULT 0,stop_requested INTEGER NOT NULL DEFAULT 0);
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('core');
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('subtitles');
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('previews');
        """)


def validate_jobs(names: list[str]) -> list[str]:
    result = list(dict.fromkeys(names))
    if not result or any(name not in JOBS for name in result):
        raise HTTPException(400, "Indexes must contain core, subtitles, or previews")
    return result


def enqueue(job: str, path: str, reason: str = "Media changed") -> bool:
    ensure_queue_tables()
    with connection() as db:
        existing = db.execute(
            "SELECT id FROM index_task_queue WHERE job=? AND path=? AND status IN ('pending','running')",
            (job, path),
        ).fetchone()
        if existing:
            return False
        db.execute(
            "INSERT INTO index_task_queue(job,path,reason,status,created_at,updated_at) VALUES(?,?,?,'pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (job, path, reason[:300]),
        )
    logger.info("index_queue=%s event=added file=%s reason=%s", job, path.replace("\n", "\\n"), reason.replace("\n", " ")[:200])
    with conditions[job]:
        conditions[job].notify_all()
    return True


def enqueue_many(job: str, items: list[dict], reason: str) -> int:
    ensure_queue_tables()
    rows = [(job, str(item["path"]), reason[:300], job, str(item["path"])) for item in items]
    with connection() as db:
        before = db.total_changes
        db.executemany(
            """INSERT INTO index_task_queue(job,path,reason,status,created_at,updated_at)
               SELECT ?,?,?,'pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
               WHERE NOT EXISTS (
                 SELECT 1 FROM index_task_queue
                 WHERE job=? AND path=? AND status IN ('pending','running')
               )""",
            rows,
        )
        added = db.total_changes - before
    logger.info("index_queue=%s event=batch_added requested=%d added=%d reason=%s", job, len(items), added, reason)
    with conditions[job]:
        conditions[job].notify_all()
    return added


def clear_index(job: str) -> None:
    with connection() as db:
        if job == "core":
            db.execute("DELETE FROM movie_stream_index_value")
            db.execute("DELETE FROM movie_stream_index")
            db.execute("DELETE FROM tv_stream_index_value")
            db.execute("DELETE FROM tv_stream_index_media")
            db.execute("DELETE FROM external_subtitle_index")
        elif job == "subtitles":
            db.execute("DELETE FROM subtitle_extended_index")
            db.execute("DELETE FROM subtitle_extended_media")
        else:
            db.execute("DELETE FROM preview_cache_index")
            db.execute("DELETE FROM preview_cache_files")
            shutil.rmtree(legacy.CACHE_DIR, ignore_errors=True)
            legacy.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        db.execute("DELETE FROM external_sidecar_index_state WHERE job=?", (job,))


def all_index_items() -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute(
            "SELECT path,modified,size,title FROM plex_media ORDER BY kind,title COLLATE NOCASE,path"
        )]


def pending_index_items(job: str) -> list[dict]:
    if job != "core":
        return []
    import app.v79 as tv_index

    ordinary = legacy.pending(job)
    if job == "core":
        ordinary.extend(tv_index.pending_episode_rows())
    sidecars = tv_index.pending_external_sidecars(job)
    return list({str(item["path"]): item for item in [*ordinary, *sidecars]}.values())


def migrate_index_paths(changes: dict[str, str], reason: str = "Plex media path changed") -> int:
    """Move pending/failed index work to paths resolved by stable Plex IDs."""
    changes = {old: new for old, new in changes.items() if old and new and old != new}
    if not changes:
        return 0
    migrated = 0
    with connection() as db:
        for old, new in changes.items():
            for job in JOBS:
                duplicate = db.execute(
                    "SELECT id FROM index_task_queue WHERE job=? AND path=? AND status IN ('pending','running') LIMIT 1",
                    (job, new),
                ).fetchone()
                if duplicate:
                    cursor = db.execute(
                        "UPDATE index_task_queue SET status='cancelled',error=?,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP "
                        "WHERE job=? AND path=? AND status IN ('pending','failed')",
                        (f"Superseded by current Plex path: {new}", job, old),
                    )
                else:
                    candidates = db.execute(
                        "SELECT id FROM index_task_queue WHERE job=? AND path=? AND status IN ('pending','failed') ORDER BY id",
                        (job, old),
                    ).fetchall()
                    cursor = db.execute(
                        "UPDATE index_task_queue SET path=?,reason=?,status='pending',error=NULL,finished_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (new, reason[:300], candidates[0]["id"]),
                    ) if candidates else db.execute("SELECT 1 WHERE 0")
                    if len(candidates) > 1:
                        db.executemany(
                            "UPDATE index_task_queue SET status='cancelled',error=?,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            [(f"Duplicate moved-path request; retained item #{candidates[0]['id']}", item["id"]) for item in candidates[1:]],
                        )
                migrated += cursor.rowcount
            db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (old,))
            db.execute("DELETE FROM movie_stream_index WHERE path=?", (old,))
            db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (old,))
            db.execute("DELETE FROM subtitle_extended_media WHERE path=?", (old,))
            db.execute("DELETE FROM preview_cache_index WHERE path=?", (old,))
            db.execute("DELETE FROM preview_cache_files WHERE path=?", (old,))
    for old, new in changes.items():
        shutil.rmtree(legacy.cache_folder(old), ignore_errors=True)
        logger.info("index_queue event=path_migrated from=%s to=%s", old.replace("\n", "\\n"), new.replace("\n", "\\n"))
    for condition in conditions.values():
        with condition:
            condition.notify_all()
    return migrated


def discard_unchanged_plex_index_requests() -> int:
    """Remove legacy Plex-rebuild requests for media whose indexed file fingerprint still matches."""
    with connection() as db:
        obsolete = db.execute(
            """SELECT queue.id
               FROM index_task_queue queue
               JOIN plex_media media ON media.path=queue.path
               LEFT JOIN movie_stream_index movie ON media.kind='movie' AND movie.path=media.path
               LEFT JOIN tv_stream_index_media episode ON media.kind='episode' AND episode.path=media.path
               WHERE queue.job='core' AND queue.status='pending'
                 AND queue.reason='Plex catalog media added or changed'
                 AND ((media.kind='movie' AND movie.modified=media.modified AND movie.size=media.size)
                   OR (media.kind='episode' AND episode.modified=media.modified AND episode.size=media.size))"""
        ).fetchall()
        if obsolete:
            db.executemany("DELETE FROM index_task_queue WHERE id=? AND status='pending'", [(row["id"],) for row in obsolete])
    if obsolete:
        logger.info("index_queue=core event=unchanged_plex_requests_discarded count=%d", len(obsolete))
    return len(obsolete)


def deduplicate_active_index_paths() -> int:
    cancelled = 0
    with connection() as db:
        groups = db.execute(
            "SELECT job,path FROM index_task_queue WHERE status IN ('pending','running') GROUP BY job,path HAVING count(*)>1"
        ).fetchall()
        for group in groups:
            rows = db.execute(
                "SELECT id,status FROM index_task_queue WHERE job=? AND path=? AND status IN ('pending','running') "
                "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,id",
                (group["job"], group["path"]),
            ).fetchall()
            for row in rows[1:]:
                if row["status"] != "pending":
                    continue
                cancelled += db.execute(
                    "UPDATE index_task_queue SET status='cancelled',error=?,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f"Duplicate active request; retained item #{rows[0]['id']}", row["id"]),
                ).rowcount
    if cancelled:
        logger.info("index_queue event=duplicate_requests_cancelled count=%d", cancelled)
    return cancelled


def resolve_moved_plex_path(stale_path: str) -> str | None:
    """Refresh one Plex item and return its accessible current Part path."""
    with connection() as db:
        row = db.execute(
            "SELECT library_key,library_name,rating_key,kind FROM plex_media WHERE path=?",
            (stale_path,),
        ).fetchone()
    if not row or not row["rating_key"]:
        return None
    import urllib.parse
    import app.v68 as plex_sync

    metadata = plex_sync.plex.plex_request(
        f"/library/metadata/{urllib.parse.quote(str(row['rating_key']))}"
    ).get("MediaContainer", {}).get("Metadata", [])
    if not metadata:
        return None
    library = {
        "library_key": str(row["library_key"]),
        "title": str(row["library_name"]),
        "kind": "movie" if row["kind"] == "movie" else "show",
    }
    records, aliases = plex_sync.rows_for_items(library, metadata)
    current = next((str(record[0]) for record in records if Path(str(record[0])).is_file()), None)
    if not current:
        return None
    plex_sync.persist_library(library, records, aliases, int(time.time()), False)
    migrate_index_paths({stale_path: current}, "Recovered moved Plex media")
    return current


def prepare_index_check(task_id: int, payload: dict) -> dict:
    """Discover stale fingerprints in the generic queue, then fan them into the target index queue."""
    import app.v65 as generic_queue

    job = validate_jobs([str(payload.get("job") or "")])[0]
    generic_queue.update_progress(task_id, 0, 1, f"Checking {job} index fingerprints")
    items = pending_index_items(job)
    added = enqueue_many(job, items, "Incremental check") if items else 0
    generic_queue.update_progress(task_id, 1, 1, f"Check complete; {added} item(s) queued")
    logger.info("task_queue event=index_check_prepared id=%d index=%s discovered=%d added=%d", task_id, job, len(items), added)
    return {"job": job, "discovered": len(items), "queued": added}


def prepare_index_rebuild(task_id: int, payload: dict) -> dict:
    """Generic-queue task which fans a rebuild out to one dedicated index queue."""
    import app.v65 as generic_queue

    job = validate_jobs([str(payload.get("job") or "")])[0]
    with connection() as db:
        setting = db.execute("SELECT paused FROM index_queue_settings WHERE job=?", (job,)).fetchone()
        was_paused = bool(setting and setting["paused"])
        db.execute("UPDATE index_queue_settings SET paused=1 WHERE job=?", (job,))
        db.execute(
            "UPDATE index_task_queue SET status='cancelled',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP "
            "WHERE job=? AND status='pending'",
            (job,),
        )
    try:
        generic_queue.update_progress(task_id, 0, 1, f"Reading media catalog for {job} rebuild")
        items = all_index_items()
        batch_size = 250
        batch_count = max(1, (len(items) + batch_size - 1) // batch_size)
        total_steps = batch_count + 1
        clear_index(job)
        if job != "core":
            generic_queue.update_progress(task_id, 1, 1, f"Cleared {job}; this service is now on demand")
            logger.info("task_queue event=index_cache_cleared id=%d index=%s mode=on_demand", task_id, job)
            return {"job": job, "requested": 0, "queued": 0, "mode": "on_demand"}
        generic_queue.update_progress(task_id, 0, total_steps, f"Clearing {job} index")
        added = 0
        for batch_number, start in enumerate(range(0, len(items), batch_size), 1):
            batch = items[start:start + batch_size]
            added += enqueue_many(job, batch, "Full rebuild")
            queued_so_far = min(start + len(batch), len(items))
            generic_queue.update_progress(
                task_id,
                batch_number,
                total_steps,
                f"Adding media to {job} index queue: {queued_so_far}/{len(items)}",
            )
            if batch_number == 1 or batch_number % 5 == 0 or queued_so_far == len(items):
                logger.info(
                    "task_queue event=index_rebuild_prepare_progress id=%d index=%s completed=%d total=%d added=%d",
                    task_id, job, queued_so_far, len(items), added,
                )
        generic_queue.update_progress(task_id, total_steps, total_steps, f"Added {added} items to {job} index queue")
        logger.info("task_queue event=index_rebuild_prepared id=%d index=%s requested=%d added=%d", task_id, job, len(items), added)
        return {"job": job, "requested": len(items), "queued": added}
    finally:
        if not was_paused:
            with connection() as db:
                db.execute("UPDATE index_queue_settings SET paused=0,stop_requested=0 WHERE job=?", (job,))
            with conditions[job]:
                conditions[job].notify_all()


def media_indexes_for_edit(edit: dict, html_cleanups: list | None = None, remuxed: bool = False) -> list[str]:
    result = {"core"}
    removed = edit.get("remove") or []
    embedded = any(str(item).startswith("embedded:") for item in removed)
    integrated = any(item.get("embed") and f"external:{item.get('path')}" not in removed for item in edit.get("external_subtitles") or [])
    structural = embedded or integrated or bool(html_cleanups) or remuxed
    if structural:
        result.add("previews")
    return [job for job in JOBS if job in result]


def request_media_indexes(path: str, names: list[str], reason: str) -> int:
    requested = validate_jobs(names)
    if "previews" in requested:
        shutil.rmtree(legacy.cache_folder(path), ignore_errors=True)
        with connection() as db:
            db.execute("DELETE FROM preview_cache_files WHERE path=?", (path,))
            db.execute("DELETE FROM preview_cache_index WHERE path=?", (path,))
        logger.info("preview_cache event=media_invalidated file=%s reason=%s", path.replace("\n", "\\n"), reason.replace("\n", " ")[:200])
    return enqueue("core", path, reason) if "core" in requested else 0


def queue_state(job: str) -> dict:
    with connection() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,count(*) n FROM index_task_queue WHERE job=? GROUP BY status", (job,))}
        setting = db.execute("SELECT paused,stop_requested FROM index_queue_settings WHERE job=?", (job,)).fetchone()
        items = [dict(row) for row in db.execute(
            "SELECT id,path,reason,status,attempts,error,created_at,started_at FROM index_task_queue WHERE job=? AND status IN ('running','pending','failed') ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,id DESC LIMIT 20",
            (job,),
        )]
    base = _legacy_status(job)
    running = counts.get("running", 0) > 0
    return {**base, "running": running, "paused": bool(setting["paused"]), "queued": counts.get("pending", 0), "failed": counts.get("failed", 0), "completed": counts.get("succeeded", 0), "total": counts.get("pending", 0) + counts.get("running", 0), "current": "Index queue", "items": items, "queue": True}


def worker(job: str) -> None:
    logger.info("index_queue=%s event=worker_started", job)
    processed = 0
    while True:
        with connection() as db:
            setting = db.execute("SELECT paused,stop_requested FROM index_queue_settings WHERE job=?", (job,)).fetchone()
            if setting["stop_requested"]:
                db.execute("UPDATE index_task_queue SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE job=? AND status='pending'", (job,))
                db.execute("UPDATE index_queue_settings SET stop_requested=0,paused=1 WHERE job=?", (job,))
                setting = {"paused": 1}
            foreground = db.execute("SELECT 1 FROM task_queue WHERE status='running' LIMIT 1").fetchone() if job == "core" else None
            row = None if setting["paused"] or foreground else db.execute("SELECT * FROM index_task_queue WHERE job=? AND status='pending' ORDER BY id LIMIT 1", (job,)).fetchone()
            claimed = db.execute("UPDATE index_task_queue SET status='running',started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,attempts=attempts+1 WHERE id=? AND status='pending'", (row["id"],)).rowcount if row else 0
        if not row or not claimed:
            with conditions[job]: conditions[job].wait(timeout=5)
            continue
        started = time.monotonic(); path = row["path"]
        try:
            media = Path(path)
            if not media.is_file():
                recovered = resolve_moved_plex_path(path)
                if not recovered:
                    raise FileNotFoundError(f"Media file is not accessible and Plex has no accessible replacement: {path}")
                path = recovered
                media = Path(path)
                with connection() as db:
                    db.execute("UPDATE index_task_queue SET path=?,reason=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (path, "Recovered moved Plex media", row["id"]))
                logger.info("index_queue=%s event=item_path_recovered id=%d file=%s", job, row["id"], path.replace("\n", "\\n"))
            stat = media.stat()
            with connection() as db:
                catalog = db.execute("SELECT title FROM plex_media WHERE path=?", (path,)).fetchone()
            if not catalog:
                raise RuntimeError("Media is not in the synchronized Plex catalog")
            legacy.processors[job]({"path": path, "title": catalog["title"], "modified": int(stat.st_mtime), "size": stat.st_size})
            with connection() as db:
                db.execute("UPDATE index_task_queue SET status='succeeded',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,error=NULL WHERE id=?", (row["id"],))
            processed += 1
            if processed == 1 or processed % 25 == 0:
                logger.info("index_queue=%s event=progress processed=%d last_id=%d seconds=%.2f", job, processed, row["id"], time.monotonic()-started)
        except Exception as exc:
            with connection() as db:
                db.execute("UPDATE index_task_queue SET status='failed',error=?,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(getattr(exc, "detail", exc))[-3000:], row["id"]))
            logger.exception("index_queue=%s event=item_failed id=%d seconds=%.2f file=%s", job, row["id"], time.monotonic()-started, path.replace("\n", "\\n"))


@app.on_event("startup")
def initialize_index_queues() -> None:
    ensure_queue_tables()
    with connection() as db:
        db.execute("UPDATE index_task_queue SET status='pending',started_at=NULL WHERE status='running'")
        old = db.execute("SELECT id,payload_json FROM task_queue WHERE task_type='media_reindex' AND status IN ('pending','running','failed')").fetchall()
    deduplicate_active_index_paths()
    discard_unchanged_plex_index_requests()
    for item in old:
        payload = json.loads(item["payload_json"]); path = str(payload.get("path") or "")
        if path:
            request_media_indexes(path, payload.get("indexes") or list(JOBS), "Migrated from generic queue")
    if old:
        with connection() as db:
            db.execute("UPDATE task_queue SET status='cancelled',progress_message='Migrated to index queues',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE task_type='media_reindex' AND status IN ('pending','running','failed')")
        logger.info("index_queue event=generic_tasks_migrated count=%d", len(old))


def start_index_queue_workers() -> None:
    for job in JOBS:
        if job not in threads or not threads[job].is_alive():
            threads[job] = threading.Thread(target=worker, args=(job,), name=f"vse-index-queue-{job}", daemon=True)
            threads[job].start()


@app.post("/api/v80/index/request")
def add_index_request(request: IndexRequest) -> dict:
    return {"path": request.path, "indexes": validate_jobs(request.indexes), "added": request_media_indexes(request.path, request.indexes, request.reason)}


@app.get("/api/v80/setup/index/{job}/status")
def index_queue_status(job: str) -> dict:
    validate_jobs([job]); return queue_state(job)


@app.post("/api/v80/setup/index/{job}/check")
def check_index_queue(job: str) -> dict:
    validate_jobs([job])
    import app.v65 as generic_queue

    task = generic_queue.enqueue(
        "index_check_prepare",
        {"job": job},
        f"Check {job} index fingerprints",
        deduplicate=True,
    )
    logger.info("index_queue=%s event=check_requested generic_task_id=%d", job, task["id"])
    return task


@app.post("/api/v80/setup/index/{job}/rebuild")
def rebuild_index_queue(job: str) -> dict:
    validate_jobs([job])
    import app.v65 as generic_queue

    task = generic_queue.enqueue(
        "index_rebuild_prepare",
        {"job": job},
        f"Prepare {job} index rebuild",
        deduplicate=True,
    )
    logger.info("index_queue=%s event=rebuild_requested generic_task_id=%d", job, task["id"])
    return task


@app.post("/api/v80/setup/index/{job}/{action}")
def control_index_queue(job: str, action: str) -> dict:
    validate_jobs([job])
    if action not in {"pause", "resume", "stop", "retry"}: raise HTTPException(404, "Unknown index queue action")
    with connection() as db:
        if action == "pause": db.execute("UPDATE index_queue_settings SET paused=1 WHERE job=?", (job,))
        elif action == "resume": db.execute("UPDATE index_queue_settings SET paused=0,stop_requested=0 WHERE job=?", (job,))
        elif action == "stop": db.execute("UPDATE index_queue_settings SET stop_requested=1 WHERE job=?", (job,))
        else: db.execute("UPDATE index_task_queue SET status='pending',error=NULL,finished_at=NULL WHERE job=? AND status='failed'", (job,))
    with conditions[job]: conditions[job].notify_all()
    logger.info("index_queue=%s event=%s", job, action); return queue_state(job)


# Existing check endpoints and schedules resolve these module attributes at run time.
legacy.start = lambda job: enqueue_many(job, pending_index_items(job), "Scheduled incremental check")
legacy.status = queue_state

# v65 is already loaded by the application version chain. Registering here keeps
# index-specific implementation out of the generic queue module.
import app.v65 as generic_queue
generic_queue.TASK_HANDLERS["index_check_prepare"] = prepare_index_check
generic_queue.TASK_HANDLERS["index_rebuild_prepare"] = prepare_index_rebuild
