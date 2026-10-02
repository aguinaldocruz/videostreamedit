import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.postgres_store import (
    _lock_workflow_mutation,
    initialize_schema as initialize_postgres_workflow_schema,
)
from app.postgres_store import (
    luw_details,
    luw_read_model,
    read_model_state,
    read_model_summary,
    recover_luws,
    cleanup_terminal_luw_locks,
    rollback_workflow,
    workflow_details,
    workflow_read_model,
)
from app.v11 import column_exists, connection
from app.v84 import app

logger = logging.getLogger("videostreamedit")

# Phase 1 durable preflight dispatcher routes and lifecycle hooks.
from app import preflight_dispatcher as _preflight_dispatcher  # noqa: F401,E402
from app import v99_backup as _backup  # noqa: F401,E402
from app import dashboard as _dashboard  # noqa: F401,E402


class LanguageRegionUse(BaseModel):
    value: str = Field(min_length=1, max_length=32)


class WorkflowRollbackRequest(BaseModel):
    # Explicit confirmation prevents an accidental media restore from a stale UI.
    confirm: Literal["ROLLBACK"]


class RecoveryDiscardRequest(BaseModel):
    confirm: Literal["DELETE RECOVERY"]
    review_token: str = Field(min_length=64, max_length=64)


class MediaNoteRequest(BaseModel):
    entity_type: Literal["movie", "tv"]
    entity_key: str = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=4000)
    reviewed: bool | None = None
    plex_sync_change: bool | None = None
    final_version: bool | None = None


