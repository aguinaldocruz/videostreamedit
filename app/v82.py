from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from fastapi import HTTPException

import app.v54 as indexes
import app.v65 as tasks
import app.v79 as tv_bulk
import app.v80 as queues
from app.v5 import external_subtitles, split_tag
from app.v11 import connection
from app.v13 import media_details_with_ietf
from app.v43 import optimized_media_edit
from app.v7 import ReorderEditRequest
from app.v81 import app


logger = logging.getLogger("uvicorn.error")


def ensure_unified_index() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS media_stream_index (
                path TEXT NOT NULL, source TEXT NOT NULL, stream_type TEXT NOT NULL,
                type_index INTEGER NOT NULL, external_path TEXT NOT NULL DEFAULT '',
                codec TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '', track_name TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0, is_forced INTEGER NOT NULL DEFAULT 0,
                filename_tags TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY(path,source,stream_type,type_index,external_path)
            );
            CREATE INDEX IF NOT EXISTS media_stream_index_filters
                ON media_stream_index(stream_type,language,region,track_name,path);
            CREATE INDEX IF NOT EXISTS media_stream_index_path ON media_stream_index(path);
            CREATE TABLE IF NOT EXISTS media_stream_index_state (
                path TEXT PRIMARY KEY, modified_ns INTEGER NOT NULL, size INTEGER NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1, indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS performance_metric (
                name TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0,
                total REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)


def fast_streams(path: Path) -> list[dict]:
    streams: list[dict] = []
    if path.suffix.lower() in {".mkv", ".mka", ".mks", ".mk3d"}:
        try:
            result = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True, timeout=90, check=True)
            counters = {"audio": 0, "subtitle": 0}
            for track in json.loads(result.stdout).get("tracks", []):
                kind = "subtitle" if track.get("type") == "subtitles" else track.get("type")
                if kind not in counters:
                    continue
                properties = track.get("properties") or {}
                language, region = split_tag(str(properties.get("language_ietf") or properties.get("language") or ""))
                if language == "por":
                    language, region = "pt", region or "BR"
                elif language == "pt":
                    region = region or "BR"
                elif language == "eng":
                    language = "en"
                streams.append({"source": "embedded", "stream_type": kind, "type_index": counters[kind], "external_path": "", "codec": str(track.get("codec") or properties.get("codec_id") or "unknown"), "language": language, "region": region, "track_name": str(properties.get("track_name") or ""), "default": bool(properties.get("default_track")), "forced": bool(properties.get("forced_track")), "filename_tags": []})
                counters[kind] += 1
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            streams = []
    if not streams:
        details = media_details_with_ietf(str(path))
        streams = [{"source": "embedded", "stream_type": item["codec_type"], "type_index": int(item["type_index"]), "external_path": "", "codec": str(item.get("codec") or "unknown"), "language": str(item.get("language") or ""), "region": str(item.get("region") or ""), "track_name": str(item.get("title") or ""), "default": bool(item.get("default")), "forced": bool(item.get("forced")), "filename_tags": []} for item in details.get("streams", [])]
    for item in external_subtitles(path):
        streams.append({"source": "external", "stream_type": "external", "type_index": -1, "external_path": str(item["path"]), "codec": str(item.get("codec") or ""), "language": str(item.get("language") or ""), "region": str(item.get("region") or ""), "track_name": str(item.get("title") or ""), "default": False, "forced": bool(item.get("forced")), "filename_tags": item.get("filename_tags") or []})
    return streams


