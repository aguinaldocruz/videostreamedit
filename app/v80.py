from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v54 as legacy
from app.postgres_store import (
    begin_task_stage,
    workflow_storage_status,
    fail_task_stage,
    finish_task_stage,
    mark_read_models_fresh,
    register_task_stage,
    reset_task_stage_for_retry,
    task_stage_exists,
)
from app.v11 import column_exists, connection
from app.v79 import app

logger = logging.getLogger("uvicorn.error")
JOBS = ("core", "subtitles", "previews")
conditions = {job: threading.Condition() for job in JOBS}
threads: dict[str, list[threading.Thread]] = {}
WORKER_COUNTS = {"core": 1, "subtitles": 1, "previews": 1}
index_shutdown = threading.Event()


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
                detection_json TEXT,
                error TEXT,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS index_task_queue_work ON index_task_queue(job,status,id);
            CREATE TABLE IF NOT EXISTS index_queue_settings (job TEXT PRIMARY KEY,paused INTEGER NOT NULL DEFAULT 0,stop_requested INTEGER NOT NULL DEFAULT 0);
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('core');
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('subtitles');
            INSERT OR IGNORE INTO index_queue_settings(job) VALUES('previews');
            CREATE TABLE IF NOT EXISTS deferred_language_detection (path TEXT PRIMARY KEY, requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, detection_json TEXT);
        """)
        if not column_exists(db, "index_task_queue", "group_id"):
            db.execute("ALTER TABLE index_task_queue ADD COLUMN IF NOT EXISTS group_id TEXT") if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres" else db.execute("ALTER TABLE index_task_queue ADD COLUMN group_id TEXT")
        if not column_exists(db, "index_task_queue", "detection_json"):
            db.execute("ALTER TABLE index_task_queue ADD COLUMN IF NOT EXISTS detection_json TEXT") if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres" else db.execute("ALTER TABLE index_task_queue ADD COLUMN detection_json TEXT")
        if not column_exists(db, "deferred_language_detection", "detection_json"):
            db.execute("ALTER TABLE deferred_language_detection ADD COLUMN detection_json TEXT")
        if not column_exists(db, "index_task_queue", "expedite_until"):
            db.execute("ALTER TABLE index_task_queue ADD COLUMN IF NOT EXISTS expedite_until TEXT") if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres" else db.execute("ALTER TABLE index_task_queue ADD COLUMN expedite_until TEXT")
        if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres":
            # enqueue paths call this while workers write index tables. ALTER
            # TABLE takes an ACCESS EXCLUSIVE lock even when the column is
            # already BIGINT; repeating it caused worker deadlocks. Serialize
            # the migration and inspect the actual type before altering.
            db.execute("SELECT pg_advisory_xact_lock(hashtext('videostreamedit:index-column-width-v2'))")
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
                    current = db.execute(
                        "SELECT udt_name FROM information_schema.columns "
                        "WHERE table_schema=current_schema() AND table_name=? AND column_name=?",
                        (table, column),
                    ).fetchone()
                    udt = current["udt_name"] if current and isinstance(current, dict) else (current[0] if current else None)
                    if udt and str(udt).lower() not in {"int8", "bigint"}:
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


def enqueue(job: str, path: str, reason: str = "Media changed", detection: dict | None = None) -> bool:
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
            "INSERT OR IGNORE INTO index_task_queue(job,path,reason,status,group_id,expedite_until,detection_json,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (job, path, reason[:300], group_id, expedite_until, json.dumps(detection or {}, ensure_ascii=False, separators=(",", ":"))),
        )
        row = db.execute("SELECT id,group_id FROM index_task_queue WHERE job=? AND path=? AND status='pending' ORDER BY id DESC LIMIT 1", (job, path)).fetchone()
    if row:
        register_task_stage(row["group_id"], f"index:{job}", path, {"job": job, "reason": reason}, row["id"])
    logger.info("index_queue=%s event=added file=%s reason=%s", job, path.replace("\n", "\\n"), reason.replace("\n", " ")[:200])
    with conditions[job]:
        conditions[job].notify_all()
    return True


def enqueue_many(job: str, items: list[dict], reason: str) -> int:
    ensure_queue_tables()
    rows = []
    # Avoid one database round-trip per media during full-catalog maintenance.
    # Expedite inheritance is only consulted when an active boost exists.
    with connection() as db:
        boost_active = bool(db.execute("SELECT 1 FROM task_queue_expedite LIMIT 1").fetchone())
    for item in items:
        item_path = str(item["path"])
        gid = uuid.uuid4().hex
        detection = item.get("detection") or {}
        expedite = _inherited_expedite(item_path, gid) if boost_active else None
        rows.append((job, item_path, reason[:300], gid, expedite, json.dumps(detection, ensure_ascii=False, separators=(",", ":")), job, item_path))
    with connection() as db:
        before = db.total_changes
        db.executemany(
            """INSERT OR IGNORE INTO index_task_queue(job,path,reason,status,group_id,expedite_until,detection_json,created_at,updated_at)
               SELECT ?,?,?, 'pending', ?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
               WHERE NOT EXISTS (
                 SELECT 1 FROM index_task_queue
                 WHERE job=? AND path=? AND status IN ('pending','running')
               )""",
            rows,
        )
        added = db.total_changes - before
        staged_rows = db.execute(f"SELECT id,group_id,path FROM index_task_queue WHERE job=? AND reason=? AND status='pending' ORDER BY id DESC LIMIT {len(items)}", (job, reason[:300])).fetchall() if added else []
    for staged in staged_rows:
        register_task_stage(staged["group_id"], f"index:{job}", staged["path"], {"job": job, "reason": reason}, staged["id"])
    logger.info("index_queue=%s event=batch_added requested=%d added=%d reason=%s", job, len(items), added, reason)
    with conditions[job]:
        conditions[job].notify_all()
    return added


def clear_index(job: str) -> None:
    with connection() as db:
        if job == "core":
            db.execute("DELETE FROM media_stream_index")
            db.execute("DELETE FROM media_stream_index_state")
            db.execute("DELETE FROM media_video_title")
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


def _quick_core_signature(path: str, size: int | None = None) -> str | None:
    """Read only bounded file edges for incremental core-index validation."""
    try:
        media = Path(path)
        stat = media.stat()
        if size is not None and int(size) != int(stat.st_size):
            return None
        chunk = 64 * 1024
        with media.open("rb") as handle:
            first = handle.read(chunk)
            if stat.st_size > chunk:
                handle.seek(max(0, stat.st_size - chunk))
            last = handle.read(chunk)
        digest = hashlib.sha256()
        digest.update(b"core-signature-v1")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(first)
        digest.update(last)
        return digest.hexdigest()
    except OSError:
        return None


def pending_index_items(job: str) -> list[dict]:
    """Return media whose persisted fingerprint is stale for this index."""
    if job not in JOBS:
        return []
    import app.v79 as tv_index
    table = {"core": "media_stream_index_state", "subtitles": "subtitle_extended_media", "previews": "preview_cache_index"}[job]
    markup_stale = " OR coalesce(cached.markup_version,0) != 2" if job == "subtitles" else ""
    signature_select = ", cached.content_signature AS content_signature" if job == "core" else ""
    if job == "core":
        freshness = "cached.path IS NULL OR (cached.modified_ns / 1000000000) != media.modified OR cached.size != media.size"
    else:
        freshness = "cached.path IS NULL OR cached.modified != media.modified OR cached.size != media.size"
    with connection() as db:
        rows = [dict(row) for row in db.execute(f"""
            SELECT media.path, media.modified, media.size, media.title{signature_select}
              FROM plex_media media
              LEFT JOIN {table} cached ON cached.path=media.path
             WHERE ({freshness}){markup_stale}
             ORDER BY media.kind, media.title COLLATE NOCASE, media.path
        """)]
    if job == "core":
        # Existing rows with a signature can detect a rewrite that preserved
        # both size and timestamp. This is an explicit incremental check, not
        # a rebuild: only mismatching paths are returned for queueing.
        with connection() as db:
            signed = [dict(row) for row in db.execute("""
                SELECT media.path, media.modified, media.size, media.title,
                       state.content_signature
                  FROM plex_media media
                  JOIN media_stream_index_state state ON state.path=media.path
                 WHERE coalesce(state.content_signature,'') != ''
            """)]
        known = {str(item["path"]) for item in rows}
        signature_checked = 0
        signature_changed = 0
        for item in signed:
            signature_checked += 1
            current = _quick_core_signature(str(item["path"]), item["size"])
            if current and current != str(item["content_signature"]):
                signature_changed += 1
                if str(item["path"]) not in known:
                    rows.append(item)
                    known.add(str(item["path"]))
        if signature_checked:
            logger.info(
                "index_queue=core event=signature_check checked=%d changed=%d",
                signature_checked, signature_changed,
            )

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
            "media_stream_index", "media_stream_index_state",
            "media_video_title", "external_subtitle_index", "external_sidecar_index_state",
            "subtitle_extended_index", "subtitle_extended_media", "portuguese_language_detection", "portuguese_detection_state", "subtitle_detection_stream_state",
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
            db.execute("DELETE FROM media_stream_index WHERE path=?", (old,))
            db.execute("DELETE FROM media_stream_index_state WHERE path=?", (old,))
            db.execute("DELETE FROM media_video_title WHERE path=?", (old,))
            db.execute("DELETE FROM external_subtitle_index WHERE media_path=?", (old,))
            db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (old,))
            db.execute("DELETE FROM subtitle_extended_media WHERE path=?", (old,))
            db.execute("DELETE FROM portuguese_language_detection WHERE path=?", (old,))
            db.execute("DELETE FROM portuguese_detection_state WHERE path=?", (old,))
            db.execute("DELETE FROM subtitle_detection_stream_state WHERE path=?", (old,))
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
               JOIN media_stream_index_state state ON state.path=media.path
               WHERE queue.job='core' AND queue.status='pending'
                 AND queue.reason='Plex catalog media added or changed'
                 AND state.modified_ns = (media.modified * 1000000000)
                 AND state.size = media.size"""
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


