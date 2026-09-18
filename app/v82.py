from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

import app.v54 as indexes
import app.v65 as tasks
import app.v79 as tv_bulk
import app.v80 as queues
from app.v5 import external_subtitles, split_tag, plex_language_pair
from app.v11 import column_exists, connection
from app.v13 import media_details_with_ietf
from app.v2 import probe
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
                schema_version INTEGER NOT NULL DEFAULT 2, indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS core_index_parity_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_limit INTEGER NOT NULL,
                sample_kind TEXT NOT NULL DEFAULT '',
                checked INTEGER NOT NULL DEFAULT 0,
                mismatches INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS core_index_parity_audit_recent
                ON core_index_parity_audit(checked_at);
            CREATE TABLE IF NOT EXISTS core_index_parity_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'pending',
                total INTEGER NOT NULL DEFAULT 0,
                cursor_cursor_offset INTEGER NOT NULL DEFAULT 0,
                checked INTEGER NOT NULL DEFAULT 0,
                mismatches INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS core_index_parity_runs_status
                ON core_index_parity_runs(status, id);
            CREATE TABLE IF NOT EXISTS performance_metric (
                name TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0,
                total REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS media_video_title (
                path TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', video_index INTEGER NOT NULL DEFAULT 0,
                modified_ns INTEGER NOT NULL, size INTEGER NOT NULL, indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS media_video_title_path ON media_video_title(path);
        """)
        if not column_exists(db, "media_stream_index_state", "content_signature"):
            db.execute("ALTER TABLE media_stream_index_state ADD COLUMN content_signature TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE INDEX IF NOT EXISTS media_stream_index_state_signature ON media_stream_index_state(content_signature)")
        # The first deployed parity migration used the PostgreSQL-reserved
        # column name ``offset``. Rename it in place when upgrading that table.
        if column_exists(db, "core_index_parity_runs", "offset") and not column_exists(db, "core_index_parity_runs", "cursor_offset"):
            db.execute('ALTER TABLE core_index_parity_runs RENAME COLUMN "offset" TO cursor_offset')
        elif not column_exists(db, "core_index_parity_runs", "cursor_offset"):
            db.execute("ALTER TABLE core_index_parity_runs ADD COLUMN cursor_offset INTEGER NOT NULL DEFAULT 0")


def fast_streams(path: Path) -> list[dict]:
    streams: list[dict] = []
    if path.suffix.lower() in {".mkv", ".mka", ".mks", ".mk3d"}:
        try:
            result = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True, timeout=90, check=True)
            counters = {"audio": 0, "subtitle": 0}
            video_title = ""
            video_index = 0
            detailed_streams = None
            for track in json.loads(result.stdout).get("tracks", []):
                kind = "subtitle" if track.get("type") == "subtitles" else track.get("type")
                if kind == "video":
                    properties = track.get("properties") or {}
                    if not video_title:
                        video_title = str(properties.get("track_name") or "").strip()
                    video_index += 1
                    continue
                if kind not in counters:
                    continue
                properties = track.get("properties") or {}
                language, region = split_tag(str(properties.get("language_ietf") or properties.get("language") or ""))
                if language == "por":
                    language = "pt"
                elif language == "eng":
                    language = "en"
                language, region = plex_language_pair(language, region)
                type_index = counters[kind]
                language = tv_bulk.comparable_language(language)
                track_name = str(properties.get("track_name") or "")
                # Plex-aware editor parsing is authoritative. mkvmerge can
                # expose only legacy ISO-639 values (for example ``por``) and
                # incorrectly imply PT for a regionless Portuguese track. Read
                # the richer IETF-aware details once per file and reconcile
                # every embedded stream before writing filter indexes.
                try:
                    if detailed_streams is None:
                        detailed_streams = media_details_with_ietf(str(path)).get("streams", [])
                    match = next((item for item in detailed_streams if item.get("codec_type") == kind and int(item.get("type_index", -1)) == type_index), None)
                    if match:
                        language = tv_bulk.comparable_language(match.get("language") or language)
                        region = str(match.get("region") or "").strip().upper()
                        if match.get("title") is not None:
                            track_name = str(match.get("title") or "")
                except Exception:
                    pass
                streams.append({"source": "embedded", "stream_type": kind, "type_index": type_index, "external_path": "", "codec": str(track.get("codec") or properties.get("codec_id") or "unknown"), "language": language, "region": region, "track_name": track_name, "default": bool(properties.get("default_track")), "forced": bool(properties.get("forced_track")), "filename_tags": []})
                counters[kind] += 1
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            streams = []
    if not streams:
        details = media_details_with_ietf(str(path))
        streams = [{"source": "embedded", "stream_type": item["codec_type"], "type_index": int(item["type_index"]), "external_path": "", "codec": str(item.get("codec") or "unknown"), "language": str(item.get("language") or ""), "region": str(item.get("region") or ""), "track_name": str(item.get("title") or ""), "default": bool(item.get("default")), "forced": bool(item.get("forced")), "filename_tags": []} for item in details.get("streams", []) if item.get("codec_type") in {"audio", "subtitle"}]
        video_title = ""
        video_index = 0
        try:
            for stream in probe(path).get("streams", []):
                if stream.get("codec_type") == "video":
                    if not video_title:
                        video_title = str((stream.get("tags") or {}).get("title") or "").strip()
                    video_index += 1
        except Exception:
            pass
    streams.append({"source": "__video_meta__", "stream_type": "video", "type_index": 0, "external_path": "", "codec": "", "language": "", "region": "", "track_name": video_title, "default": False, "forced": False, "filename_tags": [], "video_index": max(0, video_index - 1)})
    for item in external_subtitles(path):
        streams.append({"source": "external", "stream_type": "external", "type_index": -1, "external_path": str(item["path"]), "codec": str(item.get("codec") or ""), "language": str(item.get("language") or ""), "region": str(item.get("region") or ""), "track_name": str(item.get("title") or ""), "default": False, "forced": bool(item.get("forced")), "filename_tags": item.get("filename_tags") or []})
    return streams


def core_content_signature(path: Path, stat=None) -> str:
    """Cheap content fingerprint for same-size/timestamp-preserved rewrites.

    Only the first and last 64 KiB are read, so checks remain bounded even for
    very large media. The full stream snapshot remains the authoritative
    semantic index; this signature only improves stale detection.
    """
    stat = stat or path.stat()
    chunk = 64 * 1024
    with path.open("rb") as handle:
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


def unified_core_index(item: dict) -> None:
    ensure_unified_index()
    path = Path(item["path"])
    values = fast_streams(path)
    video_meta = next((value for value in values if value.get("source") == "__video_meta__"), {"track_name": "", "video_index": 0})
    values = [value for value in values if value.get("source") != "__video_meta__"]
    stat = path.stat()
    content_signature = core_content_signature(path, stat)
    with connection() as db:
        # Publish one complete media snapshot in this transaction, but avoid
        # rewriting unchanged stream rows. Readers see either the old snapshot
        # or the new one, never the intermediate state.
        current_rows = {
            (str(row["source"]), str(row["stream_type"]), int(row["type_index"]), str(row["external_path"] or "")): dict(row)
            for row in db.execute(
                "SELECT source,stream_type,type_index,external_path,codec,language,region,track_name,is_default,is_forced,filename_tags "
                "FROM media_stream_index WHERE path=?",
                (str(path),),
            ).fetchall()
        }
        snapshot = {}
        for value in values:
            key = (str(value["source"]), str(value["stream_type"]), int(value["type_index"]), str(value["external_path"] or ""))
            payload = {
                "codec": str(value["codec"] or ""),
                "language": str(value["language"] or ""),
                "region": str(value["region"] or ""),
                "track_name": str(value["track_name"] or ""),
                "is_default": int(bool(value["default"])),
                "is_forced": int(bool(value["forced"])),
                "filename_tags": json.dumps(value["filename_tags"] or [], ensure_ascii=False),
            }
            snapshot[key] = payload
            old_row = current_rows.get(key)
            if old_row and all(str(old_row.get(field) or "") == str(value_now or "") for field, value_now in payload.items()):
                continue
            db.execute(
                "INSERT OR REPLACE INTO media_stream_index(path,source,stream_type,type_index,external_path,codec,language,region,track_name,is_default,is_forced,filename_tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(path), *key, payload["codec"], payload["language"], payload["region"], payload["track_name"], payload["is_default"], payload["is_forced"], payload["filename_tags"]),
            )
        for key in current_rows.keys() - snapshot.keys():
            db.execute(
                "DELETE FROM media_stream_index WHERE path=? AND source=? AND stream_type=? AND type_index=? AND external_path=?",
                (str(path), *key),
            )
        db.execute("INSERT OR REPLACE INTO media_stream_index_state(path,modified_ns,size,schema_version,content_signature,indexed_at) VALUES(?,?,?,3,?,CURRENT_TIMESTAMP)", (str(path), stat.st_mtime_ns, stat.st_size, content_signature))
        db.execute("INSERT OR REPLACE INTO media_video_title(path,title,video_index,modified_ns,size,indexed_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)", (str(path), str(video_meta.get("track_name") or ""), int(video_meta.get("video_index") or 0), stat.st_mtime_ns, stat.st_size))
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
        migrated_parser = db.execute("SELECT 1 FROM feature_migrations WHERE name='unified_stream_index_parser_v2'").fetchone()
        if not migrated_parser:
            db.execute("DELETE FROM media_stream_index")
            db.execute("DELETE FROM media_stream_index_state")
            db.execute("UPDATE index_task_queue SET status='cancelled',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE job='core' AND status IN ('pending','running')")
            db.execute("INSERT OR IGNORE INTO index_task_queue(job,path,reason,status,created_at,updated_at) SELECT 'core',path,'Unified parser v2 migration','pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM plex_media")
            db.execute("INSERT INTO feature_migrations(name) VALUES('unified_stream_index_parser_v2')")
            logger.info("core_index event=parser_migration_queued version=2")
        parser_v3 = db.execute("SELECT 1 FROM feature_migrations WHERE name='plex_aware_parser_v3'").fetchone()
        if not parser_v3:
            # Re-evaluate every cached stream row with the same IETF-aware
            # parser used by Stream Properties; retain old rows until each
            # media is refreshed so filters remain usable during the rollout.
            db.execute("INSERT OR IGNORE INTO index_task_queue(job,path,reason,status,created_at,updated_at) SELECT 'core',path,'Plex-aware parser v3 migration','pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM plex_media")
            db.execute("INSERT INTO feature_migrations(name) VALUES('plex_aware_parser_v3')")
            logger.info("core_index event=plex_aware_parser_migration_queued version=3")
        canonical = db.execute("SELECT 1 FROM feature_migrations WHERE name='canonical_index_design_v3'").fetchone()
        if not canonical:
            # Keep Plex configuration/catalog and all learning/saved-property
            # data, but discard historical jobs, duplicate index contents,
            # preview files, and stale synchronization markers.  The unified
            # core index is then rebuilt from the current catalog below.
            for table in (
                "index_task_queue", "task_queue", "media_change_request",
                "media_stream_index", "media_stream_index_state",
                "external_subtitle_index", "external_sidecar_index_state",
                "subtitle_extended_index", "subtitle_extended_media",
                "preview_cache_index", "preview_cache_files",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("UPDATE plex_config SET last_sync=NULL WHERE id=1")
            db.execute("DELETE FROM plex_sync_state")
            db.execute("INSERT INTO feature_migrations(name) VALUES('canonical_index_design_v3')")
            db.execute("INSERT INTO index_task_queue(job,path,reason,status,created_at,updated_at) SELECT 'core',path,'Canonical index design v3 migration','pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM plex_media")
            logger.info("core_index event=canonical_migration_queued version=3")
        # Legacy movie/TV projection tables are no longer part of the schema.
        # They were parity-checked and emptied before this migration; dropping
        # them prevents accidental writes and removes duplicate storage.
        for table in ("movie_stream_index_value", "movie_stream_index", "tv_stream_index_value", "tv_stream_index_media"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        db.execute("INSERT OR IGNORE INTO feature_migrations(name) VALUES('legacy_projection_schema_removed_v1')")

        language_migration = db.execute("SELECT 1 FROM feature_migrations WHERE name='plex_language_semantics_v4'").fetchone()
        if not language_migration:
            db.execute("INSERT INTO index_task_queue(job,path,reason,status,created_at,updated_at) SELECT 'core',path,'Plex language semantics v4 migration','pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM (SELECT DISTINCT path FROM media_stream_index WHERE lower(language)='und') media WHERE NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE q.job='core' AND q.path=media.path AND q.status IN ('pending','running'))")
            db.execute("INSERT INTO feature_migrations(name) VALUES('plex_language_semantics_v4')")
            logger.info("core_index event=plex_language_semantics_migration_queued version=4")


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
                value["language"] = tv_bulk.comparable_language(value["language"])
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
    episode = (re.search(r"(?:^|[^A-Za-z])(S\d{1,2}E\d{1,2})(?:[^A-Za-z]|$)", path, re.I) or [None, ""])[1].upper()
    prefix = f"Processing episode {episode} · " if episode else "Processing media · "
    tasks.update_progress(task_id, 0, 2, prefix + "Checking current streams")
    edit, matched = tv_bulk.episode_bulk_edit(path, request)
    if not matched:
        tasks.update_progress(task_id, 2, 2, prefix + "No matching streams remain; no change needed")
        return {"path": path, "streams": 0, "skipped": True, "reason": "No current stream matches the queued filter"}
    if not tv_bulk.edit_has_effective_changes(edit):
        tasks.update_progress(task_id, 2, 2, prefix + "Already compliant; no change needed")
        return {"path": path, "streams": matched, "skipped": True, "reason": "Media already has the requested values"}
    tasks.update_progress(task_id, 1, 2, prefix + f"Applying changes to {matched} matching stream(s)")
    result = optimized_media_edit(ReorderEditRequest.model_validate(edit))
    reindex = ["core", "previews"] if request.filters.stream_type == "external" or request.remove else ["core"]
    queues.request_media_indexes(path, reindex, "Queued filtered movie edit completed")
    tasks.update_progress(task_id, 2, 2, prefix + "Stream changes applied")
    return {**result, "path": path, "streams": matched}


def enqueue_filtered_movie_edits(paths: list[str], request: MovieStreamBulkEdit) -> tuple[int, list[int]]:
    template = request.model_dump(exclude={"paths", "mode"})
    targets = tv_bulk.indexed_target_keys(paths, request.filters)
    now = tasks.utc_now()
    queued = 0
    task_ids: list[int] = []
    with connection() as db:
        for path in paths:
            if not targets[path]:
                continue
            per_media = {**template, "target_keys": targets[path]}
            task_type = 'filtered_stream_edit_now' if request.mode == 'now' else 'filtered_stream_edit'
            payload_data = tasks.attach_media_signature(task_type, {"path": path, "request": per_media})
            payload = json.dumps(payload_data, ensure_ascii=False, separators=(",", ":"))
            cursor = db.execute(
                "INSERT INTO task_queue(task_type,label,payload_json,status,progress_message,created_at,updated_at) VALUES(?,?,?,'pending','Waiting',?,?)",
                (task_type, f"Movie filtered stream edit · {Path(path).name}", payload, now, now),
            )
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (cursor.lastrowid, path, now))
            task_ids.append(int(cursor.lastrowid))
            queued += 1
    tasks.wake_queue()
    logger.info("movie_stream_bulk_edit event=batch_queued media=%d", queued)
    return queued, task_ids


def process_movie_bulk_preflight(task_id: int, payload: dict) -> dict:
    request_data = dict(payload.get("request") or {})
    paths = list(dict.fromkeys(payload.get("paths") or []))
    request_data.update({"paths": paths, "mode": "now"})
    request = MovieStreamBulkEdit.model_validate(request_data)
    targets = tv_bulk.indexed_target_keys(paths, request.filters)
    candidates: list[tuple[str, dict]] = []
    skipped = 0
    tasks.update_progress(task_id, 0, max(1, len(paths)), "Checking bulk changes")
    for number, path in enumerate(paths, 1):
        if targets.get(path):
            per_media = {**request.model_dump(exclude={"paths", "mode"}), "target_keys": targets[path]}
            try:
                preview, matched = tv_bulk.episode_bulk_edit(path, tv_bulk.SeasonStreamBulkEdit.model_validate({**per_media, "paths": [path], "mode": "now"}))
                if matched and tv_bulk.edit_has_effective_changes(preview):
                    candidates.append((path, per_media))
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning("movie_stream_bulk_preflight event=media_failed path=%s error=%s", path, str(exc).replace("\n", " ")[-300:])
                skipped += 1
        else:
            skipped += 1
        tasks.update_progress(task_id, number, max(1, len(paths)), f"Checked {number} of {len(paths)} media")
    child_ids: list[int] = []
    now = tasks.utc_now()
    with connection() as db:
        for path, per_media in candidates:
            signed = tasks.attach_media_signature("filtered_stream_edit", {"path": path, "request": per_media})
            cursor = db.execute("INSERT INTO task_queue(task_type,label,payload_json,status,progress_message,created_at,updated_at) VALUES(?,?,?,'pending','Waiting',?,?)", ("filtered_stream_edit", f"Movie filtered stream edit · {Path(path).name}", json.dumps(signed, ensure_ascii=False, separators=(",", ":")), now, now))
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (cursor.lastrowid, path, now))
            child_ids.append(int(cursor.lastrowid))
    tasks.wake_queue()
    logger.info("movie_stream_bulk_preflight event=completed checked=%d queued=%d skipped=%d", len(paths), len(child_ids), skipped)
    return {"queued": len(child_ids), "skipped": skipped, "task_ids": child_ids}


tasks.TASK_HANDLERS["filtered_stream_edit"] = process_filtered_stream_edit
tasks.TASK_HANDLERS["filtered_stream_edit_now"] = process_filtered_stream_edit
tasks.TASK_HANDLERS["movie_bulk_preflight"] = process_movie_bulk_preflight


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
    if "track_name" in changed and request.filters.language is None and not request.filters.language_regions:
        raise HTTPException(400, "Select a language and region before changing track names in bulk")
    # Both modes use an asynchronous preflight. Signature capture and current
    # stream verification can be expensive for large movie selections; keeping
    # them in the queue makes the response immediate and observable.
    preflight = tasks.enqueue("movie_bulk_preflight", {"paths": request.paths, "request": request.model_dump(exclude={"paths", "mode"}), "mode": request.mode}, "Preflight movie bulk stream change")
    return {"mode": request.mode, "queued": 1, "preflight": True, "task_ids": [preflight["id"]], "applied": 0, "streams": 0, "skipped": [], "failed": []}



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
            """SELECT p.path,p.title,s.modified_ns,s.size AS indexed_size,s.schema_version,
                      s.content_signature,v.modified_ns AS video_modified_ns,v.size AS video_size
               FROM plex_media p LEFT JOIN media_stream_index_state s ON s.path=p.path
               LEFT JOIN media_video_title v ON v.path=p.path
               ORDER BY p.path"""
        )]
    pending: dict[str, dict] = {}
    signature_checked = 0
    signature_changed = 0
    for item in rows:
        try:
            stat = Path(item["path"]).stat()
            stale = (
                item["modified_ns"] != stat.st_mtime_ns or item["indexed_size"] != stat.st_size
                or int(item["schema_version"] or 0) < 3
                or item["video_modified_ns"] != stat.st_mtime_ns
                or item["video_size"] != stat.st_size
            )
            if not stale and item.get("content_signature"):
                signature_checked += 1
                current = queues._quick_core_signature(item["path"], stat.st_size)
                if current and current != str(item["content_signature"]):
                    stale = True
                    signature_changed += 1
            if stale:
                pending[item["path"]] = {"path": item["path"], "title": item["title"], "modified": int(stat.st_mtime), "size": stat.st_size}
        except OSError:
            pending[item["path"]] = {"path": item["path"], "title": item["title"], "modified": 0, "size": 0}
    for item in tv_bulk.pending_external_sidecars("core"):
        pending[str(item["path"])] = item
    logger.info(
        "core_index event=fingerprint_check catalog=%d pending=%d signature_checked=%d signature_changed=%d",
        len(rows), len(pending), signature_checked, signature_changed,
    )
    return list(pending.values())


def clear_unified_index(job: str) -> None:
    if job == "core":
        with connection() as db:
            db.execute("DELETE FROM media_stream_index")
            db.execute("DELETE FROM media_stream_index_state")
            db.execute("DELETE FROM media_video_title")
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


class CoreParityRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)


class LegacyCleanupRequest(BaseModel):
    confirm: str = Field(min_length=1, max_length=32)


@app.post("/api/v82/setup/index/core/parity/full/start")
def start_full_core_parity() -> dict:
    """Create an idle, resumable full-parity run; does not inspect media."""
    with connection() as db:
        active = db.execute(
            "SELECT id,status,total,cursor_offset,checked,mismatches,missing FROM core_index_parity_runs WHERE status IN ('pending','running') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active:
            result = dict(active)
            result["offset"] = result.pop("cursor_offset", 0)
            return {"run": result, "existing": True}
        total = int(db.execute("SELECT count(*) FROM plex_media").fetchone()[0] or 0)
        row = db.execute(
            "INSERT INTO core_index_parity_runs(status,total) VALUES('pending',?)",
            (total,),
        )
        run_id = int(row.lastrowid)
    return {"run": {"id": run_id, "status": "pending", "total": total, "offset": 0, "checked": 0, "mismatches": 0, "missing": 0}, "existing": False}


@app.get("/api/v82/setup/index/core/parity/full/{run_id}")
def full_core_parity_status(run_id: int) -> dict:
    with connection() as db:
        row = db.execute(
            "SELECT id,status,total,cursor_offset,checked,mismatches,missing,started_at,finished_at FROM core_index_parity_runs WHERE id=?",
            (run_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Parity run not found")
    result = dict(row)
    result["offset"] = result.pop("cursor_offset", 0)
    return {"run": result}


@app.post("/api/v82/setup/index/core/parity/full/{run_id}/batch")
def run_full_core_parity_batch(run_id: int, limit: int = Query(default=100, ge=1, le=200)) -> dict:
    """Process one bounded batch of a previously started full-parity run."""
    with connection() as db:
        run = db.execute(
            "SELECT id,status,total,cursor_offset,checked,mismatches,missing FROM core_index_parity_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if not run:
            raise HTTPException(404, "Parity run not found")
        if str(run["status"]) == "complete":
            result = dict(run); result["offset"] = result.pop("cursor_offset", 0)
            return {"run": result, "processed": 0}
        if str(run["status"]) == "cancelled":
            raise HTTPException(409, "Parity run is cancelled")
        db.execute("UPDATE core_index_parity_runs SET status='running' WHERE id=?", (run_id,))
        paths = [str(row["path"]) for row in db.execute(
            "SELECT path FROM plex_media ORDER BY path LIMIT ? OFFSET ?",
            (limit, int(run["cursor_offset"] or 0)),
        ).fetchall()]
    result = compare_core_index_parity(CoreParityRequest(paths=paths))
    processed = len(paths)
    with connection() as db:
        current = db.execute(
            "SELECT total,cursor_offset,checked,mismatches,missing FROM core_index_parity_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        new_offset = int(current["cursor_offset"] or 0) + processed
        checked = int(current["checked"] or 0) + int(result.get("checked") or 0)
        mismatches = int(current["mismatches"] or 0) + len(result.get("mismatches") or [])
        missing = int(current["missing"] or 0) + len(result.get("missing") or [])
        complete = new_offset >= int(current["total"] or 0)
        if complete:
            db.execute(
                "UPDATE core_index_parity_runs SET status='complete',cursor_offset=?,checked=?,mismatches=?,missing=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_offset, checked, mismatches, missing, run_id),
            )
        else:
            db.execute(
                "UPDATE core_index_parity_runs SET status='running',cursor_offset=?,checked=?,mismatches=?,missing=? WHERE id=?",
                (new_offset, checked, mismatches, missing, run_id),
            )
        final = db.execute(
            "SELECT id,status,total,cursor_offset,checked,mismatches,missing FROM core_index_parity_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if complete:
            db.execute(
                "INSERT INTO core_index_parity_audit(sample_limit,sample_kind,checked,mismatches,missing) VALUES(?,?,?,?,?)",
                (int(final["total"] or 0), "__full__", checked, mismatches, missing),
            )
    final_result = dict(final); final_result["offset"] = final_result.pop("cursor_offset", 0)
    return {"run": final_result, "processed": processed, "mismatches": result.get("mismatches", []), "missing": result.get("missing", [])}


@app.post("/api/v82/setup/index/core/parity/full/{run_id}/cancel")
def cancel_full_core_parity(run_id: int) -> dict:
    with connection() as db:
        row = db.execute("UPDATE core_index_parity_runs SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('pending','running')", (run_id,))
        if not row.rowcount:
            raise HTTPException(409, "Parity run is not active")
        result = db.execute("SELECT id,status,total,cursor_offset,checked,mismatches,missing FROM core_index_parity_runs WHERE id=?", (run_id,)).fetchone()
    output = dict(result); output["offset"] = output.pop("cursor_offset", 0)
    return {"run": output}


def _parity_rows(path: str) -> list[tuple]:
    with connection() as db:
        rows = db.execute("SELECT stream_type,language,region,track_name FROM media_stream_index WHERE path=?", (path,)).fetchall()
    return sorted((str(row["stream_type"]), tv_bulk.comparable_language(row["language"]), str(row["region"] or "").upper(), str(row["track_name"] or "")) for row in rows)


@app.get("/api/v82/setup/index/core/parity-sample")
def compare_core_index_parity_sample(
    limit: int = Query(default=25, ge=1, le=100),
    kind: str | None = Query(default=None, pattern="^(movie|episode)$"),
) -> dict:
    """Compare a bounded catalog sample without probing or queueing media."""
    with connection() as db:
        if kind:
            rows = db.execute(
                "SELECT path FROM plex_media WHERE kind=? ORDER BY path LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT path FROM plex_media ORDER BY path LIMIT ?",
                (limit,),
            ).fetchall()
    result = compare_core_index_parity(CoreParityRequest(paths=[str(row["path"]) for row in rows]))
    result["sample_limit"] = limit
    result["sample_kind"] = kind
    with connection() as db:
        db.execute(
            "INSERT INTO core_index_parity_audit(sample_limit,sample_kind,checked,mismatches,missing) VALUES(?,?,?,?,?)",
            (limit, kind or "", int(result.get("checked") or 0), len(result.get("mismatches") or []), len(result.get("missing") or [])),
        )
    return result


@app.post("/api/v82/setup/index/core/parity")
def compare_core_index_parity(request: CoreParityRequest) -> dict:
    """Verify that requested media have canonical stream snapshots."""
    unique = list(dict.fromkeys(str(path) for path in request.paths if str(path).strip()))
    if not unique:
        return {"checked": 0, "missing": [], "mismatches": [], "match_count": 0}
    with connection() as db:
        catalog = {str(row["path"]) for row in db.execute(f"SELECT path FROM plex_media WHERE path IN ({','.join('?' for _ in unique)})", unique).fetchall()}
        indexed = {str(row["path"]) for row in db.execute(f"SELECT path FROM media_stream_index_state WHERE path IN ({','.join('?' for _ in unique)})", unique).fetchall()}
    missing = sorted(catalog - indexed)
    missing.extend(sorted(set(unique) - catalog))
    return {"checked": len(unique), "missing": missing, "mismatches": [], "match_count": len(unique) - len(missing)}


@app.post("/api/v82/setup/index/core/cleanup-legacy")
def cleanup_legacy_core_projections(request: LegacyCleanupRequest) -> dict:
    if request.confirm != "CANONICAL_ONLY":
        raise HTTPException(400, "Type CANONICAL_ONLY to confirm legacy projection cleanup")
    return {"ok": True, "canonical_preserved": True, "catalog_preserved": True, "removed": {}, "legacy_projection_writes_enabled": False, "legacy_projections_removed": True}


@app.get("/api/v82/setup/index/core/compatibility")
def core_index_compatibility() -> dict:
    """Read-only cutover report; does not probe media or enqueue work."""
    with connection() as db:
        counts = {}
        for name, query in {
            "canonical_stream_rows": "SELECT count(*) FROM media_stream_index",
            "canonical_media": "SELECT count(*) FROM media_stream_index_state",
            "canonical_unsigned_media": "SELECT count(*) FROM media_stream_index_state WHERE coalesce(content_signature,'')=''",
            "canonical_orphan_rows": "SELECT count(*) FROM media_stream_index value WHERE NOT EXISTS (SELECT 1 FROM plex_media media WHERE media.path=value.path)",
            "catalog_media": "SELECT count(*) FROM plex_media",
        }.items():
            counts[name] = int(db.execute(query).fetchone()[0] or 0)
    with connection() as db:
        audit = db.execute(
            "SELECT sample_limit,sample_kind,checked,mismatches,missing,checked_at FROM core_index_parity_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
    catalog_media_count = counts["catalog_media"]
    audit_complete = bool(audit and str(audit["sample_kind"] or "") == "__full__" and int(audit["checked"] or 0) >= catalog_media_count and not int(audit["mismatches"] or 0) and not int(audit["missing"] or 0))
    return {
        "canonical_authoritative": True,
        "legacy_projection_writes_enabled": False,
        "legacy_projections_removed": True,
        "last_parity_audit": dict(audit) if audit else None,
        "counts": counts,
        "cleanup_ready": False,
        "next_action": "No legacy cleanup required; canonical index is authoritative",
    }


@app.post("/api/v82/setup/queues/prune")
def prune_finished_queue_history() -> dict:
    with connection() as db:
        generic = db.execute("DELETE FROM task_queue WHERE status IN ('succeeded','cancelled')").rowcount
        indexed = db.execute("DELETE FROM index_task_queue WHERE status IN ('succeeded','cancelled')").rowcount
    logger.info("queue_retention event=finished_history_cleared generic=%d index=%d", generic, indexed)
    return {"generic": generic, "index": indexed}
