from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import app.v11 as plex
import app.v19 as titles
import app.v54 as indexes
import app.v65 as tasks
from app.postgres_store import mark_read_models_fresh
from app.v2 import probe
from app.v51 import (
    HTML_TAG,
    TEXT_SUBTITLE_CODECS,
    SubtitleCleanup,
    apply_subtitle_cleanup,
    complete_extracted_text,
)
from app.v67 import app
from app.preflight_dispatcher import enqueue_bulk_preflight, enqueue_preflight, register_approval_handler, register_handler

logger = logging.getLogger("uvicorn.error")
plex_sync_lock = threading.Lock()
plex_schedule_thread: threading.Thread | None = None
DAY_INTERVALS = {"daily": 1, "every_other_day": 2, "weekly": 7}


class PlexSyncSchedule(BaseModel):
    frequency: Literal["disabled", "minutes", "hours", "daily", "every_other_day", "weekly"] = "disabled"
    interval: int = Field(default=30, ge=1, le=1440)
    time: str = "03:00"


def parse_clock(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Schedule time must use HH:MM")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(400, "Schedule time must use HH:MM")
    return hour, minute


def selected_libraries() -> list[dict]:
    with plex.connection() as db:
        return [dict(row) for row in db.execute("SELECT library_key,title,kind FROM plex_libraries WHERE selected=1 ORDER BY kind,title COLLATE NOCASE")]


def paged_library(key: str, kind: str, filters: list[tuple[str, int]] | None = None) -> list[dict]:
    start, found = 0, []
    media_type = 1 if kind == "movie" else 4
    query = f"/library/sections/{urllib.parse.quote(key)}/all?type={media_type}"
    for field, timestamp in filters or []:
        query += f"&{field}>={int(timestamp)}"
    while True:
        container = plex.plex_request(query, start=start).get("MediaContainer", {})
        page = container.get("Metadata", [])
        found.extend(page)
        total = int(container.get("totalSize", container.get("size", len(found))))
        if not page or len(found) >= total:
            return found
        start += len(page)


def changed_library_items(library: dict, since: int) -> list[dict]:
    # Plex exposes addedAt and updatedAt as epoch seconds. Query both and merge,
    # because a newly added item and a metadata/file update are distinct events.
    merged: dict[str, dict] = {}
    for field in ("updatedAt", "addedAt"):
        for item in paged_library(library["library_key"], library["kind"], [(field, max(0, since - 2))]):
            merged[str(item.get("ratingKey") or item.get("key"))] = item
    return list(merged.values())


def show_aliases(items: list[dict]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    keys = {str(item.get("grandparentRatingKey")) for item in items if item.get("grandparentRatingKey")}
    for key in keys:
        try:
            metadata = plex.plex_request(f"/library/metadata/{urllib.parse.quote(key)}").get("MediaContainer", {}).get("Metadata", [])
            if metadata:
                displayed = metadata[0].get("originalTitle") or metadata[0].get("title") or ""
                result[key] = [value for value in titles.title_values(metadata[0]) if value.casefold() != str(displayed).casefold()]
        except Exception as exc:
            logger.warning("plex_sync event=show_alias_failed rating_key=%s error=%s", key, str(exc).replace("\n", " ")[-300:])
    return result


def rows_for_items(library: dict, items: list[dict]) -> tuple[list[tuple], list[tuple]]:
    records, aliases = [], []
    show_titles = show_aliases(items) if library["kind"] == "show" else {}
    for item in items:
        kind = "movie" if library["kind"] == "movie" else "episode"
        displayed = item.get("title") or ""
        alternatives = ([value for value in titles.title_values(item) if value.casefold() != str(displayed).casefold()]
                        if kind == "movie" else show_titles.get(str(item.get("grandparentRatingKey")), []))
        for media in item.get("Media", []):
            for part in media.get("Part", []):
                path_value = part.get("file")
                if not path_value:
                    continue
                path = Path(path_value)
                try:
                    stat = path.stat()
                    size, modified = stat.st_size, int(stat.st_mtime)
                except OSError:
                    size, modified = int(part.get("size") or 0), int(item.get("updatedAt") or 0)
                records.append((path_value, kind, str(item.get("ratingKey", "")), library["library_key"], library["title"], displayed or path.stem, item.get("grandparentTitle"), item.get("parentIndex"), item.get("index"), size, modified, int(item.get("addedAt") or 0), int(item.get("updatedAt") or 0)))
                aliases.append((path_value, json.dumps(alternatives, ensure_ascii=False)))
    return records, aliases


def initial_watermark(library_key: str) -> int:
    with plex.connection() as db:
        row = db.execute("SELECT watermark FROM plex_sync_state WHERE library_key=?", (library_key,)).fetchone()
        if row:
            return int(row[0])
        config = db.execute("SELECT last_sync FROM plex_config WHERE id=1").fetchone()
        existing = db.execute("SELECT count(*) FROM plex_media WHERE library_key=?", (library_key,)).fetchone()[0]
    if not existing:
        return 0
    if config and config[0]:
        try:
            return int(datetime.fromisoformat(str(config[0]).replace(" ", "T") + "+00:00").timestamp())
        except ValueError:
            pass
    return int(time.time())


def existing_file_fingerprints(library_key: str) -> dict[str, tuple[int, int]]:
    """Return the media-content fingerprints stored before a Plex sync."""
    with plex.connection() as db:
        rows = db.execute(
            "SELECT path,size,modified FROM plex_media WHERE library_key=?",
            (library_key,),
        ).fetchall()
    return {str(row["path"]): (int(row["size"] or 0), int(row["modified"] or 0)) for row in rows}


def catalog_paths(items: list[dict]) -> set[str]:
    """Extract Plex's current part paths without probing files or stream data."""
    paths: set[str] = set()
    for item in items:
        for media in item.get("Media", []):
            for part in media.get("Part", []):
                if part.get("file"):
                    paths.add(str(part["file"]))
    return paths


def file_changed_records(records: list[tuple], previous: dict[str, tuple[int, int]]) -> list[tuple]:
    """Select new, moved, or content-changed files; ignore Plex-only metadata changes."""
    return [
        record for record in records
        if previous.get(str(record[0])) != (int(record[9] or 0), record[10] or 0)
    ]


def register_internal_change_scope(path: str, scope: dict | None, reason: str = "") -> None:
    """Carry an intentional local-write scope through the next Plex sync."""
    value = str(path or "")
    if not value:
        return
    try:
        stat = Path(value).stat()
    except OSError:
        return
    now = int(time.time())
    with plex.connection() as db:
        db.execute("INSERT OR REPLACE INTO plex_internal_change_scope(path,expected_size,expected_modified,scope_json,reason,created_at,expires_at) VALUES(?,?,?,?,?,?,?)", (value, int(stat.st_size), int(stat.st_mtime), json.dumps(scope or {}, ensure_ascii=False, separators=(",", ":")), str(reason or ""), now, now + 6 * 60 * 60))

def consume_internal_change_scope(path: str, size: int, modified: int) -> tuple[dict, str] | None:
    """Consume a matching intentional-write marker during Plex sync."""
    value = str(path or "")
    with plex.connection() as db:
        row = db.execute("SELECT expected_size,expected_modified,scope_json,reason,expires_at FROM plex_internal_change_scope WHERE path=?", (value,)).fetchone()
        if not row:
            return None
        db.execute("DELETE FROM plex_internal_change_scope WHERE path=?", (value,))
    if int(row[4] or 0) < int(time.time()) or int(row[0] or -1) != int(size or -1) or int(row[1] or -1) != int(modified or -1):
        return None
    try:
        scope = json.loads(row[2] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        scope = {}
    return (scope if isinstance(scope, dict) else {}, str(row[3] or ""))

def structurally_changed_records(records: list[tuple], previous: dict[str, tuple[int, int]]) -> list[tuple]:
    """Return changes that are strong evidence of a replacement/new media.

    A modified timestamp alone is not structural: mkvpropedit and other
    metadata-only edits update mtime without adding/removing media. A new path
    or a size change is retained here; stream add/remove detection is handled
    separately by the index worker after it compares stream identities.
    """
    result = []
    for record in records:
        old = previous.get(str(record[0]))
        if old is None or int(old[0] or 0) != int(record[9] or 0):
            result.append(record)
    return result


def persist_library(library: dict, records: list[tuple], aliases: list[tuple], watermark: int, rebuild: bool, current_paths: set[str] | None = None) -> None:
    moved_paths: dict[str, str] = {}
    with plex.connection() as db:
        current_by_key: dict[str, list[str]] = {}
        for record in records:
            current_by_key.setdefault(str(record[2]), []).append(str(record[0]))
        if rebuild:
            previous = db.execute("SELECT path,rating_key FROM plex_media WHERE library_key=?", (library["library_key"],)).fetchall()
            paths = [row["path"] for row in previous]
            for old in previous:
                current = current_by_key.get(str(old["rating_key"]), [])
                if len(current) == 1 and old["path"] != current[0]:
                    moved_paths[str(old["path"])] = current[0]
            db.execute("DELETE FROM plex_media WHERE library_key=?", (library["library_key"],))
            db.executemany("DELETE FROM plex_title_aliases WHERE path=?", [(path,) for path in paths])
        elif records:
            # A moved Plex item keeps its rating key but receives a new Part
            # path. Replace cached paths for changed items during incremental sync.
            rating_keys = sorted({record[2] for record in records if record[2]})
            placeholders = ",".join("?" for _ in rating_keys)
            stale_rows = db.execute(
                f"SELECT path,rating_key FROM plex_media WHERE library_key=? AND rating_key IN ({placeholders})",
                (library["library_key"], *rating_keys),
            ).fetchall()
            stale_paths = [row["path"] for row in stale_rows]
            for old in stale_rows:
                current = current_by_key.get(str(old["rating_key"]), [])
                if len(current) == 1 and old["path"] != current[0]:
                    moved_paths[str(old["path"])] = current[0]
            db.execute(
                f"DELETE FROM plex_media WHERE library_key=? AND rating_key IN ({placeholders})",
                (library["library_key"], *rating_keys),
            )
            db.executemany("DELETE FROM plex_title_aliases WHERE path=?", [(path,) for path in stale_paths])
        db.executemany("INSERT OR REPLACE INTO plex_media(path,kind,rating_key,library_key,library_name,title,show_title,season_number,episode_number,size,modified,plex_added_at,plex_updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", records)
        db.executemany("INSERT OR REPLACE INTO plex_title_aliases(path,alternatives) VALUES(?,?)", aliases)
        if current_paths is not None:
            # Incremental timestamp queries do not return deleted Plex items.
            # Reconcile against the lightweight full path catalog so removals
            # disappear even when no remaining item was modified.
            stale = db.execute("SELECT path FROM plex_media WHERE library_key=?", (library["library_key"],)).fetchall()
            removed = [row["path"] for row in stale if str(row["path"]) not in current_paths]
            if removed:
                db.executemany("DELETE FROM plex_media WHERE path=?", [(path,) for path in removed])
                db.executemany("DELETE FROM plex_title_aliases WHERE path=?", [(path,) for path in removed])
                logger.info("plex_sync event=removed_media_reconciled library=%s removed=%d", library["title"].replace("\n", " "), len(removed))
        db.execute("INSERT INTO plex_sync_state(library_key,watermark,last_check,last_rebuild) VALUES(?,?,CAST(CURRENT_TIMESTAMP AS TEXT),CASE WHEN ? THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE NULL END) ON CONFLICT(library_key) DO UPDATE SET watermark=excluded.watermark,last_check=CAST(CURRENT_TIMESTAMP AS TEXT),last_rebuild=CASE WHEN ? THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE plex_sync_state.last_rebuild END", (library["library_key"], watermark, bool(rebuild), bool(rebuild)))
    if moved_paths:
        from app.v80 import migrate_index_paths
        migrated = migrate_index_paths(moved_paths)
        logger.info("plex_sync event=moved_paths_reconciled paths=%d queue_items=%d", len(moved_paths), migrated)


def clear_reviewed_for_removed_media(library_key: str, current_paths: set[str]) -> int:
    """Clear a TV show's review flag when an episode leaves the Plex catalog."""
    cleared = 0
    with plex.connection() as db:
        rows = db.execute("SELECT path,kind,show_title FROM plex_media WHERE library_key=?", (library_key,)).fetchall()
        seen = set()
        for row in rows:
            if str(row["path"]) in current_paths or str(row["path"]) in seen:
                continue
            seen.add(str(row["path"]))
            if row["kind"] != "episode":
                continue
            key = f"{library_key}:{row['show_title'] or 'Unknown show'}"
            note = db.execute("SELECT reviewed,note FROM media_notes WHERE entity_type='tv' AND entity_key=?", (key,)).fetchone()
            if not note or not note["reviewed"]:
                continue
            db.execute("UPDATE media_notes SET plex_sync_change=1,updated_at=CURRENT_TIMESTAMP WHERE entity_type='tv' AND entity_key=?", (key,))
            cleared += 1
    if cleared:
        logger.info("plex_sync event=review_status_cleared reason=media_removed library=%s shows=%d", library_key, cleared)
    return cleared


def clear_reviewed_for_changed_records(records: list[tuple]) -> int:
    """Clear collection review flags when Plex reports new or changed media.

    Movie notes are keyed by their file path; episode notes are keyed by the
    synthesized Plex show key used by the TV listing. Notes themselves remain.
    """
    cleared = 0
    with plex.connection() as db:
        for record in records:
            kind = str(record[1] or "")
            if kind == "movie":
                entity_type, entity_key = "movie", str(record[0])
            elif kind == "episode":
                entity_type, entity_key = "tv", f"{record[3]}:{record[6] or 'Unknown show'}"
            else:
                continue
            row = db.execute("SELECT reviewed,note FROM media_notes WHERE entity_type=? AND entity_key=?", (entity_type, entity_key)).fetchone()
            if not row or not row["reviewed"]:
                continue
            db.execute("UPDATE media_notes SET plex_sync_change=1,updated_at=CURRENT_TIMESTAMP WHERE entity_type=? AND entity_key=?", (entity_type, entity_key))
            cleared += 1
    if cleared:
        logger.info("plex_sync event=review_status_cleared changed_media=%d", cleared)
    return cleared


def process_plex_sync(task_id: int, payload: dict) -> dict:
    rebuild = bool(payload.get("rebuild"))
    if not plex_sync_lock.acquire(blocking=False):
        raise RuntimeError("Another Plex synchronization is already running")
    try:
        libraries = selected_libraries()
        if not libraries:
            raise RuntimeError("Select at least one Plex library")
        started = int(time.time())
        changed = 0
        catalog_records = 0
        for number, library in enumerate(libraries, 1):
            tasks.update_progress(task_id, number - 1, len(libraries), f"Checking {library['title']}")
            since = 0 if rebuild else initial_watermark(library["library_key"])
            # A full, metadata-only catalog pass is required to detect Plex
            # removals; stream/file processing remains incremental below.
            all_items = paged_library(library["library_key"], library["kind"])
            items = all_items if rebuild or not since else changed_library_items(library, since)
            records, aliases = rows_for_items(library, items)
            previous = existing_file_fingerprints(library["library_key"])
            changed_records = file_changed_records(records, previous)
            # A project-authored write is already indexed and must not be
            # treated as an external Plex update. Consume its marker before
            # reviewed-state invalidation or Plex-change reporting. A marker
            # only matches the exact size/mtime written by the project; a
            # mismatch is deliberately treated as an external change.
            internal_scopes: dict[str, tuple[dict, str]] = {}
            for record in changed_records:
                changed_path = str(record[0])
                marker = consume_internal_change_scope(changed_path, int(record[9] or 0), int(record[10] or 0))
                if marker is not None:
                    internal_scopes[changed_path] = marker
            if internal_scopes:
                changed_records = [record for record in changed_records if str(record[0]) not in internal_scopes]
                logger.info(
                    "plex_sync event=internal_project_changes_ignored files=%d",
                    len(internal_scopes),
                )
            structural_records = [
                record for record in structurally_changed_records(records, previous)
                if str(record[0]) not in internal_scopes
            ]
            clear_reviewed_for_changed_records(structural_records)
            clear_reviewed_for_removed_media(library["library_key"], catalog_paths(all_items))
            persist_library(library, records, aliases, started, rebuild, catalog_paths(all_items))
            if changed_records:
                from app.v80 import request_media_indexes
                for record in changed_records:
                    changed_path = str(record[0])
                    from app.v80 import detection_scope_for_operation
                    request_media_indexes(changed_path, ["core", "subtitles"], "Plex catalog media added or changed", detection_scope=detection_scope_for_operation("media_added_or_changed"))
                    mark_read_models_fresh(changed_path, "plex", {"modified": record[10] or 0, "size": int(record[9] or 0)})
            changed += len(changed_records)
            catalog_records += len(records)
            logger.info("plex_sync event=library_processed mode=%s library=%s items=%d catalog_items=%d media=%d file_changes=%d step=%d total=%d", "rebuild" if rebuild else "incremental", library["title"].replace("\n", "\\n"), len(items), len(all_items), len(records), len(changed_records), number, len(libraries))
        from app.v80 import prune_orphaned_index_entries
        prune_orphaned_index_entries()
        with plex.connection() as db:
            db.execute("UPDATE plex_config SET last_sync=datetime('now') WHERE id=1")
            total = db.execute("SELECT count(*) FROM plex_media").fetchone()[0]
        tasks.update_progress(task_id, len(libraries), len(libraries), "Plex catalog updated")
        logger.info("plex_sync event=completed mode=%s libraries=%d scanned_media=%d changed_media=%d catalog_media=%d", "rebuild" if rebuild else "incremental", len(libraries), catalog_records, changed, total)
        return {"mode": "rebuild" if rebuild else "incremental", "libraries": len(libraries), "changed_media": changed, "scanned_media": catalog_records, "media": total}
    finally:
        plex_sync_lock.release()


def process_subtitle_html(task_id: int, payload: dict) -> dict:
    tasks.update_progress(task_id, 0, 2, "Removing subtitle HTML tags")
    result = apply_subtitle_cleanup(SubtitleCleanup.model_validate(payload), operation_id=f"task-{task_id}")
    tasks.update_progress(task_id, 1, 2, "Queueing subtitle indexes")
    from app.v80 import request_media_indexes
    # HTML/ASS markup cleanup changes presentation only.  Preserve existing
    # subtitle language detection; subtitle indexing still refreshes HTML,
    # damage and preview metadata.
    register_internal_change_scope(result["path"], {"subtitle_indices": "all"}, "Subtitle HTML removed")
    request_media_indexes(result["path"], ["subtitles", "previews"], "Subtitle HTML removed")
    tasks.update_progress(task_id, 2, 2, "Subtitle cleanup completed")
    return result


def preflight_html_cleanup(payload: dict, fingerprint: dict) -> dict:
    """Validate HTML cleanup without creating execution work."""
    request_payload = dict(payload)
    request_payload.pop("_preflight_fingerprint", None)
    request_payload.pop("_preflight_result", None)
    request = SubtitleCleanup.model_validate(request_payload)
    if not fingerprint.get("exists"):
        return {"decision": "invalid", "reason": "Media file is not accessible"}
    if request.external_path:
        external = Path(request.external_path)
        suffix = external.suffix.lower().lstrip(".")
        if suffix not in {"srt", "ass", "ssa", "vtt", "webvtt", "sub", "txt"}:
            return {"decision": "skipped", "reason": "Only text subtitles can have markup removed"}
        if not external.is_file():
            return {"decision": "invalid", "reason": "External subtitle file is not accessible"}
        text = external.read_text(encoding="utf-8", errors="replace")
    else:
        if not fingerprint.get("exists"):
            return {"decision": "invalid", "reason": "Media file is not accessible"}
        try:
            subtitles = [stream for stream in probe(Path(request.path)).get("streams", []) if stream.get("codec_type") == "subtitle"]
            index = request.type_index if request.type_index is not None else -1
            codec = str(subtitles[index].get("codec_name") or "").lower() if 0 <= index < len(subtitles) else ""
            if codec not in TEXT_SUBTITLE_CODECS:
                return {"decision": "skipped", "reason": "Only text subtitles can have markup removed"}
            text = complete_extracted_text(Path(request.path), f"0:s:{index}")
        except Exception as exc:
            return {"decision": "invalid", "reason": f"Subtitle text could not be extracted: {exc}"}
        if not text:
            return {"decision": "skipped", "reason": "Subtitle text could not be extracted"}
    if not HTML_TAG.search(text):
        return {"decision": "skipped", "reason": "No HTML tags found"}
    return {"decision": "approved", "reason": "Text subtitle contains removable markup"}


def approve_html_cleanup(payload: dict, result: dict) -> dict:
    # Dispatcher-only fingerprints/results are validation metadata, not part of
    # SubtitleCleanup's strict request model. Strip them before creating the
    # executable child task (bulk and single-media paths).
    items = payload.get("_bulk_items")
    if items is not None:
        task_ids = []
        for item in items:
            request = dict(item)
            request.pop("_preflight_fingerprint", None)
            request.pop("_preflight_result", None)
            child = tasks.enqueue("subtitle_html_cleanup", request, "Remove subtitle HTML tags", deduplicate=True)
            task_ids.append(child["id"])
        return {"task_ids": task_ids, "queued": len(task_ids), "task_type": "subtitle_html_cleanup"}
    request = dict(payload)
    request.pop("_preflight_fingerprint", None)
    request.pop("_preflight_result", None)
    child = tasks.enqueue("subtitle_html_cleanup", request, "Remove subtitle HTML tags", deduplicate=True)
    return {"task_id": child["id"], "task_type": "subtitle_html_cleanup"}


register_handler("subtitle_html_cleanup", preflight_html_cleanup)
register_approval_handler("subtitle_html_cleanup", approve_html_cleanup)

tasks.TASK_HANDLERS["plex_sync"] = process_plex_sync
tasks.TASK_HANDLERS["subtitle_html_cleanup"] = process_subtitle_html

_TESS_LANGUAGES = {"pt": "por", "pt-br": "por", "pt-pt": "por", "en": "eng", "es": "spa", "fr": "fra", "de": "deu", "it": "ita"}

OCR_STAGE_ROOT = Path(os.environ.get("VSE_OCR_STAGE_DIR", "/config/ocr-staging"))

def _ensure_ocr_stage_table() -> None:
    with plex.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS ocr_staged_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, original_path TEXT NOT NULL, staged_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'staged', task_id INTEGER,
            size_bytes INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            converted_at TEXT, restored_at TEXT, approved_at TEXT
        )""")
        if not plex.column_exists(db, "ocr_staged_backups", "media_kind"):
            db.execute("ALTER TABLE ocr_staged_backups ADD COLUMN media_kind TEXT NOT NULL DEFAULT 'unknown'")
        db.execute("CREATE INDEX IF NOT EXISTS ocr_staged_status ON ocr_staged_backups(status, id)")
        if not plex.column_exists(db, "ocr_staged_backups", "converted_path"):
            db.execute("ALTER TABLE ocr_staged_backups ADD COLUMN converted_path TEXT")

def stage_ocr_original(path: Path, task_id: int) -> int:
    _ensure_ocr_stage_table()
    if not path.is_file():
        raise RuntimeError(f"Media is not accessible: {path}")
    OCR_STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:20]
    staged = OCR_STAGE_ROOT / f"{digest}-{path.name}"
    with plex.connection() as db:
        existing = db.execute("SELECT id,staged_path,status FROM ocr_staged_backups WHERE original_path=? AND status IN ('staged','converted') ORDER BY id DESC LIMIT 1", (str(path),)).fetchone()
        if existing and Path(existing["staged_path"]).is_file():
            return int(existing["id"])
    if not staged.exists():
        shutil.copy2(path, staged)
    media_kind = "unknown"
    with plex.connection() as db:
        media_row = db.execute("SELECT kind FROM plex_media WHERE path=? LIMIT 1", (str(path),)).fetchone()
        if media_row and media_row["kind"]:
            media_kind = "tv episode" if str(media_row["kind"]) == "episode" else str(media_row["kind"])
        # The same media can be submitted by a manual request and a queued
        # request at nearly the same time.  The path is intentionally unique,
        # so make the insert idempotent instead of allowing that race to fail
        # the OCR job.
        size_bytes = staged.stat().st_size
        db.execute(
            "INSERT OR IGNORE INTO ocr_staged_backups(original_path,staged_path,title,media_kind,status,task_id,size_bytes) VALUES(?,?,?,?,?,?,?)",
            (str(path), str(staged), path.name, media_kind, "staged", task_id, size_bytes),
        )
        row = db.execute(
            "SELECT id,status FROM ocr_staged_backups WHERE staged_path=? LIMIT 1",
            (str(staged),),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Unable to register OCR staging backup: {staged}")
        # A previously approved/restored backup may legitimately be reused
        # for a later conversion; reactivate its record rather than inserting
        # a duplicate row with the same staged_path.
        if str(row["status"]) not in {"staged", "converted"}:
            db.execute(
                "UPDATE ocr_staged_backups SET original_path=?,title=?,media_kind=?,status='staged',task_id=?,size_bytes=?,restored_at=NULL,approved_at=NULL WHERE id=?",
                (str(path), path.name, media_kind, task_id, size_bytes, int(row["id"])),
            )
        return int(row["id"])

def _ocr_stage_update(stage_id: int, status: str) -> None:
    _ensure_ocr_stage_table()
    column = "converted_at" if status == "converted" else "restored_at" if status == "restored" else "approved_at" if status == "approved" else None
    with plex.connection() as db:
        if column:
            db.execute(f"UPDATE ocr_staged_backups SET status=?,{column}=CURRENT_TIMESTAMP WHERE id=?", (status, stage_id))
        else:
            db.execute("UPDATE ocr_staged_backups SET status=? WHERE id=?", (status, stage_id))



def _ccextractor_subtitle(media: Path, type_index: int, language: str, codec: str) -> Path | None:
    """Use CCExtractor for DVB/teletext/closed-caption bitmap streams."""
    codec_key = codec.casefold().replace("_", " ")
    if not any(value in codec_key for value in ("dvb", "teletext", "eia-608", "cea-608", "cea-708", "xsub")):
        return None
    if not shutil.which("ccextractor"):
        logger.warning("ocr event=ccextractor_unavailable codec=%s", codec)
        return None
    work = Path(tempfile.mkdtemp(prefix="vse-ccx-")); source = work / "source.mkv"; output = work / "converted.srt"
    tess = _TESS_LANGUAGES.get(language.strip().casefold(), language.strip())
    try:
        # DVB pages are bitmap subtitles.  First make a DVD-sub/VobSub-style
        # intermediate when possible; this gives OCR a stable bitmap codec and
        # preserves event timing while retaining the original as fallback.
        is_dvb = "dvb" in codec_key
        remux_args = ["-map", "0:v:0", "-map", f"0:s:{type_index}", "-c:v", "copy"]
        if is_dvb:
            remux_args += ["-c:s", "dvdsub"]
            logger.info("ocr event=dvb_to_vobsub_started file=%s stream=%d", str(media).replace("\n", "\\n"), type_index)
        else:
            remux_args += ["-c", "copy"]
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media), *remux_args, str(source)], capture_output=True, text=True, timeout=900, check=True)
        except subprocess.CalledProcessError:
            if not is_dvb:
                raise
            # Some DVB variants cannot be encoded as dvdsub by FFmpeg.  Keep
            # a second, lossless MKV remux path rather than failing conversion.
            logger.warning("ocr event=dvb_to_vobsub_fallback file=%s stream=%d", str(media).replace("\n", "\\n"), type_index)
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media), "-map", "0:v:0", "-map", f"0:s:{type_index}", "-c", "copy", str(source)], capture_output=True, text=True, timeout=900, check=True)
        command = ["ccextractor", "--input", "mkv", "--out", "srt", "--ocr-line-split", "--no-progress-bar", "--ocrlang", tess, "-o", str(output), str(source)]
        subprocess.run(command, capture_output=True, text=True, timeout=3600, check=True)
        if output.is_file() and output.stat().st_size > 0:
            raw = output.read_text(encoding="utf-8", errors="replace")
            cues = len(re.findall(r"(?m)^\d+\s*$", raw))
            letters = len(re.findall(r"[A-Za-zÀ-ÿ]", raw))
            if cues and letters >= max(8, cues * 2):
                logger.info("ocr event=ccextractor_completed file=%s stream=%d codec=%s vobsub_intermediate=%s cues=%d", str(media).replace("\n", "\\n"), type_index, codec, is_dvb, cues)
                return output
            logger.warning("ocr event=ccextractor_rejected file=%s stream=%d reason=low_quality cues=%d letters=%d", str(media).replace("\n", "\\n"), type_index, cues, letters)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("ocr event=ccextractor_failed file=%s stream=%d error=%s", str(media).replace("\n", "\\n"), type_index, str(exc).replace("\n", " ")[-300:])
    shutil.rmtree(work, ignore_errors=True)
    return None


def _bdsup2sub_normalized(media: Path, type_index: int, codec: str) -> Path | None:
    """Extract and normalize a bitmap subtitle with BDSup2Sub when available."""
    jar = Path("/opt/bdsup2sub.jar")
    if not jar.is_file() or codec.casefold() not in {"hdmv pgs", "hdmv_pgs_subtitle", "pgs", "vobsub", "dvd_subtitle"}:
        return None
    work = Path(tempfile.mkdtemp(prefix="vse-bdsup-"))
    source = work / ("source.sup" if "pgs" in codec.casefold() or "hdmv" in codec.casefold() else "source.sub")
    target = work / "normalized.sup"
    try:
        # mkvextract preserves VobSub sidecars (.idx/.sub) and PGS packets
        # more reliably than asking FFmpeg to write a standalone bitmap file.
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"s:{type_index}", "-show_entries", "stream=index", "-of", "csv=p=0", str(media)], capture_output=True, text=True, timeout=60, check=True)
        global_index = probe.stdout.strip().splitlines()[0]
        subprocess.run(["mkvextract", "tracks", str(media), f"{global_index}:{source}"], capture_output=True, text=True, timeout=300, check=True)
        subprocess.run(["java", "-Djava.awt.headless=true", "-jar", str(jar), "-r", "keep", "-f", "lanczos3", "-S", "2,2", "-o", str(target), str(source)], capture_output=True, text=True, timeout=600, check=True)
        logger.info("ocr event=bdsup2sub_normalized file=%s stream=%d codec=%s", str(media).replace("\n", "\\n"), type_index, codec)
        return target
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("ocr event=bdsup2sub_failed file=%s stream=%d error=%s", str(media).replace("\n", "\\n"), type_index, str(exc).replace("\n", " ")[-300:])
        shutil.rmtree(work, ignore_errors=True)
        return None


def _vobsub_to_srt(media: Path, type_index: int, language: str) -> Path:
    """Extract a real VobSub track and convert its IDX/SUB pair with VobSub2SRT."""
    if not shutil.which("vobsub2srt"):
        raise RuntimeError("VobSub conversion is unavailable: vobsub2srt is not installed")
    work = Path(tempfile.mkdtemp(prefix="vse-vobsub-"))
    basename = work / "subtitle"
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"s:{type_index}", "-show_entries", "stream=index", "-of", "csv=p=0", str(media)], capture_output=True, text=True, timeout=60, check=True)
        lines = probe.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError("VobSub track could not be located")
        global_index = lines[0].strip()
        subprocess.run(["mkvextract", "tracks", str(media), f"{global_index}:{basename}.sub"], capture_output=True, text=True, timeout=300, check=True)
        tess = _TESS_LANGUAGES.get(language.strip().casefold(), language.strip())
        command = ["vobsub2srt", str(basename)]
        if tess and tess.casefold() not in {"und", "unknown"}:
            command += ["--tesseract-lang", tess]
        subprocess.run(command, capture_output=True, text=True, timeout=3600, check=True)
        output = basename.with_suffix(".srt")
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("VobSub2SRT produced no subtitle text")
        final = media.with_name(f".{media.stem}.vse-ocr.srt")
        shutil.copyfile(output, final)
        return final
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().replace("\n", " ")[-400:]
        raise RuntimeError(f"VobSub conversion failed: {detail or 'vobsub2srt error'}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _image_codec_family(codec: str) -> str:
    key = str(codec or "").casefold().replace("_", " ").replace("/", " ")
    if "vobsub" in key or "dvd subtitle" in key or key in {"s vobsub", "idx"}:
        return "vobsub"
    if "dvb" in key:
        return "dvb"
    if "pgs" in key or "hdmv" in key or key in {"sup", "s hdmv pgs"}:
        return "pgs"
    if "xsub" in key:
        return "xsub"
    return "unsupported"

def _ocr_graphical_subtitle(media: Path, type_index: int, language: str) -> Path:
    if type_index < 0:
        raise RuntimeError("External image subtitle conversion is not supported without a timed container stream")
    tess = _TESS_LANGUAGES.get(language.strip().casefold(), language.strip())
    if not tess or tess.casefold() in {"und", "unknown"}:
        raise RuntimeError("Image subtitle language is not set or is und")
    normalized_subtitle = None
    cce_subtitle = None
    try:
        stream_info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"s:{type_index}", "-show_entries", "stream=codec_long_name,codec_name", "-of", "json", str(media)], capture_output=True, text=True, timeout=60, check=True)
        stream_rows = json.loads(stream_info.stdout).get("streams", [])
        if stream_rows:
            codec = str(stream_rows[0].get("codec_long_name") or stream_rows[0].get("codec_name") or "")
            family = _image_codec_family(codec)
            if family == "vobsub":
                return _vobsub_to_srt(media, type_index, language)
            if family == "dvb":
                cce_subtitle = _ccextractor_subtitle(media, type_index, language, codec)
                if not cce_subtitle:
                    raise RuntimeError("DVB subtitle conversion is unavailable or failed; no fallback conversion was attempted")
                return cce_subtitle
            if family == "xsub":
                raise RuntimeError("XSub conversion is not supported by the selected OCR routine")
            if family != "pgs":
                raise RuntimeError(f"Unsupported image subtitle codec: {codec}")
            normalized_subtitle = _bdsup2sub_normalized(media, type_index, codec)
            if not normalized_subtitle:
                raise RuntimeError("PGS conversion requires a working BDSup2Sub installation")
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        normalized_subtitle = None
    if cce_subtitle:
        return cce_subtitle
    try:
        packets = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"s:{type_index}", "-show_entries", "packet=pts_time", "-of", "json", str(media)], capture_output=True, text=True, timeout=120, check=True)
        timestamps = [float(item["pts_time"]) for item in json.loads(packets.stdout).get("packets", []) if item.get("pts_time") is not None]
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not locate image subtitle events") from exc
    if not timestamps:
        raise RuntimeError("Image subtitle contains no timed events")
    entries = []
    for number, timestamp in enumerate(timestamps):
        end = timestamps[number + 1] if number + 1 < len(timestamps) else timestamp + 4.0
        # Render only the usual subtitle area and enlarge it before OCR.  OCR
        # over the complete movie frame also recognizes scenery, credits and
        # logos, producing the unintelligible text seen in early conversions.
        render_filter = (
            f"[0:v:0][0:s:{type_index}]overlay="
            f"x=(W-w)/2:y=(H-h)/2,"
            "crop=in_w:in_h*0.50:0:in_h*0.50,"
            "scale=iw*2:ih*2:flags=lanczos,format=gray,"
            "eq=contrast=1.8:brightness=0.05"
        )
        if normalized_subtitle:
            frame_command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, timestamp):.3f}", "-i", str(media), "-i", str(normalized_subtitle), "-filter_complex", render_filter.replace(f"[0:v:0][0:s:{type_index}]", "[0:v:0][1:s:0]"), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
        else:
            frame_command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, timestamp):.3f}", "-i", str(media), "-filter_complex", render_filter, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
        frame = subprocess.run(frame_command, capture_output=True, timeout=120, check=True)
        try:
            # ffmpeg emits a binary PNG frame.  Keep the OCR subprocess in
            # binary mode; ``text=True`` makes subprocess try to encode the
            # bytes input and raises: "bytes object has no attribute encode".
            ocr = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", tess, "--psm", "6", "-c", "preserve_interword_spaces=1"],
                input=frame.stdout,
                capture_output=True,
                text=False,
                timeout=30,
                check=True,
            )
            text = ocr.stdout.decode("utf-8", errors="replace").strip()
        except FileNotFoundError as exc:
            raise RuntimeError("Tesseract OCR is not installed in the container") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"OCR failed for language {language}") from exc
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            def stamp(value):
                hours, rest = divmod(value, 3600); minutes, seconds_float = divmod(rest, 60); seconds = int(seconds_float); millis = round((seconds_float - seconds) * 1000)
                if millis >= 1000: seconds += 1; millis -= 1000
                return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
            entries.append(f"{len(entries)+1}\n{stamp(timestamp)} --> {stamp(max(timestamp + .2, end))}\n{text}\n")
    if normalized_subtitle:
        shutil.rmtree(normalized_subtitle.parent, ignore_errors=True)
    if not entries:
        raise RuntimeError("OCR produced no subtitle text")
    output = media.with_name(f".{media.stem}.vse-ocr.srt")
    output.write_text("\n".join(entries), encoding="utf-8")
    return output

def _resolve_ocr_language(payload: dict) -> tuple[str, float, str]:
    """Resolve a Tesseract language from Plex metadata or a trusted preflight result.

    Plex-normalized metadata is treated as authoritative for the first OCR pass.
    A future detector may provide detected_language/language_confidence; those
    values are accepted only when confidence is at least 0.80.
    """
    metadata = str(payload.get("language") or "").strip()
    detected = str(payload.get("detected_language") or "").strip()
    try:
        confidence = float(payload.get("language_confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    invalid = {"", "und", "unknown", "zxx", "mis"}
    if metadata.casefold() not in invalid:
        return metadata, 1.0, "Plex stream language metadata"
    if detected.casefold() not in invalid and confidence >= 0.80:
        return detected, confidence, "OCR language preflight"
    raise RuntimeError("Image subtitle language could not be determined with at least 80% confidence; conversion was not started")


def process_image_subtitle_convert(task_id: int, payload: dict) -> dict:
    media = Path(str(payload.get("path") or "")).resolve()
    if not media.is_file():
        raise RuntimeError(f"Media is not accessible: {media}")
    language, language_confidence, language_source = _resolve_ocr_language(payload)
    tasks.update_progress(task_id, 0, 4, "Validating subtitle language before OCR")
    subtitle = _ocr_graphical_subtitle(media, int(payload.get("type_index", -1)), language)
    # Preserve the complete original container immediately before remuxing. A
    # full copy restores every track, attachment, chapter, timing, and tag.
    stage_id = stage_ocr_original(media, task_id)
    tasks.update_progress(task_id, 1, 4, "Graphical subtitle recognized; original staged")
    tasks.update_progress(task_id, 2, 4, "Replacing graphical subtitle with OCR text")
    streams = indexes.probe(media).get("streams", []) if hasattr(indexes, "probe") else []
    if not streams:
        from app.v2 import probe
        streams = probe(media).get("streams", [])
    subtitle_globals = [i for i, stream in enumerate(streams) if stream.get("codec_type") == "subtitle"]
    selected = subtitle_globals[int(payload.get("type_index", -1))] if 0 <= int(payload.get("type_index", -1)) < len(subtitle_globals) else -1
    if selected < 0:
        subtitle.unlink(missing_ok=True); raise RuntimeError("Subtitle stream was not found")
    temporary = media.with_name(f".{media.stem}.vse-ocr-{task_id}{media.suffix}")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media), "-i", str(subtitle)]
    subtitle_output = 0
    for global_index, stream in enumerate(streams):
        command += ["-map", "1:0" if global_index == selected else f"0:{global_index}"]
        if stream.get("codec_type") == "subtitle":
            if global_index == selected: command += [f"-c:s:{subtitle_output}", "srt"]
            subtitle_output += 1
    command += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy", f"-metadata:s:s:{int(payload.get('type_index', 0))}", f"language={language}", str(temporary)]
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=3600, check=True)
        os.chmod(temporary, media.stat().st_mode); os.replace(temporary, media)
        _ocr_stage_update(stage_id, "converted")
        with plex.connection() as db:
            db.execute("UPDATE ocr_staged_backups SET converted_path=? WHERE id=?", (str(media), stage_id))
    finally:
        subtitle.unlink(missing_ok=True); temporary.unlink(missing_ok=True)
        if subtitle.parent.name.startswith("vse-ccx-"):
            shutil.rmtree(subtitle.parent, ignore_errors=True)
    tasks.update_progress(task_id, 3, 4, "Queueing subtitle indexes")
    from app.v80 import detection_scope_for_operation, request_media_indexes
    request_media_indexes(str(media), ["subtitles", "core"], "Image subtitle converted to SRT", detection_scope=detection_scope_for_operation("subtitle_conversion"))
    tasks.update_progress(task_id, 4, 4, "Image subtitle conversion completed")
    return {"path": str(media), "type_index": int(payload.get("type_index", -1)), "language": language, "language_confidence": language_confidence, "language_source": language_source}

tasks.TASK_HANDLERS["image_subtitle_convert"] = process_image_subtitle_convert


# Dispatcher-backed image subtitle conversion.  Validation is deliberately
# lightweight and non-destructive; the existing handler remains responsible
# for staging the original container before replacement.
def preflight_image_convert(payload: dict, fingerprint: dict) -> dict:
    path = str(payload.get("path") or "")
    if not fingerprint.get("exists"):
        return {"decision": "invalid", "reason": "Media file is not accessible", "path": path}
    if str(payload.get("source") or "embedded") != "embedded":
        return {"decision": "skipped", "reason": "External image subtitles are not supported by the staged OCR converter", "path": path}
    language = str(payload.get("language") or "").strip()
    if not language or language.casefold() in {"und", "unknown", "zxx", "mis"}:
        return {"decision": "skipped", "reason": "Image subtitle language is not set", "path": path}
    try:
        streams = probe(Path(path)).get("streams", [])
        subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
        index = int(payload.get("type_index", -1))
        if index < 0 or index >= len(subtitles):
            return {"decision": "stale", "reason": "Subtitle stream no longer exists", "path": path}
        codec = str(subtitles[index].get("codec_name") or subtitles[index].get("codec_tag_string") or "").casefold()
        if codec not in {"dvd_subtitle", "dvb_subtitle", "hdmv_pgs_subtitle", "pgssub", "pgs", "vobsub", "xsub", "sup", "s_hdmv/pgs", "s_vobsub", "s_dvbsub"}:
            return {"decision": "skipped", "reason": "Subtitle is no longer a graphical format", "path": path, "codec": codec}
    except Exception as exc:
        return {"decision": "invalid", "reason": f"Could not inspect subtitle stream: {exc}", "path": path}
    return {"decision": "approved", "path": path, "language": language, "fingerprint": fingerprint}

def approve_image_convert_bulk(payload: dict, result: dict) -> dict:
    child_ids = []
    for item in payload.get("_bulk_items", []):
        request = dict(item)
        request.pop("_preflight_fingerprint", None)
        request.pop("_preflight_result", None)
        child = tasks.enqueue("image_subtitle_convert", request, "Convert image subtitle to SRT", deduplicate=True)
        if child and child.get("id") is not None:
            child_ids.append(int(child["id"]))
    return {"task_ids": child_ids, "queued": len(child_ids), "task_type": "image_subtitle_convert"}

register_handler("image_subtitle_convert_bulk", preflight_image_convert)
register_approval_handler("image_subtitle_convert_bulk", approve_image_convert_bulk)


@app.get("/api/v68/ocr/staged")
def list_ocr_staged() -> dict:
    _ensure_ocr_stage_table()
    reconcile_startup_artifacts()
    with plex.connection() as db:
        rows = db.execute("SELECT * FROM ocr_staged_backups WHERE status IN ('staged','converted','restored') ORDER BY id DESC").fetchall()
    return {"stage_root": str(OCR_STAGE_ROOT), "items": [dict(row) for row in rows]}

@app.post("/api/v68/ocr/staged/{stage_id}/approve")
def approve_ocr_staged(stage_id: int) -> dict:
    _ensure_ocr_stage_table()
    with plex.connection() as db:
        row = db.execute("SELECT * FROM ocr_staged_backups WHERE id=? AND status IN ('staged','converted','restored')", (stage_id,)).fetchone()
    if not row:
        raise HTTPException(404, "OCR staging record not found")
    staged = Path(row["staged_path"]).resolve()
    if staged.parent != OCR_STAGE_ROOT.resolve() or not staged.is_file():
        raise HTTPException(404, "Staged original file is missing")
    staged.unlink()
    converted = Path(str(row["converted_path"] or "")) if row["converted_path"] else None
    if converted and converted.is_file() and converted.parent == OCR_STAGE_ROOT.resolve():
        converted.unlink()
    with plex.connection() as db:
        db.execute("DELETE FROM ocr_staged_backups WHERE id=?", (stage_id,))
    logger.info("ocr_staging event=approved id=%d original=%s", stage_id, row["original_path"])
    return {"id": stage_id, "status": "approved"}

@app.get("/api/v68/ocr/staged/{stage_id}/converted")
def download_ocr_converted(stage_id: int) -> FileResponse:
    _ensure_ocr_stage_table()
    with plex.connection() as db:
        row = db.execute("SELECT converted_path,title,original_path FROM ocr_staged_backups WHERE id=? AND status IN ('converted','restored')", (stage_id,)).fetchone()
    if not row or not row["converted_path"]:
        raise HTTPException(404, "No preserved converted artifact is available")
    artifact = Path(str(row["converted_path"])).resolve()
    original = Path(str(row["original_path"])).resolve()
    if artifact != original and artifact.parent != OCR_STAGE_ROOT.resolve():
        raise HTTPException(404, "Preserved converted artifact is missing")
    if not artifact.is_file():
        raise HTTPException(404, "Preserved converted artifact is missing")
    return FileResponse(artifact, media_type="video/x-matroska", filename=artifact.name)

@app.post("/api/v68/ocr/staged/{stage_id}/finalize")
def finalize_ocr_converted(stage_id: int) -> dict:
    """Put the converted candidate at the media's final/original path, retaining the staged original for rollback."""
    _ensure_ocr_stage_table()
    with plex.connection() as db:
        row = db.execute("SELECT * FROM ocr_staged_backups WHERE id=? AND status IN ('converted','restored')", (stage_id,)).fetchone()
    if not row:
        raise HTTPException(404, "OCR staging record not found")
    original = Path(row["original_path"]).resolve()
    artifact = Path(str(row["converted_path"] or "")).resolve() if row["converted_path"] else None
    staged = Path(row["staged_path"]).resolve()
    if staged.parent != OCR_STAGE_ROOT.resolve() or not staged.is_file():
        raise HTTPException(404, "Staged original backup is missing")
    if not original.parent.is_dir():
        raise HTTPException(409, "Original media folder is unavailable")
    if artifact == original and original.is_file():
        return {"id": stage_id, "status": "converted", "path": str(original), "already_final": True}
    if not artifact or artifact.parent != OCR_STAGE_ROOT.resolve() or not artifact.is_file():
        raise HTTPException(404, "Preserved converted candidate is missing")
    temporary = original.with_name(f".{original.name}.vse-finalize")
    try:
        shutil.copyfile(artifact, temporary)
        os.chmod(temporary, original.stat().st_mode if original.exists() else staged.stat().st_mode)
        os.replace(temporary, original)
    finally:
        temporary.unlink(missing_ok=True)
    # The candidate now lives at its final media path. Keep only the original
    # rollback copy in staging; approval will remove that copy and its record.
    artifact.unlink()
    with plex.connection() as db:
        db.execute("UPDATE ocr_staged_backups SET status='converted',converted_path=?,size_bytes=? WHERE id=?", (str(original), staged.stat().st_size, stage_id))
    from app.v80 import detection_scope_for_operation, request_media_indexes
    request_media_indexes(str(original), ["subtitles", "core", "previews"], "OCR converted candidate moved to final media path", detection_scope=detection_scope_for_operation("subtitle_conversion"))
    logger.info("ocr_staging event=converted_finalized id=%d original=%s", stage_id, original)
    return {"id": stage_id, "status": "converted", "path": str(original)}

def perform_ocr_rollback(stage_id: int, task_id: int | None = None) -> dict:
    _ensure_ocr_stage_table()
    with plex.connection() as db:
        row = db.execute("SELECT * FROM ocr_staged_backups WHERE id=? AND status IN ('staged','converted','restored')", (stage_id,)).fetchone()
    if not row:
        raise RuntimeError("OCR staging record not found")
    staged = Path(row["staged_path"]).resolve(); original = Path(row["original_path"]).resolve()
    if staged.parent != OCR_STAGE_ROOT.resolve() or not staged.is_file():
        raise RuntimeError("Staged original file is missing")
    if not original.parent.is_dir():
        raise RuntimeError("Original media folder is unavailable")
    if str(row["status"]) == "restored" and original.is_file():
        return {"id": stage_id, "status": "restored", "path": str(original), "already_restored": True}
    converted_snapshot = Path(str(row["converted_path"] or "")) if row["converted_path"] else OCR_STAGE_ROOT / f"{stage_id}-converted-{original.name}"
    if converted_snapshot.parent != OCR_STAGE_ROOT.resolve():
        converted_snapshot = OCR_STAGE_ROOT / f"{stage_id}-converted-{original.name}"
    if original.is_file() and converted_snapshot != original and not converted_snapshot.exists():
        shutil.copy2(original, converted_snapshot)
    token = str(task_id or uuid.uuid4().hex)
    temporary = original.with_name(f".{original.name}.vse-rollback-{token}")
    try:
        shutil.copyfile(staged, temporary)
        os.chmod(temporary, original.stat().st_mode if original.exists() else staged.stat().st_mode)
        os.replace(temporary, original)
    finally:
        temporary.unlink(missing_ok=True)
    _ocr_stage_update(stage_id, "restored")
    with plex.connection() as db:
        db.execute("UPDATE ocr_staged_backups SET converted_path=? WHERE id=?", (str(converted_snapshot), stage_id))
    from app.v80 import detection_scope_for_operation, request_media_indexes
    request_media_indexes(str(original), ["subtitles", "core", "previews"], "OCR original restored", detection_scope=detection_scope_for_operation("ocr_restore"))
    logger.info("ocr_staging event=rollback id=%d original=%s", stage_id, original)
    return {"id": stage_id, "status": "restored", "path": str(original)}


def process_ocr_rollback(task_id: int, payload: dict) -> dict:
    tasks.update_progress(task_id, 0, 2, "Restoring staged original media")
    result = perform_ocr_rollback(int(payload.get("stage_id", 0)), task_id=task_id)
    tasks.update_progress(task_id, 1, 2, "Refreshing media indexes")
    tasks.update_progress(task_id, 2, 2, "Rollback completed")
    return result


tasks.TASK_HANDLERS["ocr_rollback"] = process_ocr_rollback

@app.post("/api/v68/ocr/staged/{stage_id}/rollback")
def rollback_ocr_staged(stage_id: int) -> dict:
    task = tasks.enqueue("ocr_rollback", {"stage_id": stage_id}, f"Rollback OCR staging #{stage_id}", deduplicate=True)
    logger.info("ocr_staging event=rollback_queued id=%d task=%d", stage_id, task["id"])
    return {"queued": True, "task_id": task["id"], "stage_id": stage_id, "status": "pending"}


def plex_schedule_data() -> dict:
    with plex.connection() as db:
        row = db.execute("SELECT frequency,interval_value,time_of_day,last_run FROM plex_sync_schedule WHERE id=1").fetchone()
    value = dict(row)
    value["time"] = value.pop("time_of_day")
    value["interval"] = value.pop("interval_value")
    value["next_run"] = next_plex_run(value)
    return value


def next_plex_run(value: dict) -> str | None:
    frequency = value["frequency"]
    if frequency == "disabled":
        return None
    now = datetime.now().astimezone()
    last = datetime.fromisoformat(value["last_run"]).astimezone() if value.get("last_run") else now
    if frequency in {"minutes", "hours"}:
        delta = timedelta(**{frequency: value["interval"]})
        return (last + delta).isoformat(timespec="minutes")
    hour, minute = parse_clock(value["time"])
    candidate = last.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=DAY_INTERVALS[frequency])
    return candidate.isoformat(timespec="minutes")


def plex_schedule_due(value: dict) -> bool:
    next_run = next_plex_run(value)
    return bool(next_run and datetime.fromisoformat(next_run) <= datetime.now().astimezone())


def run_plex_scheduler() -> None:
    logger.info("plex_scheduler event=worker_started")
    while True:
        try:
            value = plex_schedule_data()
            if plex_schedule_due(value):
                tasks.enqueue("plex_sync", {"rebuild": False, "source": "schedule"}, "Scheduled Plex incremental check", deduplicate=True)
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                with plex.connection() as db:
                    db.execute("UPDATE plex_sync_schedule SET last_run=?,updated_at=CURRENT_TIMESTAMP WHERE id=1", (now,))
                logger.info("plex_scheduler event=incremental_check_queued frequency=%s interval=%d", value["frequency"], value["interval"])
        except Exception as exc:
            logger.warning("plex_scheduler event=check_failed error=%s", str(exc).replace("\n", " ")[-500:])
        threading.Event().wait(30)


def reconcile_startup_artifacts() -> None:
    """Report unresolved staging and remove only stale app-owned remux artifacts."""
    _ensure_ocr_stage_table()
    with plex.connection() as db:
        stages = db.execute("SELECT status,count(*) AS n FROM ocr_staged_backups WHERE status IN ('staged','converted','restored') GROUP BY status").fetchall()
        rows = db.execute("SELECT payload_json FROM task_queue WHERE status IN ('pending','running','failed')").fetchall()
    parent_dirs = set()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        for key in ("path", "source"):
            value = str(payload.get(key) or (payload.get("edit") or {}).get(key) or "")
            if value:
                parent_dirs.add(str(Path(value).resolve().parent))
    patterns = (".*.subtitle-clean.vse-*.mkv", ".*.subtitle-clean.vse-*.mp4", ".*.subtitle-clean.vse.mkv", ".*.subtitle-clean.vse.mp4", ".*.vse-ocr-*.mkv", ".*.vse-ocr-*.mp4", ".*.vse-ocr.mkv", ".*.vse-ocr.mp4", ".*.vse-rollback-*", ".*.vse-rollback", ".*.vse-finalize", ".*.vse-*.tmp")
    active_payload = " ".join(str(row["payload_json"] or "") for row in rows)
    removed = 0
    cutoff = time.time() - 600
    for directory in parent_dirs:
        folder = Path(directory)
        if not folder.is_dir():
            continue
        for pattern in patterns:
            for artifact in folder.glob(pattern):
                try:
                    if artifact.is_file() and artifact.stat().st_mtime < cutoff and str(artifact) not in active_payload:
                        artifact.unlink(); removed += 1
                except OSError as exc:
                    logger.warning("startup_recovery event=temporary_cleanup_failed path=%s error=%s", artifact, str(exc).replace("\\n", " ")[-300:])
    unresolved = ",".join(f"{row['status']}={row['n']}" for row in stages) or "none"
    logger.info("startup_recovery event=checked unresolved_ocr_staging=%s stale_temp_removed=%d", unresolved, removed)


@app.on_event("startup")
def initialize_incremental_plex_sync() -> None:
    global plex_schedule_thread
    _ensure_ocr_stage_table()
    reconcile_startup_artifacts()
    with plex.connection() as db:
        for column, statement in (("plex_added_at", "ALTER TABLE plex_media ADD COLUMN plex_added_at INTEGER NOT NULL DEFAULT 0"), ("plex_updated_at", "ALTER TABLE plex_media ADD COLUMN plex_updated_at INTEGER NOT NULL DEFAULT 0")):
            if not plex.column_exists(db, "plex_media", column):
                db.execute(statement)
        # PostgreSQL INTEGER is 32-bit; media byte sizes and Plex timestamps
        # can exceed that range. Widen catalog fingerprint columns before the
        # next incremental sync. SQLite keeps its existing compatibility path.
        if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres":
            for column in ("size", "modified", "plex_added_at", "plex_updated_at"):
                try:
                    db.execute(f"ALTER TABLE plex_media ALTER COLUMN {column} TYPE BIGINT")
                except Exception as exc:
                    logger.warning("plex_sync event=column_width_migration_failed column=%s error=%s", column, str(exc).replace("\n", " ")[-300:])
        db.executescript("""
            CREATE TABLE IF NOT EXISTS plex_sync_state (
                library_key TEXT PRIMARY KEY, watermark INTEGER NOT NULL DEFAULT 0,
                last_check TEXT, last_rebuild TEXT
            );
            CREATE TABLE IF NOT EXISTS plex_sync_schedule (
                id INTEGER PRIMARY KEY CHECK(id=1), frequency TEXT NOT NULL DEFAULT 'disabled',
                interval_value INTEGER NOT NULL DEFAULT 30, time_of_day TEXT NOT NULL DEFAULT '03:00',
                last_run TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO plex_sync_schedule(id) VALUES(1);
        """)


def start_plex_scheduler() -> None:
    global plex_schedule_thread
    if not plex_schedule_thread or not plex_schedule_thread.is_alive():
        plex_schedule_thread = threading.Thread(target=run_plex_scheduler, name="vse-plex-scheduler", daemon=True)
        plex_schedule_thread.start()


@app.post("/api/v68/plex/check")
def queue_plex_check() -> dict:
    return tasks.enqueue("plex_sync", {"rebuild": False, "source": "manual"}, "Plex incremental check", deduplicate=True)


@app.post("/api/v68/plex/rebuild")
def queue_plex_rebuild() -> dict:
    return tasks.enqueue("plex_sync", {"rebuild": True, "source": "manual"}, "Rebuild Plex catalog", deduplicate=True)


@app.get("/api/v68/plex/schedule")
def get_plex_schedule() -> dict:
    return plex_schedule_data()


@app.put("/api/v68/plex/schedule")
def update_plex_schedule(request: PlexSyncSchedule) -> dict:
    parse_clock(request.time)
    local_now = datetime.now().astimezone()
    last_run = None
    if request.frequency in {"minutes", "hours"}:
        last_run = local_now.isoformat(timespec="seconds")
    elif request.frequency in DAY_INTERVALS:
        hour, minute = parse_clock(request.time)
        baseline = local_now if (local_now.hour, local_now.minute) >= (hour, minute) else local_now - timedelta(days=DAY_INTERVALS[request.frequency])
        last_run = baseline.isoformat(timespec="seconds")
    with plex.connection() as db:
        db.execute("UPDATE plex_sync_schedule SET frequency=?,interval_value=?,time_of_day=?,last_run=?,updated_at=CURRENT_TIMESTAMP WHERE id=1", (request.frequency, request.interval, request.time, last_run))
    logger.info("plex_scheduler event=schedule_changed frequency=%s interval=%d time=%s", request.frequency, request.interval, request.time)
    return plex_schedule_data()


@app.post("/api/v68/subtitle-html-cleanup")
def queue_subtitle_html_cleanup(request: SubtitleCleanup) -> dict:
    name = Path(request.external_path).name if request.external_path else f"subtitle {request.type_index}"
    payload = request.model_dump()
    return enqueue_preflight("subtitle_html_cleanup", request.path, payload, mode="queued", priority=70)