def detection_scope_for_edit(edit: dict, html_cleanups: list | None = None, remuxed: bool = False) -> dict:
    """Return only detector families/stream indexes invalidated by an edit.

    Track names, default/forced flags, and stream-independent metadata do not
    alter spoken/subtitle text and therefore do not trigger detection. Structural
    operations conservatively target the affected codec family because indexes
    can renumber after removal/reordering.
    """
    scope: dict[str, set[int] | str] = {}
    tracks = edit.get("tracks") or []
    for item in tracks:
        codec = item.get("codec_type") if isinstance(item, dict) else getattr(item, "codec_type", None)
        index = item.get("type_index") if isinstance(item, dict) else getattr(item, "type_index", None)
        if codec not in {"audio", "subtitle"}:
            continue
        language_change = (item.get("language") if isinstance(item, dict) else getattr(item, "language", None)) is not None
        region_change = (item.get("region") if isinstance(item, dict) else getattr(item, "region", None)) is not None
        if language_change or region_change:
            scope.setdefault("audio_indices" if codec == "audio" else "subtitle_indices", set()).add(int(index))
    removed = edit.get("remove") or []
    for identifier in removed:
        value = str(identifier)
        if value.startswith("embedded:audio:"):
            scope["audio_indices"] = "all"
        elif value.startswith("embedded:subtitle:") or value.startswith("external:"):
            scope["subtitle_indices"] = "all"
    # Bulk editors include the complete current order in every request. Do not
    # treat that canonical order as a reorder; a real reorder is confirmed by
    # the remux result below (or by an explicit removal/integration).
    if any(item.get("embed") for item in (edit.get("external_subtitles") or [])):
        scope["subtitle_indices"] = "all"
    if html_cleanups:
        scope["subtitle_indices"] = "all"
    # A remux without explicit stream information is potentially structural.
    if remuxed and not scope:
        scope["audio_indices"] = "all"
        scope["subtitle_indices"] = "all"
    normalized = {}
    for key, value in scope.items():
        normalized[key] = value if value == "all" else sorted(int(item) for item in value)
    return normalized