def unified_core_index(item: dict) -> None:
    ensure_unified_index()
    path = Path(item["path"])
    values = fast_streams(path)
    stat = path.stat()
    with connection() as db:
        kind = db.execute("SELECT kind FROM plex_media WHERE path=?", (str(path),)).fetchone()
        db.execute("DELETE FROM media_stream_index WHERE path=?", (str(path),))
        db.executemany("INSERT INTO media_stream_index(path,source,stream_type,type_index,external_path,codec,language,region,track_name,is_default,is_forced,filename_tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [(str(path), value["source"], value["stream_type"], value["type_index"], value["external_path"], value["codec"], value["language"], value["region"], value["track_name"], int(value["default"]), int(value["forced"]), json.dumps(value["filename_tags"], ensure_ascii=False)) for value in values])
        db.execute("INSERT OR REPLACE INTO media_stream_index_state(path,modified_ns,size,schema_version,indexed_at) VALUES(?,?,?,1,CURRENT_TIMESTAMP)", (str(path), stat.st_mtime_ns, stat.st_size))
        if kind and kind["kind"] == "movie":
            db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (str(path),))
            db.executemany("INSERT INTO movie_stream_index_value(path,stream_type,language,track_name) VALUES(?,?,?,?)", [(str(path), "subtitle" if value["stream_type"] == "external" else value["stream_type"], value["language"], value["track_name"]) for value in values])
            db.execute("INSERT OR REPLACE INTO movie_stream_index(path,modified,size,indexed_at) VALUES(?,?,?,CURRENT_TIMESTAMP)", (str(path), int(stat.st_mtime), stat.st_size))
        elif kind:
            db.execute("DELETE FROM tv_stream_index_value WHERE path=?", (str(path),))
            db.executemany("INSERT INTO tv_stream_index_value(path,stream_type,language,region,track_name) VALUES(?,?,?,?,?)", [(str(path), value["stream_type"], value["language"], value["region"], value["track_name"]) for value in values])
            db.execute("INSERT OR REPLACE INTO tv_stream_index_media(path,modified,size,indexed_at) VALUES(?,?,?,CURRENT_TIMESTAMP)", (str(path), int(stat.st_mtime), stat.st_size))
    tv_bulk.persist_external_sidecars("core", str(path))


indexes.processors["core"] = unified_core_index


@app.on_event("startup")
def initialize_unified_stream_index() -> None:
    ensure_unified_index()
    with connection() as db:
        migrated = db.execute("SELECT 1 FROM feature_migrations WHERE name='unified_stream_index_v1'").fetchone()
        if not migrated:
            db.execute("DELETE FROM media_stream_index")
            db.execute("DELETE FROM media_stream_index_state")
            db.execute("DELETE FROM index_task_queue WHERE job='core' AND status IN ('pending','running','succeeded','cancelled')")
            db.execute("""INSERT INTO index_task_queue(job,path,reason,status,created_at,updated_at)
                SELECT 'core',path,'Unified stream index migration','pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM plex_media""")
            db.execute("INSERT INTO feature_migrations(name) VALUES('unified_stream_index_v1')")
            logger.info("core_index event=unified_migration_queued")


def selected_movies(paths: list[str]) -> list[dict]:
    unique = list(dict.fromkeys(paths))
    found = []
    with connection() as db:
        for start in range(0, len(unique), 800):
            group = unique[start:start + 800]
            rows = db.execute(f"SELECT path,title FROM plex_media WHERE kind='movie' AND path IN ({','.join('?' for _ in group)})", group).fetchall()
            found.extend(dict(row) for row in rows)
    if {row["path"] for row in found} != set(unique):
        raise HTTPException(409, "The movie selection changed. Refresh Movies and try again")
    return found


class MovieStreamRequest(tv_bulk.SeasonStreamRequest):
    paths: list[str] = tv_bulk.Field(min_length=1, max_length=30000)


