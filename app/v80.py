from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v54 as legacy
from app.v11 import connection, column_exists
from app.v79 import app
from app.postgres_store import register_task_stage, begin_task_stage, finish_task_stage, fail_task_stage, task_stage_exists, reset_task_stage_for_retry
import uuid


logger = logging.getLogger("uvicorn.error")
JOBS = ("core", "subtitles", "previews")
conditions = {job: threading.Condition() for job in JOBS}
threads: dict[str, list[threading.Thread]] = {}
WORKER_COUNTS = {"core": 2, "subtitles": 1, "previews": 1}


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
                group_id TEXT,
                error TEXT,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS index_task_queue_work ON index_task_queue(job,status,id);
            CREATE TABLE IF NOT EXISTS index_queue_settings (job TEXT PRIMARY KEY,paused INTEGER NOT NULL DEFAULT 0,stop_requested INTEGER NOT NULL DEFAULT 0);
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('core');
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('subtitles');
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('previews');
            CREATE TABLE IF NOT EXISTS deferred_language_detection (path TEXT PRIMARY KEY, requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        if not column_exists(db, "index_task_queue", "group_id"):
            db.execute("ALTER TABLE index_task_queue ADD COLUMN group_id TEXT")
        if not column_exists(db, "index_task_queue", "expedite_until"):
            db.execute("ALTER TABLE index_task_queue ADD COLUMN expedite_until TEXT")
        if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres":
            for table, columns in {
                "movie_stream_index": ("modified", "size"),
                "subtitle_extended_media": ("modified", "size"),
                "preview_cache_index": ("modified", "size", "cache_bytes"),
                "preview_cache_files": ("size",),
                "media_stream_index_state": ("modified_ns", "size"),
                "media_video_title": ("modified_ns", "size"),
                "tv_stream_index_media": ("modified", "size"),
                "external_subtitle_index": ("modified_ns", "size"),
            }.items():
                for column in columns:
                    if column_exists(db, table, column):
                        db.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT")


def validate_jobs(names: list[str]) -> list[str]:
    result = list(dict.fromkeys(names))
    if not result or any(name not in JOBS for name in result):
        raise HTTPException(400, "Indexes must contain core, subtitles, or previews")
    return result


def _inherited_expedite(path: str, group_id: str | None = None) -> str | None:
    try:
        from app.v65 import _active_expedite_expiry
        with connection() as db:
            return _active_expedite_expiry(db, group_id, path)
    except Exception:
        return None


def enqueue(job: str, path: str, reason: str = "Media changed") -> bool:
    ensure_queue_tables()
    with connection() as db:
        existing = db.execute(
            "SELECT id FROM index_task_queue WHERE job=? AND path=? AND status IN ('pending','running')",
            (job, path),
        ).fetchone()
        if existing:
            return False
        group_id = uuid.uuid4().hex
        expedite_until = _inherited_expedite(path, group_id)
        db.execute(
            "INSERT OR IGNORE INTO index_task_queue(job,path,reason,status,group_id,expedite_until,created_at,updated_at) VALUES(?,?,?,'pending',?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (job, path, reason[:300], group_id, expedite_until),
        )
        row = db.execute("SELECT id,group_id FROM index_task_queue WHERE job=? AND path=? AND status='pending' ORDER BY id DESC LIMIT 1", (job, path)).fetchone()
    if row:
        register_task_stage(row["group_id"], f"index:{job}", path, {"job": job, "reason": reason}, int(row["id"]))
    logger.info("index_queue=%s event=added file=%s reason=%s", job, path.replace("\n", "\\n"), reason.replace("\n", " ")[:200])
    with conditions[job]:
        conditions[job].notify_all()
    return True


def enqueue_many(job: str, items: list[dict], reason: str) -> int:
    ensure_queue_tables()
    rows = []
    for item in items:
        item_path = str(item["path"])
        gid = uuid.uuid4().hex
        rows.append((job, item_path, reason[:300], gid, _inherited_expedite(item_path, gid), job, item_path))
    with connection() as db:
        before = db.total_changes
        db.executemany(
            """INSERT OR IGNORE INTO index_task_queue(job,path,reason,status,group_id,expedite_until,created_at,updated_at)
               SELECT ?,?,?, 'pending', ?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
               WHERE NOT EXISTS (
                 SELECT 1 FROM index_task_queue
                 WHERE job=? AND path=? AND status IN ('pending','running')
               )""",
            rows,
        )
        added = db.total_changes - before
        staged_rows = db.execute("SELECT id,group_id,path FROM index_task_queue WHERE job=? AND reason=? AND status='pending' ORDER BY id DESC LIMIT %s" % len(items), (job, reason[:300])).fetchall() if added else []
    for staged in staged_rows:
        register_task_stage(staged["group_id"], f"index:{job}", staged["path"], {"job": job, "reason": reason}, int(staged["id"]))
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
            # Keep language-detection results when clearing the extended
            # subtitle index; they are the source for targeted rechecks.
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
    """Return media whose persisted fingerprint is stale for this index."""
    if job not in JOBS:
        return []
    import app.v79 as tv_index
    table = {"core": "media_stream_index_state", "subtitles": "subtitle_extended_media", "previews": "preview_cache_index"}[job]
    markup_stale = " OR coalesce(cached.markup_version,0) != 2" if job == "subtitles" else ""
    with connection() as db:
        rows = [dict(row) for row in db.execute(f"""
            SELECT media.path, media.modified, media.size, media.title
              FROM plex_media media
              LEFT JOIN {table} cached ON cached.path=media.path
             WHERE cached.path IS NULL OR cached.modified!=media.modified OR cached.size!=media.size{markup_stale}
             ORDER BY media.kind, media.title COLLATE NOCASE, media.path
        """)]
    # Detector upgrades are opt-in and must not turn into a full-library
    # reinspection. Targeted rechecks use the dedicated endpoint below.
    if job == "core":
        rows.extend(tv_index.pending_episode_rows())
    if job in {"core", "subtitles"}:
        rows.extend(tv_index.pending_external_sidecars(job))
    return list({str(item["path"]): item for item in rows}.values())


def prune_orphaned_index_entries() -> dict[str, int]:
    """Remove queue/index records for media no longer present in Plex catalog."""
    with connection() as db:
        queue = db.execute("UPDATE index_task_queue SET status='cancelled',error='Removed from Plex catalog',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE status IN ('pending','failed') AND path NOT IN (SELECT path FROM plex_media)").rowcount
        tables = (
            "media_stream_index", "media_stream_index_state", "movie_stream_index",
            "movie_stream_index_value", "tv_stream_index_value", "tv_stream_index_media",
            "external_subtitle_index", "external_sidecar_index_state",
            "subtitle_extended_index", "subtitle_extended_media", "portuguese_language_detection", "portuguese_detection_state",
            "preview_cache_index", "preview_cache_files",
        )
        removed = 0
        for table in tables:
            column = 'media_path' if table in {'external_subtitle_index'} else 'path'
            if table == 'external_sidecar_index_state': column = 'path'
            removed += db.execute(f"DELETE FROM {table} WHERE {column} NOT IN (SELECT path FROM plex_media)").rowcount
    if queue or removed:
        logger.info("plex_sync event=orphaned_index_entries_pruned queue=%d index_rows=%d", queue, removed)
    return {"queue": queue, "index_rows": removed}


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
            db.execute("DELETE FROM portuguese_language_detection WHERE path=?", (old,))
            db.execute("DELETE FROM portuguese_detection_state WHERE path=?", (old,))
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


def ensure_active_unique_index() -> None:
    """Make active queue deduplication atomic after historical duplicates are cleaned."""
    try:
        with connection() as db:
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS index_task_queue_active_unique
                         ON index_task_queue(job,path)
                         WHERE status IN ('pending','running')""")
    except Exception as exc:
        logger.warning("index_queue event=active_unique_index_unavailable error=%s", str(exc).replace("\n", " ")[:500])


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
        if job == "previews":
            generic_queue.update_progress(task_id, 1, 1, "Cleared previews; preview segments remain on demand")
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
    subtitle_removed = any(str(item).startswith(("embedded:subtitle:", "external:")) for item in removed)
    subtitle_metadata_changed = any(
        getattr(item, "codec_type", item.get("codec_type") if isinstance(item, dict) else None) == "subtitle"
        and (getattr(item, "language", None) is not None or getattr(item, "region", None) is not None)
        for item in (edit.get("tracks") or [])
    )
    external_changes = edit.get("external_subtitles") or []
    subtitle_integrated = any(item.get("embed") for item in external_changes)
    subtitle_reordered = any(item.get("codec_type") == "subtitle" for item in (edit.get("order") or []))
    if subtitle_removed or subtitle_metadata_changed or subtitle_integrated or subtitle_reordered or html_cleanups:
        result.add("subtitles")
    audio_removed = any(str(item).startswith("embedded:audio:") for item in removed)
    structural = audio_removed or subtitle_removed or subtitle_integrated or bool(html_cleanups) or remuxed
    if structural:
        result.add("previews")
    return [job for job in JOBS if job in result]


def invalidate_language_detection(path: str) -> bool:
    """Remove stale detection marks immediately; a later index pass recomputes them."""
    with connection() as db:
        deleted = db.execute("DELETE FROM portuguese_language_detection WHERE path=?", (path,)).rowcount
        db.execute("DELETE FROM portuguese_detection_state WHERE path=?", (path,))
    if deleted:
        logger.info("subtitle_detection event=invalidated file=%s reason=media_change", path.replace("\n", "\\n"))
    return bool(deleted)


def invalidate_language_detections(paths: list[str]) -> int:
    normalized = list(dict.fromkeys(str(path) for path in paths if path))
    if not normalized:
        return 0
    removed = 0
    with connection() as db:
        for start in range(0, len(normalized), 800):
            batch = normalized[start:start + 800]
            placeholders = ",".join("?" for _ in batch)
            removed += db.execute(f"DELETE FROM portuguese_language_detection WHERE path IN ({placeholders})", batch).rowcount
            db.execute(f"DELETE FROM portuguese_detection_state WHERE path IN ({placeholders})", batch)
    if removed:
        logger.info("subtitle_detection event=invalidated_batch media=%d marks=%d", len(normalized), removed)
    return removed


def flush_deferred_language_detection(path: str) -> dict:
    ensure_queue_tables()
    with connection() as db:
        removed = db.execute("DELETE FROM deferred_language_detection WHERE path=?", (path,)).rowcount
    if not removed:
        return {"queued": False, "path": path}
    added = enqueue("subtitles", path, "Stream editor closed; subtitle language detection")
    from app.v65 import enqueue as enqueue_task
    audio = enqueue_task("audio_language_detection", {"path": path}, "Stream editor closed; voice language detection", deduplicate=True)
    return {"queued": True, "path": path, "subtitle_added": added, "voice_task_id": audio.get("id")}


def request_media_indexes(path: str, names: list[str], reason: str, *, defer_detection: bool = False) -> int:
    requested = validate_jobs(names)
    # All media mutations request core indexing, whose worker also refreshes
    # language detection. Remove old marks before any UI can show stale results.
    if "core" in requested or "subtitles" in requested:
        invalidate_language_detection(path)
    if "subtitles" in requested:
        # Remove the old inspection while replacement indexing is queued so
        # reports and filters cannot display stale stream properties.
        with connection() as db:
            db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (path,))
            db.execute("DELETE FROM subtitle_extended_media WHERE path=?", (path,))
        logger.info("subtitle_index event=media_invalidated file=%s reason=%s", path.replace("\n", "\\n"), reason.replace("\n", " ")[:200])
    if "previews" in requested:
        shutil.rmtree(legacy.cache_folder(path), ignore_errors=True)
        with connection() as db:
            db.execute("DELETE FROM preview_cache_files WHERE path=?", (path,))
            db.execute("DELETE FROM preview_cache_index WHERE path=?", (path,))
        logger.info("preview_cache event=media_invalidated file=%s reason=%s", path.replace("\n", "\\n"), reason.replace("\n", " ")[:200])
    added = 0
    for job in requested:
        if enqueue(job, path, reason):
            added += 1
    # When enabled, every media mutation also refreshes both language detectors.
    # This covers metadata, stream order, default/forced flags, additions and removals,
    # even when the regular edit only requested the core index.
    with connection() as db:
        detection_row = db.execute("SELECT value FROM language_detection_settings WHERE key='incremental_detection_enabled'").fetchone()
        if path and defer_detection:
            db.execute("INSERT OR REPLACE INTO deferred_language_detection(path,requested_at) VALUES(?,CURRENT_TIMESTAMP)", (path,))
    if path and not defer_detection and str(detection_row["value"] if detection_row else "1") == "1":
        if enqueue("subtitles", path, "Changed-media subtitle language detection"):
            added += 1
        from app.v65 import enqueue as enqueue_task
        audio_task = enqueue_task("audio_language_detection", {"path": path}, "Changed-media voice language detection", deduplicate=True)
        if audio_task.get("status") == "pending":
            added += 1
    logger.info("index_queue event=media_indexes_requested file=%s indexes=%s added=%d reason=%s", path.replace("\n", "\\n"), ",".join(requested), added, reason.replace("\n", " ")[:200])
    return added


def queue_state(job: str) -> dict:
    with connection() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,count(*) n FROM index_task_queue WHERE job=? GROUP BY status", (job,))}
        setting = db.execute("SELECT paused,stop_requested FROM index_queue_settings WHERE job=?", (job,)).fetchone()
        # Keep each status visible: a large pending backlog must not crowd all
        # completed entries out of the setup view.
        items = []
        for item_status in ("running", "pending", "failed", "succeeded"):
            items.extend(dict(row) for row in db.execute(
                "SELECT id,path,reason,status,attempts,error,created_at,started_at FROM index_task_queue WHERE job=? AND status=? ORDER BY id DESC LIMIT 40",
                (job, item_status),
            ))
        items.sort(key=lambda row: (0 if row["status"] == "running" else 1 if row["status"] == "pending" else 2 if row["status"] == "failed" else 3, -int(row["id"])))
    # Keep status polling O(1) over the queue.  The legacy status routine
    # rescans the entire media catalog and filesystem on every refresh, which
    # made the setup screen appear frozen during large rebuilds.
    running = counts.get("running", 0) > 0
    # Keep the displayed indexed count cheap while preserving the setup UI
    # contract.  The unified core table covers both movies and episodes.
    index_table = {"core": "media_stream_index_state", "subtitles": "subtitle_extended_media", "previews": "preview_cache_index"}[job]
    with connection() as db:
        indexed = int(db.execute(f"SELECT count(*) FROM {index_table}").fetchone()[0])
        recent = db.execute("SELECT started_at,finished_at FROM index_task_queue WHERE job=? AND status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 200", (job,)).fetchall()
    durations = []
    for row in recent:
        try:
            duration = (datetime.fromisoformat(str(row["finished_at"])) - datetime.fromisoformat(str(row["started_at"]))).total_seconds()
            if duration > 0: durations.append(duration)
        except (TypeError, ValueError):
            continue
    remaining = counts.get("pending", 0) + counts.get("running", 0)
    rate = len(durations) / sum(durations) if durations else 0.0
    eta_seconds = int(round(remaining / rate)) if rate > 0 and remaining else None
    return {"running": running, "paused": bool(setting["paused"]),
            "queued": counts.get("pending", 0), "failed": counts.get("failed", 0),
            "completed": counts.get("succeeded", 0), "indexed": indexed,
            "total": remaining, "eta_seconds": eta_seconds, "rate_per_second": rate,
            "current": "Index queue", "items": items, "queue": True}


def subtitle_index_signature(path: str, job: str) -> tuple:
    """Capture stream identities before/after indexing.

    Default/forced flags and codec-only metadata deliberately do not
    participate. Language, region, track name, stream order, and source/path
    identity do participate because they change the reviewed stream meaning.
    External sidecar additions/removals are structural stream changes too.
    """
    with connection() as db:
        rows = db.execute(
            "SELECT source,stream_type,type_index,external_path,language,region,track_name FROM media_stream_index WHERE path=? ORDER BY source,stream_type,type_index,external_path",
            (path,),
        ).fetchall()
        sidecars = db.execute(
            "SELECT 'external' AS source,'subtitle' AS stream_type,-1 AS type_index,external_path,language,region,track_name FROM external_subtitle_index WHERE media_path=? ORDER BY external_path",
            (path,),
        ).fetchall()
    return tuple(sorted([tuple(row) for row in rows] + [tuple(row) for row in sidecars], key=repr))


def clear_reviewed_for_index_change(path: str) -> bool:
    with connection() as db:
        row = db.execute("SELECT kind,library_key,show_title FROM plex_media WHERE path=?", (path,)).fetchone()
        if not row:
            return False
        entity_type = "movie" if row["kind"] == "movie" else "tv"
        entity_key = path if entity_type == "movie" else f"{row['library_key']}:{row['show_title'] or 'Unknown show'}"
        note = db.execute("SELECT reviewed,note FROM media_notes WHERE entity_type=? AND entity_key=?", (entity_type, entity_key)).fetchone()
        if not note or not note["reviewed"]:
            return False
        db.execute("UPDATE media_notes SET plex_sync_change=1,updated_at=CURRENT_TIMESTAMP WHERE entity_type=? AND entity_key=?", (entity_type, entity_key))
    logger.info("change=review_status_cleared reason=stream_definition_changed type=%s key=%s", entity_type, entity_key.replace("\n", " ")[:300])
    return True


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
            foreground = db.execute("SELECT 1 FROM task_queue WHERE status='running' AND task_type='index_rebuild_prepare' LIMIT 1").fetchone() if job == "core" else None
            row = None if setting["paused"] or foreground else db.execute("SELECT * FROM index_task_queue WHERE job=? AND status='pending' ORDER BY CASE WHEN expedite_until > ? THEN 0 ELSE 1 END, id LIMIT 1", (job, datetime.utcnow().isoformat())).fetchone()
            claimed = db.execute("UPDATE index_task_queue SET status='running',started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,attempts=attempts+1 WHERE id=? AND status='pending'", (row["id"],)).rowcount if row else 0
        if not row or not claimed:
            with conditions[job]: conditions[job].wait(timeout=5)
            continue
        started = time.monotonic(); path = row["path"]
        row_group_id = row["group_id"] if "group_id" in row.keys() else None
        stage_started = False
        try:
            if not task_stage_exists(row_group_id, f"index:{job}", int(row["id"])):
                register_task_stage(row_group_id, f"index:{job}", path, {"job": job, "reason": row["reason"]}, int(row["id"]))
            else:
                # A prior attempt may have failed its staged record while the
                # queue row was recovered as pending. Reopen that exact stage;
                # otherwise begin_task_stage would leave the item pending
                # forever and make successful work look like requeueing.
                reset_task_stage_for_retry(row_group_id, int(row["id"]))
            if not begin_task_stage(row_group_id, f"index:{job}", path, int(row["id"])):
                with connection() as db:
                    db.execute("UPDATE index_task_queue SET status='pending',started_at=NULL,updated_at=CURRENT_TIMESTAMP,error='Waiting for workflow resource or prior stage' WHERE id=?", (row["id"],))
                with conditions[job]:
                    conditions[job].wait(timeout=0.25)
                continue
            stage_started = True
            media = Path(path)
            if not media.is_file():
                recovered = resolve_moved_plex_path(path)
                if not recovered:
                    from app.v65 import enqueue as enqueue_task
                    enqueue_task("plex_sync", {"rebuild": False, "source": "missing-index-media"}, "Plex sync requested for missing indexed media", deduplicate=True)
                    raise FileNotFoundError(f"Media file is not accessible and Plex has no accessible replacement: {path}")
                path = recovered
                media = Path(path)
                with connection() as db:
                    db.execute("UPDATE index_task_queue SET path=?,reason=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (path, "Recovered moved Plex media", row["id"]))
                logger.info("index_queue=%s event=item_path_recovered id=%d file=%s", job, row["id"], path.replace("\n", "\\n"))
            stat = media.stat()
            # The index processor updates media_stream_index itself; using that
            # table as a before/after fingerprint makes subtitle inspection look
            # like the media changed and causes an endless requeue loop.
            subtitle_before = subtitle_index_signature(path, job) if job == "core" else ()
            with connection() as db:
                catalog = db.execute("SELECT title FROM plex_media WHERE path=?", (path,)).fetchone()
            if not catalog:
                raise RuntimeError("Media is not in the synchronized Plex catalog")
            legacy.processors[job]({"path": path, "title": catalog["title"], "modified": int(stat.st_mtime), "size": stat.st_size})
            subtitle_after = subtitle_index_signature(path, job) if job == "core" else subtitle_before
            stream_structure_changed = subtitle_before != subtitle_after
            # A media operation can finish its final rename/write shortly
            # after the indexer returns. For edit-triggered core work, require
            # two identical fingerprints separated by a settling delay before
            # declaring success.
            after = media.stat()
            operation_reason = str(row["reason"] or "").lower()
            settling = job == "core" and any(token in operation_reason for token in ("edit", "clone", "import", "cleanup", "remux", "outside generic queue"))
            if settling:
                time.sleep(0.35)
                settled = media.stat()
                time.sleep(0.35)
                final = media.stat()
                changed = (after.st_mtime_ns != stat.st_mtime_ns or after.st_size != stat.st_size or
                           settled.st_mtime_ns != final.st_mtime_ns or settled.st_size != final.st_size)
            else:
                changed = after.st_mtime_ns != stat.st_mtime_ns or after.st_size != stat.st_size
            with connection() as db:
                if changed:
                    db.execute("UPDATE index_task_queue SET status='pending',finished_at=NULL,updated_at=CURRENT_TIMESTAMP,error='Media changed during indexing; waiting for stable fingerprint' WHERE id=?", (row["id"],))
                    logger.info("index_queue=%s event=requeued_after_change id=%d file=%s settling=%s", job, row["id"], path.replace("\n", "\\n"), settling)
                else:
                    db.execute("UPDATE index_task_queue SET status='succeeded',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,error=NULL WHERE id=?", (row["id"],))
            if stage_started and not changed:
                finish_task_stage(row_group_id, f"index:{job}", path, {"job": job, "path": path}, int(row["id"]))
            if stream_structure_changed:
                clear_reviewed_for_index_change(path)
            processed += 1
            if processed == 1 or processed % 25 == 0:
                logger.info("index_queue=%s event=progress processed=%d last_id=%d seconds=%.2f", job, processed, row["id"], time.monotonic()-started)
        except Exception as exc:
            message = str(getattr(exc, "detail", exc))[-3000:]
            if stage_started:
                try:
                    fail_task_stage(row_group_id, f"index:{job}", path, message, int(row["id"]))
                except Exception as workflow_exc:
                    logger.exception("workflow event=index_stage_failure_persist_failed id=%d error=%s", row["id"], workflow_exc)
            # Retry transient filesystem/tool/Plex-path failures a bounded
            # number of times.  This prevents a temporary outage from losing
            # the index request while guaranteeing that bad media cannot loop
            # forever; terminal failures remain visible for manual retry.
            retry = int(row["attempts"]) < 3 and "Not Found" not in message and "404" not in message
            with connection() as db:
                if retry:
                    db.execute("UPDATE index_task_queue SET status='pending',error=?,started_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (f"Retry {row['attempts']}/3: {message}", row["id"]))
                else:
                    db.execute("UPDATE index_task_queue SET status='failed',error=?,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (message, row["id"]))
            logger.warning("index_queue=%s event=item_%s id=%d attempt=%d error=%s file=%s", job, "retry" if retry else "failed", row["id"], row["attempts"], message.replace("\n", " ")[-500:], path.replace("\n", "\\n"))


def inherit_existing_index_expedites() -> int:
    """Apply active media/workflow boosts to index rows queued before the boost."""
    changed = 0
    try:
        from app.v65 import _active_expedite_expiry
        with connection() as db:
            if not db.execute("SELECT 1 FROM task_queue_expedite WHERE expires_at > ? LIMIT 1", (datetime.utcnow().isoformat(),)).fetchone():
                return 0
            rows = db.execute("SELECT id,path,group_id FROM index_task_queue WHERE status='pending' AND (expedite_until IS NULL OR expedite_until <= CURRENT_TIMESTAMP)").fetchall()
            for row in rows:
                expiry = _active_expedite_expiry(db, row["group_id"], row["path"])
                if expiry:
                    changed += db.execute("UPDATE index_task_queue SET expedite_until=? WHERE id=? AND status='pending'", (expiry, row["id"])).rowcount
    except Exception as exc:
        logger.warning("index_queue event=expedite_inheritance_failed error=%s", str(exc).replace("\n", " ")[-300:])
    if changed:
        logger.info("index_queue event=expedite_inherited_existing items=%d", changed)
    return changed


@app.on_event("startup")
def initialize_index_queues() -> None:
    ensure_queue_tables()
    with connection() as db:
        recovered = db.execute("UPDATE index_task_queue SET status='pending',started_at=NULL,error='Recovered after restart; resuming',updated_at=CURRENT_TIMESTAMP WHERE status='running'").rowcount
        if recovered:
            logger.warning("index_queue event=recovered_after_restart items=%d", recovered)
        # Align staged index records with their durable index queue rows.
        # A restart can leave a workflow stage running while its queue item is
        # pending, which otherwise blocks that item forever.
        if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres":
            db.execute("""UPDATE workflow_stages s SET status='pending', started_at=NULL, finished_at=NULL, updated_at=now()
                          WHERE s.task_type LIKE 'index:%' AND s.status='running'
                            AND s.payload->>'task_id' IN
                              (SELECT id::text FROM index_task_queue WHERE status <> 'running')""")
        old = db.execute("SELECT id,payload_json FROM task_queue WHERE task_type='media_reindex' AND status IN ('pending','running','failed')").fetchall()
    deduplicate_active_index_paths()
    ensure_active_unique_index()
    inherit_existing_index_expedites()
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
    # Core indexing is dominated by one mkvmerge process per media file. Two
    # bounded workers improve throughput without turning the storage into an
    # uncontrolled process farm; the subtitle/preview jobs remain serialized.
    for job in JOBS:
        active = [thread for thread in threads.get(job, []) if thread.is_alive()]
        for number in range(len(active), WORKER_COUNTS.get(job, 1)):
            thread = threading.Thread(
                target=worker,
                args=(job,),
                name=f"vse-index-queue-{job}-{number + 1}",
                daemon=True,
            )
            thread.start()
            active.append(thread)
        threads[job] = active


@app.post("/api/v79/language-detection/flush")
def flush_language_detection(path: str) -> dict:
    from app.v80 import flush_deferred_language_detection
    return flush_deferred_language_detection(path)


@app.post("/api/v80/index/request")
def add_index_request(request: IndexRequest) -> dict:
    return {"path": request.path, "indexes": validate_jobs(request.indexes), "added": request_media_indexes(request.path, request.indexes, request.reason)}


@app.get("/api/v80/setup/index/{job}/status")
def index_queue_status(job: str) -> dict:
    validate_jobs([job]); return queue_state(job)


@app.post("/api/v80/setup/index/subtitles/recheck-detection")
def recheck_current_detection() -> dict:
    """Reinspect only media already present in the language mismatch index."""
    with connection() as db:
        items = [dict(row) for row in db.execute(
            "SELECT DISTINCT media.path,media.modified,media.size,media.title FROM portuguese_language_detection detected JOIN plex_media media ON media.path=detected.path WHERE detected.confidence>=0.60"
        ).fetchall()]
    added = enqueue_many("subtitles", items, "Recheck current language detections") if items else 0
    logger.info("index_queue=subtitles event=detection_recheck_requested media=%d added=%d", len(items), added)
    return {"media": len(items), "queued": added}


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


@app.post("/api/v80/setup/index/{job}/clear")
def clear_index_queue(job: str) -> dict:
    validate_jobs([job])
    with connection() as db:
        active = db.execute("SELECT count(*) FROM index_task_queue WHERE job=? AND status='running'", (job,)).fetchone()[0]
        if active:
            raise HTTPException(409, f"The {job} index is running; pause or stop it before clearing")
        db.execute("UPDATE index_task_queue SET status='cancelled',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,error='Cleared by user' WHERE job=? AND status IN ('pending','failed')", (job,))
    clear_index(job)
    logger.info("index_queue=%s event=cleared_by_user", job)
    return queue_state(job)


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
    if action not in {"pause", "resume", "stop", "retry", "delete-failed"}: raise HTTPException(404, "Unknown index queue action")
    with connection() as db:
        if action == "pause": db.execute("UPDATE index_queue_settings SET paused=1 WHERE job=?", (job,))
        elif action == "resume": db.execute("UPDATE index_queue_settings SET paused=0,stop_requested=0 WHERE job=?", (job,))
        elif action == "stop": db.execute("UPDATE index_queue_settings SET stop_requested=1 WHERE job=?", (job,))
        elif action == "delete-failed": db.execute("DELETE FROM index_task_queue WHERE job=? AND status='failed'", (job,))
        else: db.execute("UPDATE index_task_queue SET status='pending',attempts=0,error=NULL,started_at=NULL,finished_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE job=? AND status='failed'", (job,))
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