def detection_scope_for_operation(operation: str) -> dict:
    """Return the detector families required by a non-editor media operation."""
    kind = str(operation or '').strip().lower()
    if kind in {'subtitle_content', 'subtitle_conversion', 'ocr_restore'}:
        return {'subtitle_indices': 'all'}
    if kind == 'media_added_or_changed':
        return {'audio_indices': 'all', 'subtitle_indices': 'all'}
    if kind in {'audio_content', 'audio_conversion'}:
        return {'audio_indices': 'all'}
    return {}


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
    # The editor sends a complete canonical order even for metadata-only edits.
    # Treat order as structural only once the operation actually remuxed.
    subtitle_reordered = bool(remuxed and (edit.get("order") or []))
    if subtitle_removed or subtitle_metadata_changed or subtitle_integrated or subtitle_reordered or html_cleanups:
        result.add("subtitles")
    audio_removed = any(str(item).startswith("embedded:audio:") for item in removed)
    structural = audio_removed or subtitle_removed or subtitle_integrated or bool(html_cleanups) or remuxed
    if structural:
        result.add("previews")
    return [job for job in JOBS if job in result]

def invalidate_language_detection(path: str, scope: dict | None = None) -> bool:
    """Remove only stale detector rows selected by an edit."""
    scope = scope or {"audio_indices": "all", "subtitle_indices": "all"}
    removed = 0
    with connection() as db:
        subtitle = scope.get("subtitle_indices")
        if subtitle:
            if subtitle == "all":
                removed += db.execute("DELETE FROM portuguese_language_detection WHERE path=?", (path,)).rowcount
                db.execute("DELETE FROM subtitle_detection_stream_state WHERE path=?", (path,))
            else:
                marks = ",".join("?" for _ in subtitle)
                removed += db.execute(f"DELETE FROM portuguese_language_detection WHERE path=? AND source='embedded' AND type_index IN ({marks})", [path, *subtitle]).rowcount
                db.execute(f"DELETE FROM subtitle_detection_stream_state WHERE path=? AND source='embedded' AND type_index IN ({marks})", [path, *subtitle])
        audio = scope.get("audio_indices")
        if audio:
            if audio == "all":
                removed += db.execute("DELETE FROM audio_language_detection WHERE path=?", (path,)).rowcount
            else:
                marks = ",".join("?" for _ in audio)
                removed += db.execute(f"DELETE FROM audio_language_detection WHERE path=? AND type_index IN ({marks})", [path, *audio]).rowcount
        if (subtitle or audio) and scope.get("reset_state", True):
            # State is a media-level optimization. Reset it only when a full
            # subtitle pass is requested; targeted rows remain valid otherwise.
            if subtitle == "all":
                db.execute("DELETE FROM portuguese_detection_state WHERE path=?", (path,))
    if removed:
        logger.info("language_detection event=invalidated file=%s scope=%s rows=%d", path.replace("\n", "\\n"), json.dumps(scope, sort_keys=True), removed)
    return bool(removed)


