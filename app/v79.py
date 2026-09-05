from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v54 as indexes
import app.v65 as tasks
from app.v11 import connection
from app.v13 import media_details_with_ietf
from app.v43 import optimized_media_edit
from app.v5 import SUBTITLE_EXTENSIONS, external_filename_metadata, external_subtitles
from app.v78 import app
from app.v7 import ReorderEditRequest


logger = logging.getLogger("videostreamedit")
_legacy_processors = dict(indexes.processors)


class SeasonStreamRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=2000)


class SeasonStreamFilter(BaseModel):
    stream_type: Literal["audio", "subtitle", "external"] | None = None
    language: str | None = None
    region: str | None = None
    track_name: str | None = None
    filename_tag: str | None = None


class SeasonStreamBulkEdit(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=2000)
    filters: SeasonStreamFilter
    changed_fields: list[Literal["language", "region", "track_name", "integrate", "remove"]] = Field(min_length=1)
    language: str = Field(default="", max_length=64)
    region: str = Field(default="", max_length=64)
    track_name: str = Field(default="", max_length=512)
    integrate: bool = False
    remove: bool = False
    mode: Literal["now", "queue"]


def ensure_tv_stream_index() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tv_stream_index_media (
                path TEXT PRIMARY KEY, modified INTEGER NOT NULL, size INTEGER NOT NULL,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tv_stream_index_value (
                path TEXT NOT NULL, stream_type TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '',
                track_name TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS tv_stream_index_value_path ON tv_stream_index_value(path);
            CREATE INDEX IF NOT EXISTS tv_stream_index_value_filter
                ON tv_stream_index_value(stream_type,language,region,track_name);
            CREATE TABLE IF NOT EXISTS tv_stream_index_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS external_subtitle_index (
                media_path TEXT NOT NULL, external_path TEXT NOT NULL,
                codec TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '', track_name TEXT NOT NULL DEFAULT '',
                forced INTEGER NOT NULL DEFAULT 0, filename_tags TEXT NOT NULL DEFAULT '[]',
                size INTEGER NOT NULL DEFAULT 0, modified_ns INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(media_path,external_path)
            );
            CREATE INDEX IF NOT EXISTS external_subtitle_index_media ON external_subtitle_index(media_path);
            CREATE TABLE IF NOT EXISTS external_sidecar_index_state (
                job TEXT NOT NULL, path TEXT NOT NULL, signature TEXT NOT NULL,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(job,path)
            );
        """)


@app.on_event("startup")
def initialize_tv_stream_index() -> None:
    ensure_tv_stream_index()
    with connection() as db:
        version = db.execute("SELECT value FROM tv_stream_index_settings WHERE key='format_version'").fetchone()
        if not version or version["value"] != "2":
            db.execute("DELETE FROM tv_stream_index_value")
            db.execute("DELETE FROM tv_stream_index_media")
            db.execute("INSERT OR REPLACE INTO tv_stream_index_settings(key,value) VALUES('format_version','2')")
            logger.info("tv_season_index event=format_upgraded version=2 external_subtitles=enabled")
        db.execute("DELETE FROM tv_stream_index_value WHERE path NOT IN (SELECT path FROM plex_media WHERE kind='episode')")
        db.execute("DELETE FROM tv_stream_index_media WHERE path NOT IN (SELECT path FROM plex_media WHERE kind='episode')")
        db.execute("DELETE FROM external_subtitle_index WHERE media_path NOT IN (SELECT path FROM plex_media)")
        db.execute("DELETE FROM external_sidecar_index_state WHERE path NOT IN (SELECT path FROM plex_media)")


def external_sidecar_data(path: str, candidates: list[Path] | None = None) -> tuple[list[dict], str]:
    media = Path(path)
    if not media.is_file():
        return [], "missing"
    values = []
    if candidates is None:
        streams = external_subtitles(media)
    else:
        prefix = media.stem.casefold()
        streams = []
        for candidate in candidates:
            candidate_stem = candidate.stem
            folded = candidate_stem.casefold()
            if folded != prefix and not folded.startswith(prefix + "."):
                continue
            suffix = candidate_stem[len(media.stem):].lstrip(".")
            language, region, filename_tags = external_filename_metadata(suffix)
            streams.append({
                "path": str(candidate.resolve()), "name": candidate.name,
                "codec_type": "subtitle", "codec": candidate.suffix.lower().lstrip("."),
                "language": language, "region": region, "title": "",
                "forced": "Forced" in filename_tags, "external": True,
                "filename_tags": filename_tags,
            })
    for stream in streams:
        subtitle = Path(stream["path"])
        try:
            stat = subtitle.stat()
            size, modified_ns = stat.st_size, stat.st_mtime_ns
        except OSError:
            size = modified_ns = 0
        values.append({**stream, "size": size, "modified_ns": modified_ns})
    fingerprint = json.dumps(
        [(item["path"], item["size"], item["modified_ns"]) for item in values],
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return values, hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def persist_external_sidecars(job: str, path: str) -> None:
    values, signature = external_sidecar_data(path)
    with connection() as db:
        if job == "core":
            db.execute("DELETE FROM external_subtitle_index WHERE media_path=?", (path,))
            db.executemany(
                "INSERT INTO external_subtitle_index(media_path,external_path,codec,language,region,track_name,forced,filename_tags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(path, item["path"], str(item.get("codec") or ""), str(item.get("language") or ""), str(item.get("region") or ""), str(item.get("title") or ""), int(bool(item.get("forced"))), json.dumps(item.get("filename_tags") or [], ensure_ascii=False), item["size"], item["modified_ns"]) for item in values],
            )
        db.execute("INSERT OR REPLACE INTO external_sidecar_index_state(job,path,signature,indexed_at) VALUES(?,?,?,CURRENT_TIMESTAMP)", (job, path, signature))


def pending_external_sidecars(job: str) -> list[dict]:
    ensure_tv_stream_index()
    with connection() as db:
        rows = [dict(row) for row in db.execute("""
            SELECT media.path,media.modified,media.size,media.title,state.signature
              FROM plex_media media
              LEFT JOIN external_sidecar_index_state state ON state.job=? AND state.path=media.path
             ORDER BY media.kind,media.title COLLATE NOCASE,media.path
        """, (job,))]
    pending = []
    directory_candidates: dict[Path, list[Path]] = {}
    for directory in {Path(row["path"]).parent for row in rows}:
        try:
            directory_candidates[directory] = [
                candidate for candidate in sorted(directory.iterdir(), key=lambda value: value.name.casefold())
                if candidate.is_file() and candidate.suffix.lower() in SUBTITLE_EXTENSIONS
            ]
        except OSError:
            directory_candidates[directory] = []
    baselines: list[tuple[dict, list[dict], str]] = []
    for row in rows:
        values, signature = external_sidecar_data(row["path"], directory_candidates[Path(row["path"]).parent])
        if row.get("signature") is None:
            baselines.append((row, values, signature))
        elif row["signature"] != signature:
            row.pop("signature", None)
            pending.append(row)
    if baselines:
        with connection() as db:
            if job == "core":
                for row, values, _ in baselines:
                    db.execute("DELETE FROM external_subtitle_index WHERE media_path=?", (row["path"],))
                    db.executemany(
                        "INSERT INTO external_subtitle_index(media_path,external_path,codec,language,region,track_name,forced,filename_tags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [(row["path"], item["path"], str(item.get("codec") or ""), str(item.get("language") or ""), str(item.get("region") or ""), str(item.get("title") or ""), int(bool(item.get("forced"))), json.dumps(item.get("filename_tags") or [], ensure_ascii=False), item["size"], item["modified_ns"]) for item in values],
                    )
            db.executemany(
                "INSERT OR REPLACE INTO external_sidecar_index_state(job,path,signature,indexed_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                [(job, row["path"], signature) for row, _, signature in baselines],
            )
    logger.info("external_sidecar_check event=completed job=%s media=%d baselined=%d pending=%d", job, len(rows), len(baselines), len(pending))
    return pending


def pending_episode_rows() -> list[dict]:
    ensure_tv_stream_index()
    with connection() as db:
        return [dict(row) for row in db.execute("""
            SELECT media.path,media.modified,media.size,media.title
              FROM plex_media media
              LEFT JOIN tv_stream_index_media cached ON cached.path=media.path
             WHERE media.kind='episode'
               AND (cached.path IS NULL OR cached.modified!=media.modified OR cached.size!=media.size)
             ORDER BY media.title COLLATE NOCASE,media.path
        """)]


def inspect_embedded_streams(path: str) -> list[tuple[str, str, str, str]]:
    details = media_details_with_ietf(path)
    embedded = [
        (
            str(stream.get("codec_type") or ""), str(stream.get("language") or "").strip(),
            str(stream.get("region") or "").strip(), str(stream.get("title") or "").strip(),
        )
        for stream in details.get("streams", [])
        if stream.get("codec_type") in {"audio", "subtitle"} and not stream.get("external")
    ]
    external = [
        ("external", str(stream.get("language") or "").strip(), str(stream.get("region") or "").strip(), str(stream.get("title") or "").strip())
        for stream in details.get("external_subtitles", [])
    ]
    return embedded + external


def index_episode(item: dict) -> None:
    ensure_tv_stream_index()
    path = str(item["path"])
    values = inspect_embedded_streams(path)
    with connection() as db:
        db.execute("DELETE FROM tv_stream_index_value WHERE path=?", (path,))
        db.executemany(
            "INSERT INTO tv_stream_index_value(path,stream_type,language,region,track_name) VALUES(?,?,?,?,?)",
            [(path, *value) for value in values],
        )
        db.execute(
            "INSERT OR REPLACE INTO tv_stream_index_media(path,modified,size,indexed_at) VALUES(?,?,?,datetime('now'))",
            (path, int(item["modified"]), int(item["size"])),
        )
    persist_external_sidecars("core", path)


def core_index_with_tv(item: dict) -> None:
    with connection() as db:
        row = db.execute("SELECT kind FROM plex_media WHERE path=?", (item["path"],)).fetchone()
    if row and row["kind"] == "episode":
        index_episode(item)
    else:
        _legacy_processors["core"](item)
        persist_external_sidecars("core", str(item["path"]))


def subtitle_index_with_sidecars(item: dict) -> None:
    _legacy_processors["subtitles"](item)
    persist_external_sidecars("subtitles", str(item["path"]))


def preview_index_with_sidecars(item: dict) -> None:
    _legacy_processors["previews"](item)
    persist_external_sidecars("previews", str(item["path"]))


indexes.processors["core"] = core_index_with_tv
indexes.processors["subtitles"] = subtitle_index_with_sidecars
indexes.processors["previews"] = preview_index_with_sidecars


def selected_episode_rows(paths: list[str]) -> list[dict]:
    unique = list(dict.fromkeys(paths))
    found = []
    with connection() as db:
        for start in range(0, len(unique), 800):
            group = unique[start:start + 800]
            placeholders = ",".join("?" for _ in group)
            rows = db.execute(
                f"SELECT path,modified,size,title FROM plex_media WHERE kind='episode' AND path IN ({placeholders})",
                group,
            ).fetchall()
            found.extend(dict(row) for row in rows)
    if {item["path"] for item in found} != set(unique):
        raise HTTPException(409, "The selected season changed. Refresh TV Shows and try again")
    return found


@app.post("/api/v79/tv/season-stream-values")
def season_stream_values(request: SeasonStreamRequest) -> dict:
    ensure_tv_stream_index()
    items = selected_episode_rows(request.paths)
    pending = []
    with connection() as db:
        cached = {
            row["path"]: row for start in range(0, len(items), 800)
            for row in db.execute(
                f"SELECT path,modified,size FROM tv_stream_index_media WHERE path IN ({','.join('?' for _ in items[start:start + 800])})",
                [item["path"] for item in items[start:start + 800]],
            ).fetchall()
        }
    for item in items:
        old = cached.get(item["path"])
        if not old or int(old["modified"]) != int(item["modified"]) or int(old["size"]) != int(item["size"]):
            pending.append(item)
    errors = []
    for number, item in enumerate(pending, 1):
        try:
            index_episode(item)
        except Exception as exc:
            errors.append({"path": item["path"], "error": str(getattr(exc, "detail", exc))[-500:]})
        if number == 1 or number == len(pending) or number % 10 == 0:
            logger.info("tv_season_index event=progress completed=%d total=%d errors=%d", number, len(pending), len(errors))
    paths = [item["path"] for item in items]
    values = []
    with connection() as db:
        for start in range(0, len(paths), 800):
            group = paths[start:start + 800]
            rows = db.execute(
                f"SELECT path,stream_type,language,region,track_name,filename_tags FROM media_stream_index WHERE path IN ({','.join('?' for _ in group)})",
                group,
            ).fetchall()
            for row in rows:
                value = dict(row)
                try:
                    value["filename_tags"] = json.loads(value["filename_tags"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    value["filename_tags"] = []
                values.append(value)
    logger.info("tv_season_index event=ready episodes=%d inspected=%d values=%d errors=%d", len(items), len(pending), len(values), len(errors))
    return {"values": values, "episodes": len(items), "inspected": len(pending), "errors": errors}


def comparable_language(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return {"por": "pt", "pob": "pt", "eng": "en"}.get(normalized, normalized)


def region_matches(stream: dict, expected: str | None) -> bool:
    if expected is None:
        return True
    actual = str(stream.get("region") or "").strip().upper()
    wanted = str(expected).strip().upper()
    if actual == wanted:
        return True
    return wanted == "BR" and not actual and comparable_language(stream.get("language")) == "pt"


def filter_matches(stream: dict, filters: SeasonStreamFilter) -> bool:
    return (
        (filters.stream_type is None or stream.get("codec_type") == filters.stream_type)
        and (filters.language is None or comparable_language(stream.get("language")) == comparable_language(filters.language))
        and region_matches(stream, filters.region)
        and (filters.track_name is None or str(stream.get("title") or "").strip() == filters.track_name)
        and (filters.filename_tag is None or filters.filename_tag in (stream.get("filename_tags") or []))
    )


def episode_bulk_edit(path: str, request: SeasonStreamBulkEdit) -> tuple[dict, int]:
    details = media_details_with_ietf(path)
    tracks, external_changes, order, remove = [], [], [], []
    tags = {"default_audio": None, "forced_audio": None, "default_subtitle": None, "forced_subtitle": None}
    matches = 0
    changed = set(request.changed_fields)
    for stream in details["streams"]:
        stream_type = stream["codec_type"]
        type_index = int(stream["type_index"])
        key = f"embedded:{stream_type}:{type_index}"
        order.append({"source": "embedded", "codec_type": stream_type, "type_index": type_index})
        if stream.get("default"):
            tags[f"default_{stream_type}"] = key
        if stream.get("forced"):
            tags[f"forced_{stream_type}"] = key
        if not filter_matches(stream, request.filters):
            continue
        if request.remove:
            remove.append(key)
            matches += 1
            continue
        update = {"codec_type": stream_type, "type_index": type_index}
        if "language" in changed or "region" in changed:
            update["language"] = request.language if "language" in changed else str(stream.get("language") or "")
            update["region"] = request.region if "region" in changed else str(stream.get("region") or "")
        if "track_name" in changed:
            update["title"] = request.track_name
        tracks.append(update)
        matches += 1
    for stream in details.get("external_subtitles", []):
        external_path = str(stream["path"])
        key = f"external:{external_path}"
        order.append({"source": "external", "codec_type": "subtitle", "path": external_path})
        comparable = {**stream, "codec_type": "external"}
        if not filter_matches(comparable, request.filters):
            continue
        if request.remove:
            remove.append(key)
            external_changes.append({
                "path": external_path, "embed": False,
                "language": str(stream.get("language") or ""),
                "region": str(stream.get("region") or ""),
                "title": str(stream.get("title") or ""),
                "forced": bool(stream.get("forced")),
            })
        elif request.integrate:
            external_changes.append({
                "path": external_path, "embed": True,
                "language": request.language if "language" in changed else str(stream.get("language") or ""),
                "region": request.region if "region" in changed else str(stream.get("region") or ""),
                "title": request.track_name if "track_name" in changed else str(stream.get("title") or ""),
                "forced": bool(stream.get("forced")),
            })
        matches += 1
    return {"path": path, "tracks": tracks, "external_subtitles": external_changes, "order": order, **tags, "remove": remove}, matches


def process_tv_filtered_stream_edit(task_id: int, payload: dict) -> dict:
    path = str(payload["path"])
    request_data = dict(payload["request"])
    request_data["paths"] = [path]
    request_data["mode"] = "now"
    request = SeasonStreamBulkEdit.model_validate(request_data)
    tasks.update_progress(task_id, 0, 2, "Checking current streams")
    edit, matched = episode_bulk_edit(path, request)
    if not matched:
        tasks.update_progress(task_id, 2, 2, "Skipped; no current stream matches the filter")
        return {"path": path, "streams": 0, "skipped": True, "reason": "No current stream matches the queued filter"}
    tasks.update_progress(task_id, 1, 2, f"Applying changes to {matched} matching stream(s)")
    result = optimized_media_edit(ReorderEditRequest.model_validate(edit))
    from app.v80 import request_media_indexes
    reindex = ["core", "previews"] if request.filters.stream_type == "external" or request.remove else ["core"]
    request_media_indexes(path, reindex, "Queued TV filtered stream edit completed")
    tasks.update_progress(task_id, 2, 2, "Stream changes applied")
    return {**result, "path": path, "streams": matched}


def enqueue_tv_filtered_edits(paths: list[str], request: SeasonStreamBulkEdit) -> tuple[int, list[int]]:
    template = request.model_dump(exclude={"paths", "mode"})
    now = tasks.utc_now()
    task_ids: list[int] = []
    with connection() as db:
        for path in paths:
            payload = json.dumps({"path": path, "request": template}, ensure_ascii=False, separators=(",", ":"))
            cursor = db.execute(
                "INSERT INTO task_queue(task_type,label,payload_json,status,progress_message,created_at,updated_at) VALUES('tv_filtered_stream_edit',?,?,'pending','Waiting',?,?)",
                (f"TV filtered stream edit · {Path(path).name}", payload, now, now),
            )
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (cursor.lastrowid, path, now))
            task_ids.append(int(cursor.lastrowid))
    tasks.wake_queue()
    logger.info("tv_stream_bulk_edit event=batch_queued media=%d first_id=%s last_id=%s", len(task_ids), task_ids[0] if task_ids else "none", task_ids[-1] if task_ids else "none")
    return len(task_ids), task_ids


tasks.TASK_HANDLERS["tv_filtered_stream_edit"] = process_tv_filtered_stream_edit

@app.post("/api/v79/tv/season-stream-bulk-edit")
def season_stream_bulk_edit(request: SeasonStreamBulkEdit) -> dict:
    request.paths = list(dict.fromkeys(request.paths))
    selected_episode_rows(request.paths)
    changed = set(request.changed_fields)
    if request.filters.stream_type == "external":
        if request.integrate and request.remove:
            raise HTTPException(400, "External subtitles cannot be integrated and removed in the same operation")
        if changed.intersection({"language", "region", "track_name"}) and not request.integrate:
            raise HTTPException(400, "Integrate must be selected to save properties on external subtitles")
        if not request.integrate and not request.remove:
            raise HTTPException(400, "Select Integrate or Remove for matching external subtitles")
    elif request.integrate:
        raise HTTPException(400, "Integrate is available only for external subtitles")
    if request.remove and changed.intersection({"language", "region", "track_name"}):
        raise HTTPException(400, "Remove cannot be combined with metadata changes")
    if request.mode == "queue":
        queued, task_ids = enqueue_tv_filtered_edits(request.paths, request)
        return {"mode": "queue", "queued": queued, "task_ids": task_ids, "applied": 0, "streams": 0, "skipped": [], "failed": []}
    queued = applied = streams = 0
    skipped, failed = [], []
    for path in request.paths:
        try:
            edit, matched = episode_bulk_edit(path, request)
            if not matched:
                skipped.append({"path": path, "reason": "No current embedded stream matches the filter"})
                continue
            label = Path(path).name
            reindex = ["core", "previews"] if request.filters.stream_type == "external" or request.remove else ["core"]
            if request.mode == "queue":
                tasks.enqueue("media_edit", {"edit": edit, "reindex_indexes": reindex}, f"TV filtered stream edit · {label}")
                queued += 1
            else:
                optimized_media_edit(ReorderEditRequest.model_validate(edit))
                from app.v80 import request_media_indexes
                request_media_indexes(path, reindex, "TV filtered stream edit completed")
                applied += 1
            streams += matched
        except Exception as exc:
            failed.append({"path": path, "error": str(getattr(exc, "detail", exc))[-1000:]})
        completed = queued + applied + len(skipped) + len(failed)
        if completed == 1 or completed == len(request.paths) or completed % 10 == 0:
            logger.info("tv_stream_bulk_edit event=progress mode=%s completed=%d total=%d streams=%d failed=%d", request.mode, completed, len(request.paths), streams, len(failed))
    logger.info("tv_stream_bulk_edit event=completed mode=%s media=%d streams=%d skipped=%d failed=%d", request.mode, queued + applied, streams, len(skipped), len(failed))
    if not queued and not applied:
        raise HTTPException(409, failed[0]["error"] if failed else "No current episode streams match the selected values")
    return {"mode": request.mode, "queued": queued, "applied": applied, "streams": streams, "skipped": skipped, "failed": failed}