@app.on_event("startup")
def initialize_language_region_usage() -> None:
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS language_region_selection_usage (
                value TEXT PRIMARY KEY,
                use_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS media_notes (
                entity_type TEXT NOT NULL CHECK(entity_type IN ('movie','tv')),
                entity_key TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                reviewed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(entity_type, entity_key)
            )
        """)
        if not column_exists(db, "media_notes", "reviewed"):
            db.execute("ALTER TABLE media_notes ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0")
        if not column_exists(db, "media_notes", "plex_sync_change"):
            db.execute("ALTER TABLE media_notes ADD COLUMN plex_sync_change INTEGER NOT NULL DEFAULT 0")
        if not column_exists(db, "media_notes", "final_version"):
            db.execute("ALTER TABLE media_notes ADD COLUMN final_version INTEGER NOT NULL DEFAULT 0")


@app.on_event("startup")
def remove_legacy_projection_schema() -> None:
    """Final startup guard: old projection tables cannot be recreated by legacy imports."""
    with connection() as db:
        removed = []
        for table in ("movie_stream_index_value", "movie_stream_index", "tv_stream_index_value", "tv_stream_index_media"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
            removed.append(table)
    logger.info("canonical_index event=legacy_projection_schema_removed tables=%s", ",".join(removed))



def _path_candidates(path: str) -> list[str]:
    """Return equivalent path spellings used by Plex and the filesystem."""
    raw = str(path)
    candidates = [raw]
    try:
        resolved = str(Path(raw).resolve())
    except (OSError, RuntimeError):
        resolved = raw
    if resolved not in candidates:
        candidates.append(resolved)
    return candidates


def final_version_entity_for_path(path: str) -> tuple[str, str] | None:
    candidates = _path_candidates(path)
    with connection() as db:
        row = None
        matched_path = None
        for candidate in candidates:
            row = db.execute(
                "SELECT kind,library_key,show_title,path FROM plex_media WHERE path=?",
                (candidate,),
            ).fetchone()
            if row:
                matched_path = str(row["path"])
                break
    if not row or matched_path is None:
        return None
    if row["kind"] == "movie":
        return ("movie", matched_path)
    return ("tv", "episode:" + matched_path)


def assert_no_tv_draft_save(path: str) -> None:
    # A submitted TV draft owns the show until its worker completes. Enforce
    # this server-side too, across other tabs, reports and direct API calls.
    from app.v79 import tv_commit_owner
    with connection() as db:
        pending = db.execute("SELECT s.session_id FROM tv_edit_sessions s JOIN plex_media p ON s.show_id=p.library_key||':'||p.show_title WHERE p.path=? AND p.kind='episode' AND s.status='committing' LIMIT 1", (path,)).fetchone()
    if pending and pending['session_id'] != tv_commit_owner.get():
        raise HTTPException(423, 'This TV show has a submitted draft being processed. Wait for its save task to complete before changing it.')


def assert_media_editable(path: str) -> None:
    assert_no_tv_draft_save(path)
    entity = final_version_entity_for_path(path)
    if not entity:
        return
    with connection() as db:
        row = db.execute(
            "SELECT final_version FROM media_notes WHERE entity_type=? AND entity_key=?",
            entity,
        ).fetchone()
        locked = bool(row and row["final_version"])
        if not locked and entity[0] == "tv" and entity[1].startswith("episode:"):
            media_path = entity[1][len("episode:"):]
            media = db.execute(
                "SELECT library_key,show_title FROM plex_media WHERE path=?",
                (media_path,),
            ).fetchone()
            if media:
                parent = ("tv", f"{media['library_key']}:{media['show_title'] or 'Unknown show'}")
                parent_row = db.execute(
                    "SELECT final_version FROM media_notes WHERE entity_type=? AND entity_key=?",
                    parent,
                ).fetchone()
                locked = bool(parent_row and parent_row["final_version"])
    if locked:
        raise HTTPException(
            423,
            "This media is marked Final version and is view-only. Open its notes and unfreeze it before making changes.",
        )


@app.get("/api/v86/workflows/{group_id}")
def get_workflow(group_id: str) -> dict:
    """Read staged group, stage, artifact, and lock state for task review."""
    details = workflow_details(group_id)
    if not details:
        raise HTTPException(404, "Workflow group not found")
    return details


def _recovery_review(db, group_id: uuid.UUID) -> dict:
    group = db.execute(
        "SELECT group_id,status,resource_key,definition FROM workflow_groups WHERE group_id=?::uuid",
        (str(group_id),),
    ).fetchone()
    if not group:
        raise HTTPException(404, "Workflow group not found")
    root = Path(os.environ.get("WORKFLOW_STAGE_ROOT", "/data/workflow-staging")).resolve()
    rows = db.execute(
        "SELECT artifact_id,original_path,artifact_path,status FROM workflow_artifacts WHERE group_id=?::uuid ORDER BY artifact_id",
        (str(group_id),),
    ).fetchall()
    files = []
    for row in rows:
        path = Path(str(row["artifact_path"]))
        safe = not path.is_symlink() and path.resolve().is_relative_to(root)
        try:
            stat = path.stat() if safe and path.is_file() else None
        except OSError:
            stat = None
        original = str(row["original_path"] or "")
        files.append({
            "artifact_id": int(row["artifact_id"]),
            "original_path": original,
            "original_exists": bool(original and Path(original).is_file()),
            "staged_path": str(path),
            "exists": stat is not None,
            "size_bytes": stat.st_size if stat else 0,
            "mtime_ns": stat.st_mtime_ns if stat else None,
            "safe_path": safe,
            "status": str(row["status"]),
        })
    token_data = {"group_id": str(group_id), "files": files}
    token = hashlib.sha256(json.dumps(token_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    definition = group["definition"] or {}
    return {
        "group_id": str(group_id), "status": str(group["status"]),
        "resource_key": str(group["resource_key"] or ""),
        "retained": bool(definition.get("recovery_retained")),
        "retired_jobs": bool(definition.get("retired_invalid_jobs")),
        "files": files, "review_token": token,
    }


@app.get("/api/v86/workflows/{group_id}/recovery")
def get_workflow_recovery(group_id: str) -> dict:
    try:
        uid = uuid.UUID(group_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid workflow group") from exc
    with connection() as db:
        return _recovery_review(db, uid)


@app.post("/api/v86/workflows/{group_id}/recovery/discard")
def discard_retained_workflow_recovery(group_id: str, request: RecoveryDiscardRequest) -> dict:
    """Permanently delete only explicitly reviewed, retired recovery copies."""
    try:
        uid = uuid.UUID(group_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid workflow group") from exc
    with connection() as db:
        _lock_workflow_mutation(db.raw)
        db.execute("SELECT group_id FROM workflow_groups WHERE group_id=?::uuid FOR UPDATE", (str(uid),)).fetchone()
        review = _recovery_review(db, uid)
        if not review["retained"] or not review["retired_jobs"] or review["status"] != "failed":
            raise HTTPException(409, "Only explicitly retained recovery from retired failed jobs can be discarded")
        if review["review_token"] != request.review_token:
            raise HTTPException(409, "Recovery changed since review; inspect it again before deletion")
        if not review["files"] or any(not item["safe_path"] for item in review["files"]):
            raise HTTPException(409, "Recovery is missing or outside the managed staging area")
        active = db.execute(
            "SELECT 1 FROM task_queue WHERE replace(group_id,'-','')=? AND status IN ('pending','running','failed') LIMIT 1",
            (uid.hex,),
        ).fetchone()
        active_stage = db.execute(
            "SELECT 1 FROM workflow_stages WHERE group_id=?::uuid AND status IN ('pending','running','blocked') LIMIT 1",
            (str(uid),),
        ).fetchone()
        locked = db.execute("SELECT 1 FROM workflow_locks WHERE group_id=?::uuid LIMIT 1", (str(uid),)).fetchone()
        active_luw = db.execute(
            "SELECT 1 FROM workflow_luws WHERE group_id=?::uuid AND status IN ('locked','applying','verifying') LIMIT 1",
            (str(uid),),
        ).fetchone()
        if active or active_stage or locked or active_luw:
            raise HTTPException(409, "Workflow became active; recovery cannot be deleted")
        deleted, bytes_removed = 0, 0
        for item in review["files"]:
            path = Path(item["staged_path"])
            if item["exists"]:
                try:
                    path.unlink()
                except OSError as exc:
                    raise HTTPException(503, f"Could not delete recovery file: {exc}") from exc
                deleted += 1
                bytes_removed += item["size_bytes"]
        db.execute("DELETE FROM workflow_artifacts WHERE group_id=?::uuid", (str(uid),))
        db.execute(
            "UPDATE workflow_groups SET definition=definition || ?::jsonb,updated_at=now() WHERE group_id=?::uuid",
            (json.dumps({"recovery_retained": False, "recovery_discarded_by_user": True}), str(uid)),
        )
    try:
        (Path(os.environ.get("WORKFLOW_STAGE_ROOT", "/data/workflow-staging")) / str(uid)).rmdir()
    except OSError:
        pass
    logger.warning("workflow event=retained_recovery_discarded group=%s files=%d bytes=%d", uid, deleted, bytes_removed)
    return {"group_id": str(uid), "deleted_files": deleted, "bytes_removed": bytes_removed}


@app.get("/api/v86/luws")
def list_luws(limit: int = 200, status: str | None = None, resource: str | None = None) -> dict:
    """Return summary-first durable operation state for the redesigned task UI."""
    try:
        return luw_read_model(limit=limit, status=status, resource=resource)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v86/operational-summary")
def get_operational_summary() -> dict:
    """Return constant-size queue/workflow/read-model counts for diagnostics."""
    try:
        with connection() as db:
            task_rows = db.execute("SELECT status, count(*) AS amount FROM task_queue GROUP BY status").fetchall()
            index_rows = db.execute("SELECT job, status, count(*) AS amount FROM index_task_queue GROUP BY job, status").fetchall()
            workflow_rows = db.execute("SELECT status, count(*) AS amount FROM workflow_groups GROUP BY status").fetchall()
        return {
            "tasks": {str(row["status"]): int(row["amount"]) for row in task_rows},
            "indexes": _group_counts(index_rows, "job", "status"),
            "workflows": {str(row["status"]): int(row["amount"]) for row in workflow_rows},
            "read_models": read_model_summary(),
        }
    except Exception as exc:
        raise HTTPException(503, f"Operational summary unavailable: {str(exc)[:240]}") from exc


def _group_counts(rows, outer: str, inner: str) -> dict:
    result = {}
    for row in rows:
        result.setdefault(str(row[outer]), {})[str(row[inner])] = int(row["amount"])
    return result


@app.get("/api/v86/readiness")
def get_readiness() -> dict:
    """Compact readiness probe for the redesigned PostgreSQL/LUW/read-model stack."""
    checks = {"database": "ok", "workflow_schema": "ok", "read_models": "ok"}
    try:
        with connection() as db:
            db.execute("SELECT 1").fetchone()
            db.execute("SELECT 1 FROM workflow_luws LIMIT 1").fetchone()
    except Exception as exc:
        checks["database"] = "error"
        checks["workflow_schema"] = "error"
        return {"status": "not_ready", "checks": checks, "error": str(exc)[:240]}
    try:
        summary = read_model_summary()
    except Exception as exc:
        checks["read_models"] = "error"
        return {"status": "degraded", "checks": checks, "error": str(exc)[:240]}
    return {"status": "ready", "checks": checks, "read_models": summary}


@app.get("/api/v86/read-model-summary")
def get_read_model_summary() -> dict:
    """Return aggregate index freshness without scanning media in the browser."""
    return read_model_summary()


@app.get("/api/v86/read-model-state")
def get_read_model_state(limit: int = 200, resource: str | None = None) -> dict:
    """Return per-media index freshness for incremental filter/report updates."""
    return {"items": read_model_state(resource=resource, limit=limit)}


@app.get("/api/v86/workflow-read-model")
def list_workflow_read_model(limit: int = 100, status: str | None = None) -> dict:
    """Return parent workflow/stage summaries for queue progress views."""
    try:
        return workflow_read_model(limit=limit, status=status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v86/luws/{luw_id}")
def get_luw(luw_id: str) -> dict:
    """Return one LUW, its lifecycle events, and minimal rollback journal."""
    details = luw_details(luw_id)
    if not details:
        raise HTTPException(404, "LUW not found")
    return details


@app.post("/api/v86/workflows/{group_id}/rollback")
def rollback_workflow_group(group_id: str, request: WorkflowRollbackRequest) -> dict:
    """Restore staged originals for an explicitly confirmed failed workflow.

    Running/pending/succeeded groups are never restored through this endpoint;
    the user must first let the workflow finish or cancel it.
    """
    if request.confirm != "ROLLBACK":
        raise HTTPException(400, "Type ROLLBACK to confirm")
    details = workflow_details(group_id)
    if not details:
        raise HTTPException(404, "Workflow group not found")
    group = details["group"]
    if group.get("status") not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled workflows can be rolled back")
    if details.get("locks"):
        raise HTTPException(409, "Workflow still owns an active resource lock")
    if not details.get("artifacts"):
        raise HTTPException(409, "Workflow has no staged original to restore")
    try:
        result = rollback_workflow(group_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    logger.warning("workflow event=rollback_requested group=%s restored=%s", group_id, result.get("restored", 0))
    return {"ok": True, **result, "workflow": workflow_details(group_id)}


@app.post("/api/v86/language-region-use")
def record_language_region_use(request: LanguageRegionUse) -> dict:
    value = request.value.strip()
    with connection() as db:
        db.execute(
            """INSERT INTO language_region_selection_usage(value, use_count)
               VALUES (?, 1)
               ON CONFLICT(value) DO UPDATE SET use_count = language_region_selection_usage.use_count + 1""",
            (value,),
        )
        count = db.execute(
            "SELECT use_count FROM language_region_selection_usage WHERE value=?",
            (value,),
        ).fetchone()["use_count"]
    return {"value": value, "use_count": count}


@app.get("/api/v86/dashboard/stats")
def dashboard_stats() -> dict:
    """Return compact, indexed collection statistics for the dashboard."""
    with connection() as db:
        counts = db.execute("""
            SELECT
              (SELECT count(*) FROM plex_media WHERE kind='movie') AS movies,
              (SELECT count(*) FROM plex_media WHERE kind='episode') AS episodes,
              (SELECT count(DISTINCT show_title) FROM plex_media WHERE kind='episode' AND show_title IS NOT NULL AND show_title!='') AS shows,
              (SELECT coalesce(sum(size),0) FROM plex_media WHERE kind='movie') AS movie_bytes,
              (SELECT coalesce(sum(size),0) FROM plex_media WHERE kind='episode') AS episode_bytes,
              (SELECT count(DISTINCT path) FROM media_stream_index) AS indexed_media,
              (SELECT count(*) FROM plex_media) AS catalog_media,
              (SELECT count(DISTINCT i.path) FROM media_stream_index i JOIN plex_media p ON p.path=i.path WHERE p.kind='movie') AS indexed_movies,
              (SELECT count(DISTINCT i.path) FROM media_stream_index i JOIN plex_media p ON p.path=i.path WHERE p.kind='episode') AS indexed_episodes
        """).fetchone()
        language_rows = db.execute("""
            SELECT p.kind, CASE WHEN i.stream_type='external' THEN 'subtitle' ELSE i.stream_type END AS stream_type,
                   lower(trim(i.language)) AS language, upper(trim(i.region)) AS region, count(DISTINCT i.path) AS media_count
              FROM media_stream_index i JOIN plex_media p ON p.path=i.path
             WHERE i.stream_type IN ('audio','subtitle','external') AND trim(coalesce(i.language,''))!=''
             GROUP BY p.kind, CASE WHEN i.stream_type='external' THEN 'subtitle' ELSE i.stream_type END, lower(trim(i.language)),upper(trim(i.region))
             ORDER BY p.kind, stream_type, media_count DESC, language
        """).fetchall()
        libraries = db.execute("""
            SELECT library_name, kind, count(*) AS media_count
              FROM plex_media GROUP BY library_name, kind ORDER BY media_count DESC, library_name
        """).fetchall()
        coverage = db.execute("""SELECT kind,embedded,external,count(*) AS media_count FROM (
            SELECT p.kind,p.path,
              max(CASE WHEN i.stream_type='subtitle' THEN 1 ELSE 0 END) AS embedded,
              max(CASE WHEN i.stream_type='external' THEN 1 ELSE 0 END) AS external
            FROM plex_media p JOIN media_stream_index_state s ON s.path=p.path
            LEFT JOIN media_stream_index i ON i.path=p.path
            WHERE p.kind IN ('movie','episode') GROUP BY p.kind,p.path
            ) flags GROUP BY kind,embedded,external""").fetchall()
    distributions = {'movies': {'audio': [], 'subtitle': []}, 'tv': {'audio': [], 'subtitle': []}}
    for row in language_rows:
        target = 'movies' if row['kind'] == 'movie' else 'tv'
        distributions[target][row['stream_type']].append({'language': row['language'] + ('-' + row['region'] if row['region'] else ''), 'media_count': row['media_count']})
    return {
        'counts': {key: counts[key] for key in ('movies','shows','episodes','movie_bytes','episode_bytes','indexed_media','catalog_media','indexed_movies','indexed_episodes')},
        'languages': distributions,
        'libraries': [dict(row) for row in libraries],
        'subtitle_coverage': [dict(row) for row in coverage],
    }


class FinalVersionRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    final_version: bool


def _final_note_key(path: str) -> tuple[str, str] | None:
    return final_version_entity_for_path(path)


@app.get("/api/v86/final-version")
def get_final_version(path: str) -> dict:
    entity = _final_note_key(path)
    if not entity:
        raise HTTPException(404, "Media is not indexed")
    with connection() as db:
        row = db.execute("SELECT final_version,reviewed FROM media_notes WHERE entity_type=? AND entity_key=?", entity).fetchone()
        parent_final = False
        if entity[0] == "tv" and entity[1].startswith("episode:"):
            media = db.execute("SELECT library_key,show_title FROM plex_media WHERE path=?", (entity[1][len("episode:"):],)).fetchone()
            if media:
                parent_key = f"{media['library_key']}:{media['show_title'] or 'Unknown show'}"
                parent = db.execute("SELECT final_version FROM media_notes WHERE entity_type='tv' AND entity_key=?", (parent_key,)).fetchone()
                parent_final = bool(parent and parent["final_version"])
    final = bool(row and row["final_version"])
    return {"path": str(path), "final_version": final, "parent_final_version": parent_final,
            "effective_final_version": final or parent_final, "reviewed": bool(row and row["reviewed"]),
            "entity_type": entity[0], "entity_key": entity[1]}


@app.put("/api/v86/final-version")
def set_final_version(request: FinalVersionRequest) -> dict:
    path = str(request.path)
    assert_no_tv_draft_save(path)
    entity = _final_note_key(path)
    if not entity:
        raise HTTPException(404, "Media is not indexed")
    with connection() as db:
        current = db.execute("SELECT note,reviewed,plex_sync_change FROM media_notes WHERE entity_type=? AND entity_key=?", entity).fetchone()
        note = str(current["note"] or "") if current else ""
        reviewed = bool(current["reviewed"]) if current else False
        plex_change = bool(current["plex_sync_change"]) if current else False
        final_value = bool(request.final_version)
        reviewed = reviewed or final_value
        if note or reviewed or plex_change or final_value:
            db.execute("INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(entity_type,entity_key) DO UPDATE SET note=excluded.note,reviewed=excluded.reviewed,plex_sync_change=excluded.plex_sync_change,final_version=excluded.final_version,updated_at=CURRENT_TIMESTAMP", (entity[0],entity[1],note,int(reviewed),int(plex_change),int(final_value)))
        else:
            db.execute("DELETE FROM media_notes WHERE entity_type=? AND entity_key=?", entity)
        media_path = entity[1][len("episode:"):] if entity[0] == "tv" and entity[1].startswith("episode:") else entity[1]
        media = db.execute("SELECT kind,library_key,show_title FROM plex_media WHERE path=?", (media_path,)).fetchone()
        parent_result = None
        if media and media["kind"] == "episode":
            parent_key = f"{media['library_key']}:{media['show_title'] or 'Unknown show'}"
            total = int(db.execute("SELECT count(*) AS n FROM plex_media WHERE kind='episode' AND library_key=? AND show_title=?", (media["library_key"],media["show_title"])).fetchone()["n"] or 0)
            final_count = int(db.execute("SELECT count(*) AS n FROM media_notes n JOIN plex_media p ON p.path=substr(n.entity_key,9) WHERE n.entity_type='tv' AND n.final_version=1 AND n.entity_key LIKE 'episode:%' AND p.kind='episode' AND p.library_key=? AND p.show_title=?", (media["library_key"],media["show_title"])).fetchone()["n"] or 0)
            all_final = total > 0 and final_count >= total
            parent = db.execute("SELECT note,reviewed,plex_sync_change FROM media_notes WHERE entity_type='tv' AND entity_key=?", (parent_key,)).fetchone()
            if all_final:
                pnote=str(parent["note"] or "") if parent else ""; prev=bool(parent["reviewed"]) if parent else False; pplex=bool(parent["plex_sync_change"]) if parent else False
                db.execute("INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) VALUES('tv',?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(entity_type,entity_key) DO UPDATE SET reviewed=1,final_version=1,updated_at=CURRENT_TIMESTAMP", (parent_key,pnote,1,int(pplex),1))
            elif not final_value:
                db.execute("UPDATE media_notes SET final_version=0,updated_at=CURRENT_TIMESTAMP WHERE entity_type='tv' AND entity_key=?", (parent_key,))
            parent_result = {"entity_key":parent_key,"final_version":all_final}
    if request.final_version:
        from app.detection_policy import retire_final_detection
        with connection() as db: retire_final_detection(db)
        from app.subtitle_cache import prioritize_final_media
        prioritize_final_media([media_path])
    logger.info("change=final_version_saved path=%s final=%s", path.replace("\n"," ")[:300], bool(request.final_version))
    return {"path":path,"final_version":bool(request.final_version),"reviewed":reviewed,"entity_type":entity[0],"entity_key":entity[1],"parent":parent_result}


class FinalVersionShowRequest(BaseModel):
    entity_key: str = Field(min_length=1, max_length=4096)
    final_version: bool


@app.put("/api/v86/final-version/show")
def set_show_final_version(request: FinalVersionShowRequest) -> dict:
    """Toggle a show's final lock for every episode as one atomic operation."""
    entity_key = str(request.entity_key)
    if not entity_key.startswith("tv:"):
        # The listing id is normally ``library_key:show title``.  Accepting
        # the bare key keeps this endpoint independent of presentation labels.
        entity_key = "tv:" + entity_key
    raw_key = entity_key[3:]
    if ":" not in raw_key:
        raise HTTPException(400, "Invalid TV show key")
    library_key, show_title = raw_key.split(":", 1)
    with connection() as db:
        episodes = db.execute(
            "SELECT path FROM plex_media WHERE kind='episode' AND library_key=? AND show_title=?",
            (library_key, show_title),
        ).fetchall()
        if not episodes:
            raise HTTPException(404, "TV show is not indexed")
        for row in episodes:
            episode_key = "episode:" + str(row["path"])
            current = db.execute(
                "SELECT note,reviewed,plex_sync_change FROM media_notes WHERE entity_type='tv' AND entity_key=?",
                (episode_key,),
            ).fetchone()
            note = str(current["note"] or "") if current else ""
            reviewed = bool(current["reviewed"]) if current else False
            plex_change = bool(current["plex_sync_change"]) if current else False
            if note or reviewed or plex_change or request.final_version:
                db.execute(
                    "INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) VALUES('tv',?,?,?,?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(entity_type,entity_key) DO UPDATE SET note=excluded.note,reviewed=excluded.reviewed,plex_sync_change=excluded.plex_sync_change,final_version=excluded.final_version,updated_at=CURRENT_TIMESTAMP",
                    (episode_key, note, int(reviewed or request.final_version), int(plex_change), int(request.final_version)),
                )
            else:
                db.execute("DELETE FROM media_notes WHERE entity_type='tv' AND entity_key=?", (episode_key,))
        parent = db.execute(
            "SELECT note,reviewed,plex_sync_change FROM media_notes WHERE entity_type='tv' AND entity_key=?",
            (raw_key,),
        ).fetchone()
        pnote = str(parent["note"] or "") if parent else ""
        previewed = bool(parent["reviewed"]) if parent else False
        pplex = bool(parent["plex_sync_change"]) if parent else False
        if pnote or previewed or pplex or request.final_version:
            db.execute(
                "INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) VALUES('tv',?,?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(entity_type,entity_key) DO UPDATE SET note=excluded.note,reviewed=excluded.reviewed,plex_sync_change=excluded.plex_sync_change,final_version=excluded.final_version,updated_at=CURRENT_TIMESTAMP",
                (raw_key, pnote, int(previewed or request.final_version), int(pplex), int(request.final_version)),
            )
        elif not request.final_version:
            db.execute("DELETE FROM media_notes WHERE entity_type='tv' AND entity_key=?", (raw_key,))
    if request.final_version:
        from app.detection_policy import retire_final_detection
        with connection() as db: retire_final_detection(db)
        from app.subtitle_cache import prioritize_final_media
        prioritize_final_media([str(row["path"]) for row in episodes])
    from app.v19 import _listing_cache
    _listing_cache.pop('tv-summary', None)
    logger.info("change=show_final_version_saved show=%s final=%s episodes=%s", raw_key.replace("\n", " ")[:300], bool(request.final_version), len(episodes))
    return {"entity_key": raw_key, "final_version": bool(request.final_version), "episodes": len(episodes), "reviewed": bool(request.final_version)}


@app.get("/api/v86/notes")
def list_media_notes(entity_type: Literal["movie", "tv"] | None = None) -> dict:
    with connection() as db:
        query = "SELECT entity_type,entity_key,note,reviewed,plex_sync_change,final_version FROM media_notes WHERE (note!='' OR reviewed=1 OR plex_sync_change=1 OR final_version=1)"
        args = []
        if entity_type:
            query += " AND entity_type=?"; args.append(entity_type)
        rows = db.execute(query, args).fetchall()
    return {"items": {f"{row['entity_type']}:{row['entity_key']}": {"note": row["note"], "reviewed": bool(row["reviewed"]), "plex_sync_change": bool(row["plex_sync_change"]), "final_version": bool(row["final_version"])} for row in rows}, "by_key": {row["entity_key"]: {"note": row["note"], "reviewed": bool(row["reviewed"]), "plex_sync_change": bool(row["plex_sync_change"]), "final_version": bool(row["final_version"])} for row in rows}}


@app.get("/api/v86/note")
def get_media_note(entity_type: Literal["movie", "tv"], entity_key: str) -> dict:
    with connection() as db:
        row = db.execute("SELECT note,reviewed,plex_sync_change,final_version FROM media_notes WHERE entity_type=? AND entity_key=?", (entity_type, entity_key)).fetchone()
    return {"entity_type": entity_type, "entity_key": entity_key, "note": row["note"] if row else "", "reviewed": bool(row["reviewed"]) if row else False, "plex_sync_change": bool(row["plex_sync_change"]) if row else False, "final_version": bool(row["final_version"]) if row else False}


@app.put("/api/v86/note")
def save_media_note(request: MediaNoteRequest) -> dict:
    note = request.note.strip()
    if request.entity_type=='tv':
        if request.entity_key.startswith('episode:'):
            assert_no_tv_draft_save(request.entity_key[len('episode:'):])
        else:
            with connection() as db:
                pending = db.execute("SELECT 1 FROM tv_edit_sessions WHERE show_id=? AND status='committing' LIMIT 1", (request.entity_key,)).fetchone()
            if pending: raise HTTPException(423, 'This TV-show draft is being saved. Notes and review changes are locked until its task completes.')
    with connection() as db:
        current = db.execute("SELECT reviewed,plex_sync_change,final_version FROM media_notes WHERE entity_type=? AND entity_key=?", (request.entity_type, request.entity_key)).fetchone()
        was_final = bool(current["final_version"]) if current else False
        reviewed = bool(request.reviewed) if request.reviewed is not None else bool(current["reviewed"]) if current else False
        plex_sync_change = bool(request.plex_sync_change) if request.plex_sync_change is not None else bool(current["plex_sync_change"]) if current else False
        final_version = bool(request.final_version) if request.final_version is not None else bool(current["final_version"]) if current else False
        if note or reviewed or plex_sync_change or final_version:
            db.execute("INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(entity_type,entity_key) DO UPDATE SET note=excluded.note,reviewed=excluded.reviewed,plex_sync_change=excluded.plex_sync_change,final_version=excluded.final_version,updated_at=CURRENT_TIMESTAMP", (request.entity_type, request.entity_key, note, int(reviewed), int(plex_sync_change), int(final_version)))
        else:
            db.execute("DELETE FROM media_notes WHERE entity_type=? AND entity_key=?", (request.entity_type, request.entity_key))
    if final_version and not was_final:
        from app.subtitle_cache import prioritize_final_media
        if request.entity_type == "movie":
            prioritize_final_media([request.entity_key])
        elif request.entity_key.startswith("episode:"):
            prioritize_final_media([request.entity_key[len("episode:"):]])
        elif ":" in request.entity_key:
            library_key, show_title = request.entity_key.split(":", 1)
            with connection() as db:
                episodes = db.execute(
                    "SELECT path FROM plex_media WHERE kind='episode' AND library_key=? AND show_title=?",
                    (library_key, show_title),
                ).fetchall()
            prioritize_final_media([str(row["path"]) for row in episodes])
    logger.info("change=media_note_saved type=%s key=%s present=%s reviewed=%s plex_sync_change=%s final_version=%s", request.entity_type, request.entity_key.replace("\n", " ")[:200], bool(note), reviewed, plex_sync_change, final_version)
    return {"entity_type": request.entity_type, "entity_key": request.entity_key, "note": note, "reviewed": reviewed, "plex_sync_change": plex_sync_change, "final_version": final_version}

@app.on_event("startup")
def initialize_postgres_workflow() -> None:
    """Create the durable staged-workflow tables when PostgreSQL is active."""
    import os
    if os.getenv("DATABASE_BACKEND", "sqlite").lower() != "postgres":
        return
    # Local workers start after schema/recovery. Another process can still hold
    # a relation lock, so retain a bounded retry for database deadlocks.
    import time
    for attempt in range(5):
        try:
            initialize_postgres_workflow_schema()
            cleaned_locks = cleanup_terminal_luw_locks()
            if cleaned_locks:
                logger.warning("luw event=terminal_locks_cleaned count=%d", cleaned_locks)
            recovered = recover_luws()
            if recovered:
                logger.warning("luw event=recovered_after_restart count=%d", recovered)
            break
        except Exception as exc:
            if "deadlock detected" not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))


def order_startup_hooks() -> None:
    """Finish schema/recovery before any queue or scheduler starts claiming."""
    from app import db_bootstrap
    if not db_bootstrap.configured():
        # A new installation only serves the database wizard. No operational
        # schema, temporary SQLite catalog or workers are needed before setup.
        app.router.on_startup.clear()
        app.router.on_shutdown.clear()
        return
    hooks = app.router.on_startup
    services = {'initialize_preflight_dispatcher', 'start_v82_services', 'initialize_backup'}
    schema_names = ('initialize_postgres_workflow', 'initialize_unified_stream_index')
    schema = [hook for name in schema_names for hook in hooks if hook.__name__ == name]
    setup = [hook for hook in hooks if hook.__name__ not in services and hook not in schema]
    workers = [hook for hook in hooks if hook.__name__ in services]
    hooks[:] = schema + setup + workers
    shutdown = app.router.on_shutdown
    monitors = [hook for hook in shutdown if hook.__name__ == 'shutdown_performance_monitor']
    shutdown[:] = monitors + [hook for hook in shutdown if hook not in monitors]


order_startup_hooks()