def invalidate_language_detections(paths: list[str]) -> int:
    return sum(1 for path in dict.fromkeys(str(item) for item in paths if item) if invalidate_language_detection(path))


def flush_deferred_language_detection(path: str) -> dict:
    ensure_queue_tables()
    with connection() as db:
        deferred = db.execute("SELECT detection_json FROM deferred_language_detection WHERE path=?", (path,)).fetchone()
        removed = db.execute("DELETE FROM deferred_language_detection WHERE path=?", (path,)).rowcount
    if not removed:
        return {"queued": False, "path": path}
    try:
        scope = json.loads(deferred["detection_json"] or "{}") if deferred else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        scope = {}
    added = 0
    subtitle_scope = {key: value for key, value in scope.items() if key.startswith("subtitle_")}
    audio_scope = {key: value for key, value in scope.items() if key.startswith("audio_")}
    if subtitle_scope:
        added += int(enqueue("subtitles", path, "Stream editor closed; targeted subtitle language detection", subtitle_scope))
    from app.v65 import enqueue as enqueue_task
    audio_payload = {"path": path, "stream_indices": audio_scope.get("audio_indices", "all")}
    audio = enqueue_task("audio_language_detection", audio_payload, "Stream editor closed; targeted voice language detection", deduplicate=True) if audio_scope else {"id": None}
    return {"queued": True, "path": path, "subtitle_added": added, "voice_task_id": audio.get("id")}