@app.post("/api/v82/movies/stream-values")
def movie_stream_values(request: MovieStreamRequest) -> dict:
    items = selected_movies(request.paths)
    values = []
    with connection() as db:
        for start in range(0, len(items), 800):
            group = [item["path"] for item in items[start:start + 800]]
            rows = db.execute(f"SELECT path,stream_type,language,region,track_name,filename_tags FROM media_stream_index WHERE path IN ({','.join('?' for _ in group)})", group).fetchall()
            for row in rows:
                value = dict(row)
                try:
                    value["filename_tags"] = json.loads(value["filename_tags"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    value["filename_tags"] = []
                values.append(value)
    return {"values": values, "movies": len(items), "errors": []}


class MovieStreamBulkEdit(tv_bulk.SeasonStreamBulkEdit):
    paths: list[str] = tv_bulk.Field(min_length=1, max_length=30000)


class MovieBulkTaskStatus(tv_bulk.BaseModel):
    task_ids: list[int] = tv_bulk.Field(min_length=1, max_length=30000)


def process_filtered_stream_edit(task_id: int, payload: dict) -> dict:
    path = str(payload["path"])
    request_data = dict(payload["request"])
    request_data["paths"] = [path]
    request_data["mode"] = "now"
    request = tv_bulk.SeasonStreamBulkEdit.model_validate(request_data)
    tasks.update_progress(task_id, 0, 2, "Checking current streams")
    edit, matched = tv_bulk.episode_bulk_edit(path, request)
    if not matched:
        raise RuntimeError("No current stream matches the queued filter")
    tasks.update_progress(task_id, 1, 2, f"Applying changes to {matched} matching stream(s)")
    result = optimized_media_edit(ReorderEditRequest.model_validate(edit))
    reindex = ["core", "previews"] if request.filters.stream_type == "external" or request.remove else ["core"]
    queues.request_media_indexes(path, reindex, "Queued filtered movie edit completed")
    tasks.update_progress(task_id, 2, 2, "Stream changes applied")
    return {**result, "path": path, "streams": matched}


def enqueue_filtered_movie_edits(paths: list[str], request: MovieStreamBulkEdit) -> tuple[int, list[int]]:
    template = request.model_dump(exclude={"paths", "mode"})
    now = tasks.utc_now()
    queued = 0
    task_ids: list[int] = []
    with connection() as db:
        for path in paths:
            payload = json.dumps({"path": path, "request": template}, ensure_ascii=False, separators=(",", ":"))
            cursor = db.execute(
                "INSERT INTO task_queue(task_type,label,payload_json,status,progress_message,created_at,updated_at) VALUES('filtered_stream_edit',?,?,'pending','Waiting',?,?)",
                (f"Movie filtered stream edit · {Path(path).name}", payload, now, now),
            )
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (cursor.lastrowid, path, now))
            task_ids.append(int(cursor.lastrowid))
            queued += 1
    tasks.wake_queue()
    logger.info("movie_stream_bulk_edit event=batch_queued media=%d", queued)
    return queued, task_ids


tasks.TASK_HANDLERS["filtered_stream_edit"] = process_filtered_stream_edit


@app.post("/api/v82/movies/stream-bulk-edit")
def movie_stream_bulk_edit(request: MovieStreamBulkEdit) -> dict:
    request.paths = list(dict.fromkeys(request.paths))
    selected_movies(request.paths)
    changed = set(request.changed_fields)
    if request.filters.stream_type == "external":
        if request.integrate and request.remove:
            raise HTTPException(400, "External subtitles cannot be integrated and removed together")
        if changed.intersection({"language", "region", "track_name"}) and not request.integrate:
            raise HTTPException(400, "Integrate must be selected to save external subtitle properties")
        if not request.integrate and not request.remove:
            raise HTTPException(400, "Select Integrate or Remove")
    elif request.integrate:
        raise HTTPException(400, "Integrate is available only for external subtitles")
    if request.remove and changed.intersection({"language", "region", "track_name"}):
        raise HTTPException(400, "Remove cannot be combined with metadata changes")
    if request.mode == "queue":
        queued, task_ids = enqueue_filtered_movie_edits(request.paths, request)
        return {"mode": "queue", "queued": queued, "task_ids": task_ids, "applied": 0, "streams": 0, "skipped": [], "failed": []}
    queued = applied = streams = 0
    skipped, failed = [], []
    for path in request.paths:
        try:
            edit, matched = tv_bulk.episode_bulk_edit(path, request)
            if not matched:
                skipped.append({"path": path, "reason": "No current stream matches"})
                continue
            reindex = ["core", "previews"] if request.filters.stream_type == "external" or request.remove else ["core"]
            if request.mode == "queue":
                tasks.enqueue("media_edit", {"edit": edit, "reindex_indexes": reindex}, f"Movie filtered stream edit · {Path(path).name}")
                queued += 1
            else:
                optimized_media_edit(ReorderEditRequest.model_validate(edit))
                queues.request_media_indexes(path, reindex, "Movie filtered stream edit completed")
                applied += 1
            streams += matched
        except Exception as exc:
            failed.append({"path": path, "error": str(getattr(exc, "detail", exc))[-1000:]})
    if not queued and not applied:
        raise HTTPException(409, failed[0]["error"] if failed else "No movie streams match the selected values")
    logger.info("movie_stream_bulk_edit event=completed mode=%s media=%d streams=%d skipped=%d failed=%d", request.mode, queued + applied, streams, len(skipped), len(failed))
    return {"mode": request.mode, "queued": queued, "applied": applied, "streams": streams, "skipped": skipped, "failed": failed}


@app.post("/api/v82/movies/bulk-task-status")
def movie_bulk_task_status(request: MovieBulkTaskStatus) -> dict:
    counts: dict[str, int] = {}
    with connection() as db:
        for start in range(0, len(request.task_ids), 800):
            group = request.task_ids[start:start + 800]
            rows = db.execute(
                f"SELECT status,count(*) amount FROM task_queue WHERE id IN ({','.join('?' for _ in group)}) GROUP BY status",
                group,
            ).fetchall()
            for row in rows:
                counts[row["status"]] = counts.get(row["status"], 0) + row["amount"]
    active = counts.get("pending", 0) + counts.get("running", 0)
    return {"counts": counts, "active": active, "finished": active == 0}


def unified_pending_index_items(job: str) -> list[dict]:
    """Use direct filesystem fingerprints; Plex timestamps are catalog hints only."""
    if job != "core":
        return []
    with connection() as db:
        rows = [dict(row) for row in db.execute(
            """SELECT p.path,p.title,s.modified_ns,s.size AS indexed_size,s.schema_version
               FROM plex_media p LEFT JOIN media_stream_index_state s ON s.path=p.path
               ORDER BY p.path"""
        )]
    pending: dict[str, dict] = {}
    for item in rows:
        try:
            stat = Path(item["path"]).stat()
            if item["modified_ns"] != stat.st_mtime_ns or item["indexed_size"] != stat.st_size or item["schema_version"] != 1:
                pending[item["path"]] = {"path": item["path"], "title": item["title"], "modified": int(stat.st_mtime), "size": stat.st_size}
        except OSError:
            pending[item["path"]] = {"path": item["path"], "title": item["title"], "modified": 0, "size": 0}
    for item in tv_bulk.pending_external_sidecars("core"):
        pending[str(item["path"])] = item
    logger.info("core_index event=fingerprint_check catalog=%d pending=%d", len(rows), len(pending))
    return list(pending.values())


def clear_unified_index(job: str) -> None:
    if job == "core":
        with connection() as db:
            db.execute("DELETE FROM media_stream_index")
            db.execute("DELETE FROM media_stream_index_state")
    _original_clear_index(job)


def prune_queue_history() -> dict:
    """Bound finished queue history while retaining recent diagnostics."""
    with connection() as db:
        generic = db.execute(
            """DELETE FROM task_queue WHERE status IN ('succeeded','cancelled') AND id NOT IN
               (SELECT id FROM task_queue WHERE status IN ('succeeded','cancelled') ORDER BY id DESC LIMIT 2000)"""
        ).rowcount
        indexed = db.execute(
            """DELETE FROM index_task_queue WHERE status IN ('succeeded','cancelled') AND id NOT IN
               (SELECT id FROM index_task_queue WHERE status IN ('succeeded','cancelled') ORDER BY id DESC LIMIT 5000)"""
        ).rowcount
    if generic or indexed:
        logger.info("queue_retention event=pruned generic=%d index=%d", generic, indexed)
    return {"generic": generic, "index": indexed}


_original_clear_index = queues.clear_index
queues.pending_index_items = unified_pending_index_items
indexes.pending = lambda job: unified_pending_index_items(job)
indexes.start = lambda job: queues.enqueue_many(job, unified_pending_index_items(job), "Scheduled filesystem fingerprint check")
queues.clear_index = clear_unified_index


@app.on_event("startup")
def start_v82_services() -> None:
    prune_queue_history()
    import app.v81 as performance
    performance.initialize_performance_release()


@app.post("/api/v82/setup/queues/prune")
def prune_finished_queue_history() -> dict:
    with connection() as db:
        generic = db.execute("DELETE FROM task_queue WHERE status IN ('succeeded','cancelled')").rowcount
        indexed = db.execute("DELETE FROM index_task_queue WHERE status IN ('succeeded','cancelled')").rowcount
    logger.info("queue_retention event=finished_history_cleared generic=%d index=%d", generic, indexed)
    return {"generic": generic, "index": indexed}
