import logging
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.postgres_store import (
    initialize_schema as initialize_postgres_workflow_schema,
)
from app.postgres_store import (
    luw_details,
    luw_read_model,
    read_model_state,
    read_model_summary,
    recover_luws,
    rollback_workflow,
    workflow_details,
    workflow_read_model,
)
from app.v11 import column_exists, connection
from app.v84 import app

logger = logging.getLogger("videostreamedit")

# Phase 1 durable preflight dispatcher routes and lifecycle hooks.
from app import preflight_dispatcher as _preflight_dispatcher  # noqa: F401,E402


class LanguageRegionUse(BaseModel):
    value: str = Field(min_length=1, max_length=32)


class WorkflowRollbackRequest(BaseModel):
    # Explicit confirmation prevents an accidental media restore from a stale UI.
    confirm: Literal["ROLLBACK"]


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



def final_version_entity_for_path(path: str) -> tuple[str, str] | None:
    with connection() as db:
        row = db.execute("SELECT kind,library_key,show_title FROM plex_media WHERE path=?", (str(path),)).fetchone()
    if not row:
        return None
    if row["kind"] == "movie":
        return ("movie", str(path))
    return ("tv", f"{row['library_key']}:{row['show_title'] or 'Unknown show'}")

def assert_media_editable(path: str) -> None:
    entity = final_version_entity_for_path(path)
    if not entity:
        return
    with connection() as db:
        row = db.execute("SELECT final_version FROM media_notes WHERE entity_type=? AND entity_key=?", entity).fetchone()
    if row and bool(row["final_version"]):
        raise HTTPException(423, "This media is marked Final version and is view-only. Open its notes and unfreeze it before making changes.")


@app.get("/api/v86/workflows/{group_id}")
def get_workflow(group_id: str) -> dict:
    """Read staged group, stage, artifact, and lock state for task review."""
    details = workflow_details(group_id)
    if not details:
        raise HTTPException(404, "Workflow group not found")
    return details


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
    result = rollback_workflow(group_id)
    logger.warning("workflow event=rollback_requested group=%s restored=%s", group_id, result.get("restored", 0))
    return {"ok": True, **result, "workflow": workflow_details(group_id)}


@app.post("/api/v86/language-region-use")
def record_language_region_use(request: LanguageRegionUse) -> dict:
    value = request.value.strip()
    with connection() as db:
        db.execute(
            """INSERT INTO language_region_selection_usage(value, use_count)
               VALUES (?, 1)
               ON CONFLICT(value) DO UPDATE SET use_count = use_count + 1""",
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
            SELECT p.kind, i.stream_type, lower(trim(i.language)) AS language, count(DISTINCT i.path) AS media_count
              FROM media_stream_index i JOIN plex_media p ON p.path=i.path
             WHERE i.stream_type IN ('audio','subtitle') AND trim(coalesce(i.language,''))!=''
             GROUP BY p.kind, i.stream_type, lower(trim(i.language))
             ORDER BY p.kind, i.stream_type, media_count DESC, language
        """).fetchall()
        libraries = db.execute("""
            SELECT library_name, kind, count(*) AS media_count
              FROM plex_media GROUP BY library_name, kind ORDER BY media_count DESC, library_name
        """).fetchall()
    distributions = {'movies': {'audio': [], 'subtitle': []}, 'tv': {'audio': [], 'subtitle': []}}
    for row in language_rows:
        target = 'movies' if row['kind'] == 'movie' else 'tv'
        distributions[target][row['stream_type']].append({'language': row['language'], 'media_count': row['media_count']})
    return {
        'counts': {key: counts[key] for key in ('movies','shows','episodes','movie_bytes','episode_bytes','indexed_media','catalog_media','indexed_movies','indexed_episodes')},
        'languages': distributions,
        'libraries': [dict(row) for row in libraries],
    }


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
    with connection() as db:
        current = db.execute("SELECT reviewed,plex_sync_change,final_version FROM media_notes WHERE entity_type=? AND entity_key=?", (request.entity_type, request.entity_key)).fetchone()
        reviewed = bool(request.reviewed) if request.reviewed is not None else bool(current["reviewed"]) if current else False
        plex_sync_change = bool(request.plex_sync_change) if request.plex_sync_change is not None else bool(current["plex_sync_change"]) if current else False
        final_version = bool(request.final_version) if request.final_version is not None else bool(current["final_version"]) if current else False
        if note or reviewed or plex_sync_change or final_version:
            db.execute("INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(entity_type,entity_key) DO UPDATE SET note=excluded.note,reviewed=excluded.reviewed,plex_sync_change=excluded.plex_sync_change,final_version=excluded.final_version,updated_at=CURRENT_TIMESTAMP", (request.entity_type, request.entity_key, note, int(reviewed), int(plex_sync_change), int(final_version)))
        else:
            db.execute("DELETE FROM media_notes WHERE entity_type=? AND entity_key=?", (request.entity_type, request.entity_key))
    logger.info("change=media_note_saved type=%s key=%s present=%s reviewed=%s plex_sync_change=%s final_version=%s", request.entity_type, request.entity_key.replace("\n", " ")[:200], bool(note), reviewed, plex_sync_change, final_version)
    return {"entity_type": request.entity_type, "entity_key": request.entity_key, "note": note, "reviewed": reviewed, "plex_sync_change": plex_sync_change, "final_version": final_version}

@app.on_event("startup")
def initialize_postgres_workflow() -> None:
    """Create the durable staged-workflow tables when PostgreSQL is active."""
    import os
    if os.getenv("DATABASE_BACKEND", "sqlite").lower() != "postgres":
        return
    # Startup hooks also launch queue workers; retry schema creation briefly
    # if a worker has already taken a PostgreSQL relation lock.
    import time
    for attempt in range(5):
        try:
            initialize_postgres_workflow_schema()
            recovered = recover_luws()
            if recovered:
                logger.warning("luw event=recovered_after_restart count=%d", recovered)
            break
        except Exception as exc:
            if "deadlock detected" not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))