def _queue_index_dependents(path: str, completed_job: str, reason: str = "Index dependency") -> int:
    """Queue only the next required index stage for a media item.

    Core is the gate: subtitle inspection is needed only when canonical stream
    data contains subtitles. Preview-cache indexing follows subtitle inspection
    (or core directly when no subtitle stream exists).
    """
    path = str(path)
    with connection() as db:
        has_subtitles = bool(db.execute(
            "SELECT 1 FROM media_stream_index WHERE path=? AND stream_type IN ('subtitle','external') LIMIT 1",
            (path,),
        ).fetchone())
    added = 0
    if completed_job == "core":
        if has_subtitles:
            added += int(enqueue("subtitles", path, f"{reason}; core dependency complete", {"skip_detection": False}))
        else:
            added += int(enqueue("previews", path, f"{reason}; no subtitle inspection required"))
    elif completed_job == "subtitles":
        added += int(enqueue("previews", path, f"{reason}; subtitle inspection complete"))
    if added:
        logger.info("index_queue event=dependent_stage_queued file=%s completed=%s added=%d", path.replace("\n", "\\n"), completed_job, added)
    return added


def request_media_indexes(path: str, names: list[str], reason: str, *, defer_detection: bool = False, detection_scope: dict | None = None) -> int:
    requested = validate_jobs(names)
    scope = detection_scope or {}
    # Preserve the detector scope of every project-originated media write
    # for the next Plex sync; do not re-register a marker while consuming one.
    if scope and not str(reason).lower().startswith("plex catalog"):
        try:
            from app.v68 import register_internal_change_scope
            register_internal_change_scope(path, scope, reason)
        except Exception as exc:
            logger.debug("plex_sync internal scope registration skipped: %s", exc)
    if scope:
        invalidate_language_detection(path, scope)
    if "subtitles" in requested:
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
    # Smart dependency planning prevents a single media request from starting
    # all three expensive stages at once. The next stage is queued when its
    # prerequisite completes; explicit preview-only requests remain allowed.
    added = 0
    planned = list(requested)
    if "core" in planned:
        planned = ["core"]
    elif "subtitles" in planned and "previews" in planned:
        planned = ["subtitles"]
    for job in planned:
        job_scope = ({"skip_detection": True} if defer_detection else scope) if job == "subtitles" else {}
        if enqueue(job, path, reason, job_scope):
            added += 1
    with connection() as db:
        detection_row = db.execute("SELECT value FROM language_detection_settings WHERE key='incremental_detection_enabled'").fetchone()
        if path and defer_detection and scope:
            db.execute("INSERT OR REPLACE INTO deferred_language_detection(path,requested_at,detection_json) VALUES(?,CURRENT_TIMESTAMP,?)", (path, json.dumps(scope, ensure_ascii=False, separators=(",", ":"))))
    if path and scope and not defer_detection and str(detection_row["value"] if detection_row else "1") == "1":
        subtitle_scope = {key: value for key, value in scope.items() if key.startswith("subtitle_")}
        audio_scope = {key: value for key, value in scope.items() if key.startswith("audio_")}
        if subtitle_scope and "subtitles" not in requested:
            if enqueue("subtitles", path, "Changed-media targeted subtitle language detection", subtitle_scope):
                added += 1
        if audio_scope:
            from app.v65 import enqueue as enqueue_task
            payload = {"path": path, "stream_indices": audio_scope.get("audio_indices", "all")}
            audio_task = enqueue_task("audio_language_detection", payload, "Changed-media targeted voice language detection", deduplicate=True)
            if audio_task.get("status") == "pending":
                added += 1
    logger.info("index_queue event=media_indexes_requested file=%s indexes=%s detection=%s added=%d reason=%s", path.replace("\n", "\\n"), ",".join(requested), json.dumps(scope, sort_keys=True), added, reason.replace("\n", " ")[:200])
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
        items.sort(key=lambda row: (0 if row["status"] == "running" else 1 if row["status"] == "pending" else 2 if row["status"] == "failed" else 3, -row["id"]))
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
    eta_seconds = round(remaining / rate) if rate > 0 and remaining else None
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
        note = db.execute("SELECT reviewed,note,final_version FROM media_notes WHERE entity_type=? AND entity_key=?", (entity_type, entity_key)).fetchone()
        if not note or (not note["reviewed"] and not note["final_version"]):
            return False
        db.execute("UPDATE media_notes SET plex_sync_change=1,final_version=0,updated_at=CURRENT_TIMESTAMP WHERE entity_type=? AND entity_key=?", (entity_type, entity_key))
    logger.info("change=review_status_cleared reason=stream_definition_changed type=%s key=%s", entity_type, entity_key.replace("\n", " ")[:300])
    return True


