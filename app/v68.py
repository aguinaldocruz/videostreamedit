from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v11 as plex
import app.v19 as titles
import app.v54 as indexes
import app.v65 as tasks
from app.v51 import SubtitleCleanup, apply_subtitle_cleanup
from app.v67 import app


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


def file_changed_records(records: list[tuple], previous: dict[str, tuple[int, int]]) -> list[tuple]:
    """Select new, moved, or content-changed files; ignore Plex-only metadata changes."""
    return [
        record for record in records
        if previous.get(str(record[0])) != (int(record[9] or 0), int(record[10] or 0))
    ]


def persist_library(library: dict, records: list[tuple], aliases: list[tuple], watermark: int, rebuild: bool) -> None:
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
        db.execute("INSERT INTO plex_sync_state(library_key,watermark,last_check,last_rebuild) VALUES(?,?,CURRENT_TIMESTAMP,CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END) ON CONFLICT(library_key) DO UPDATE SET watermark=excluded.watermark,last_check=CURRENT_TIMESTAMP,last_rebuild=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE plex_sync_state.last_rebuild END", (library["library_key"], watermark, int(rebuild), int(rebuild)))
    if moved_paths:
        from app.v80 import migrate_index_paths
        migrated = migrate_index_paths(moved_paths)
        logger.info("plex_sync event=moved_paths_reconciled paths=%d queue_items=%d", len(moved_paths), migrated)


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
            items = paged_library(library["library_key"], library["kind"]) if rebuild or not since else changed_library_items(library, since)
            records, aliases = rows_for_items(library, items)
            previous = existing_file_fingerprints(library["library_key"])
            changed_records = file_changed_records(records, previous)
            persist_library(library, records, aliases, started, rebuild)
            if changed_records:
                from app.v80 import request_media_indexes
                for record in changed_records:
                    request_media_indexes(str(record[0]), ["core", "subtitles", "previews"], "Plex catalog media added or changed")
            changed += len(changed_records)
            catalog_records += len(records)
            logger.info("plex_sync event=library_processed mode=%s library=%s items=%d media=%d file_changes=%d step=%d total=%d", "rebuild" if rebuild else "incremental", library["title"].replace("\n", "\\n"), len(items), len(records), len(changed_records), number, len(libraries))
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
    result = apply_subtitle_cleanup(SubtitleCleanup.model_validate(payload))
    tasks.update_progress(task_id, 1, 2, "Queueing subtitle indexes")
    from app.v80 import request_media_indexes
    request_media_indexes(result["path"], ["subtitles", "previews"], "Subtitle HTML removed")
    tasks.update_progress(task_id, 2, 2, "Subtitle cleanup completed")
    return result


tasks.TASK_HANDLERS["plex_sync"] = process_plex_sync
tasks.TASK_HANDLERS["subtitle_html_cleanup"] = process_subtitle_html


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


@app.on_event("startup")
def initialize_incremental_plex_sync() -> None:
    global plex_schedule_thread
    with plex.connection() as db:
        for statement in ("ALTER TABLE plex_media ADD COLUMN plex_added_at INTEGER NOT NULL DEFAULT 0", "ALTER TABLE plex_media ADD COLUMN plex_updated_at INTEGER NOT NULL DEFAULT 0"):
            try:
                db.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
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
    return tasks.enqueue("subtitle_html_cleanup", request.model_dump(), f"Remove HTML tags from {name}")