def worker(job: str) -> None:
    logger.info("index_queue=%s event=worker_started", job)
    processed = 0
    while not index_shutdown.is_set():
        with connection() as db:
            setting = db.execute("SELECT paused,stop_requested FROM index_queue_settings WHERE job=?", (job,)).fetchone()
            if setting["stop_requested"]:
                db.execute("UPDATE index_task_queue SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE job=? AND status='pending'", (job,))
                db.execute("UPDATE index_queue_settings SET stop_requested=0,paused=1 WHERE job=?", (job,))
                setting = {"paused": 1}
            foreground = db.execute("SELECT 1 FROM task_queue WHERE status='running' AND task_type='index_rebuild_prepare' LIMIT 1").fetchone() if job == "core" else None
            row = None if setting["paused"] or foreground else db.execute("SELECT * FROM index_task_queue WHERE job=? AND status='pending' ORDER BY CASE WHEN expedite_until > ? THEN 0 ELSE 1 END, id LIMIT 1", (job, datetime.now().astimezone().replace(tzinfo=None).isoformat())).fetchone()
            claimed = db.execute("UPDATE index_task_queue SET status='running',started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,attempts=attempts+1 WHERE id=? AND status='pending'", (row["id"],)).rowcount if row else 0
        if not row or not claimed:
            with conditions[job]: conditions[job].wait(timeout=5)
            continue
        started = time.monotonic(); path = row["path"]
        row_group_id = row["group_id"] if "group_id" in row else None
        stage_started = False
        try:
            if not task_stage_exists(row_group_id, f"index:{job}", row["id"]):
                register_task_stage(row_group_id, f"index:{job}", path, {"job": job, "reason": row["reason"]}, row["id"])
            else:
                # A prior attempt may have failed its staged record while the
                # queue row was recovered as pending. Reopen that exact stage;
                # otherwise begin_task_stage would leave the item pending
                # forever and make successful work look like requeueing.
                reset_task_stage_for_retry(row_group_id, row["id"])
            if not begin_task_stage(row_group_id, f"index:{job}", path, row["id"]):
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
            detection_scope = {}
            try:
                detection_scope = json.loads(row["detection_json"] or "{}") if "detection_json" in row else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                detection_scope = {}
            legacy.processors[job]({"path": path, "title": catalog["title"], "modified": int(stat.st_mtime), "size": stat.st_size, "detection_scope": detection_scope})
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
                family = {"core": "common", "subtitles": "subtitles"}.get(job)
                if family:
                    mark_read_models_fresh(path, family, {"modified": int((final if settling else after).st_mtime), "size": (final if settling else after).st_size})
                finish_task_stage(row_group_id, f"index:{job}", path, {"job": job, "path": path}, row["id"])
                if job in {"core", "subtitles"}:
                    _queue_index_dependents(path, job, "Incremental dependency")
            # A final-version lock is for the current local media. A Plex
            # catalog refresh or an externally changed file must invalidate it
            # even when the replacement happens to expose the same stream
            # layout, so the user can review the new content again.
            external_change_reason = any(token in str(row["reason"] or "").lower() for token in (
                "plex", "catalog", "missing", "recovered", "fingerprint", "external", "file changed", "media added",
            ))
            if stream_structure_changed or (job == "core" and external_change_reason):
                clear_reviewed_for_index_change(path)
            processed += 1
            if processed == 1 or processed % 25 == 0:
                logger.info("index_queue=%s event=progress processed=%d last_id=%d seconds=%.2f", job, processed, row["id"], time.monotonic()-started)
        except Exception as exc:
            message = str(getattr(exc, "detail", exc))[-3000:]
            if stage_started:
                try:
                    fail_task_stage(row_group_id, f"index:{job}", path, message, row["id"])
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



def reconcile_index_workflow_stages() -> dict[str, int]:
    """Repair durable index stages before workers start.

    Index work lives in ``index_task_queue`` rather than the generic
    ``task_queue``.  A restart or an older workflow reconciliation pass can
    leave its stage cancelled/pending even though the queue item is runnable.
    Repair only index stages and leave user-task workflow state untouched.
    """
    if os.getenv("DATABASE_BACKEND", "sqlite").lower() != "postgres":
        return {"reopened": 0, "terminal": 0, "orphaned": 0}
    repaired = {"reopened": 0, "terminal": 0, "orphaned": 0}
    with connection() as db:
        # A queue item that is pending/running is authoritative.  Reopen its
        # stage, including stages cancelled by the old generic-task check.
        repaired["reopened"] = db.execute(
            """UPDATE workflow_stages s
               SET status='pending', error=NULL, started_at=NULL,
                   finished_at=NULL, updated_at=now()
             WHERE s.task_type LIKE 'index:%%'
               AND s.status IN ('cancelled','blocked')
               AND EXISTS (
                   SELECT 1 FROM index_task_queue q
                    WHERE q.id::text=s.payload->>'task_id'
                      AND q.status IN ('pending','running')
               )"""
        ).rowcount
        # Bring stages into line with terminal queue rows so a completed item
        # cannot be selected again after a restart.
        repaired["terminal"] = db.execute(
            """UPDATE workflow_stages s
               SET status=q.status,
                   error=CASE WHEN q.status='failed' THEN q.error ELSE NULL END,
                   finished_at=COALESCE(s.finished_at, now()), updated_at=now()
             FROM index_task_queue q
            WHERE s.task_type LIKE 'index:%%'
              AND s.status IN ('pending','running')
              AND q.id::text=s.payload->>'task_id'
              AND q.status IN ('succeeded','failed','cancelled')"""
        ).rowcount
        # Mark genuinely orphaned stages for visibility.  They are never
        # allowed to block a later stage, but remain auditable in the workflow
        # history instead of being silently deleted.
        repaired["orphaned"] = db.execute(
            """UPDATE workflow_stages s
               SET status='cancelled',
                   error=COALESCE(s.error, 'Orphaned index stage repaired at startup'),
                   finished_at=COALESCE(s.finished_at, now()), updated_at=now()
             WHERE s.task_type LIKE 'index:%%'
               AND s.status IN ('pending','running','blocked')
               AND s.payload->>'task_id' IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM index_task_queue q
                    WHERE q.id::text=s.payload->>'task_id'
               )"""
        ).rowcount
    if any(repaired.values()):
        logger.warning(
            "index_queue event=workflow_stage_reconciled reopened=%d terminal=%d orphaned=%d",
            repaired["reopened"], repaired["terminal"], repaired["orphaned"],
        )
    return repaired

def inherit_existing_index_expedites() -> int:
    """Apply active media/workflow boosts to index rows queued before the boost."""
    changed = 0
    try:
        from app.v65 import _active_expedite_expiry
        with connection() as db:
            if not db.execute("SELECT 1 FROM task_queue_expedite WHERE expires_at > ? LIMIT 1", (datetime.now().astimezone().replace(tzinfo=None).isoformat(),)).fetchone():
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
    reconcile_index_workflow_stages()
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
    index_shutdown.clear()
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
@app.on_event("shutdown")
def shutdown_index_queue_workers() -> None:
    index_shutdown.set()
    for condition in conditions.values():
        with condition:
            condition.notify_all()
    workers = [thread for group in threads.values() for thread in group]
    for thread in workers:
        thread.join(timeout=30)
    logger.info("index_queue event=worker_shutdown workers=%d alive=%d", len(workers), sum(thread.is_alive() for thread in workers))


def flush_language_detection(path: str) -> dict:
    from app.v80 import flush_deferred_language_detection
    return flush_deferred_language_detection(path)


@app.post("/api/v80/index/request")
def add_index_request(request: IndexRequest) -> dict:
    return {"path": request.path, "indexes": validate_jobs(request.indexes), "added": request_media_indexes(request.path, request.indexes, request.reason)}


@app.get("/api/v80/setup/index/{job}/status")
def index_queue_status(job: str) -> dict:
    validate_jobs([job]); return queue_state(job)


@app.get("/api/v80/setup/index/health")
def index_workflow_health() -> dict:
    """Return a read-only operator view of index/workflow consistency."""
    queue = {job: {"pending": 0, "running": 0, "failed": 0, "succeeded": 0, "cancelled": 0} for job in JOBS}
    stages: dict[str, dict[str, int]] = {}
    try:
        with connection() as db:
            for row in db.execute("SELECT job,status,count(*) AS n FROM index_task_queue GROUP BY job,status").fetchall():
                job = str(row["job"])
                if job in queue:
                    queue[job][str(row["status"])] = int(row["n"])
            stage_rows = db.execute(
                "SELECT task_type,status,count(*) AS n FROM workflow_stages WHERE task_type LIKE 'index:%' GROUP BY task_type,status"
            ).fetchall()
            for row in stage_rows:
                stages.setdefault(str(row["task_type"]), {})[str(row["status"])] = int(row["n"])
            # Compare stage task ids with their owning index queue. This is
            # intentionally read-only; the runtime monitor performs repairs.
            queue_status = {int(row["id"]): str(row["status"]) for row in db.execute("SELECT id,status FROM index_task_queue").fetchall()}
            queue_ids = set(queue_status)
            orphaned = recoverable = terminal_mismatch = 0
            for row in db.execute(
                "SELECT task_type,status,payload FROM workflow_stages WHERE task_type LIKE 'index:%' AND status IN ('pending','running','blocked')"
            ).fetchall():
                payload = row["payload"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = {}
                try:
                    task_id = int((payload or {}).get("task_id"))
                except (TypeError, ValueError):
                    task_id = None
                if task_id is None or task_id not in queue_ids:
                    orphaned += 1
                    continue
                qstatus = queue_status.get(task_id, "missing")
                sstatus = str(row["status"])
                if qstatus in {"pending", "running"} and sstatus in {"cancelled", "blocked"}:
                    recoverable += 1
                if qstatus in {"succeeded", "failed", "cancelled"} and sstatus in {"pending", "running"}:
                    terminal_mismatch += 1
            locks = int(db.execute("SELECT count(*) FROM workflow_locks").fetchone()[0])
    except Exception as exc:
        logger.warning("index_queue event=health_check_failed error=%s", str(exc).replace("\n", " ")[-500:])
        return {"status": "unavailable", "error": str(exc), "queue": queue, "stages": stages}
    issues = orphaned + recoverable + terminal_mismatch + sum(values["failed"] for values in queue.values())
    storage = workflow_storage_status()
    if storage["status"] != "healthy":
        issues += 1
    return {
        "status": "healthy" if issues == 0 else "attention",
        "storage": storage,
        "queue": queue,
        "stages": stages,
        "workflow_locks": locks,
        "orphaned_stages": orphaned,
        "recoverable_stages": recoverable,
        "terminal_mismatches": terminal_mismatch,
        "failed_queue_items": sum(values["failed"] for values in queue.values()),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


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
