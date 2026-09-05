from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import app.v54 as jobs
from app.v2 import probe
from app.v11 import connection
from app.v28 import authorized_import_file
from app.v55 import cached_subtitle_preview
from app.v59 import app


logger = logging.getLogger("uvicorn.error")
DEFAULT_CACHE_LIMIT = 512 * 1024**2
control_events = {name: threading.Event() for name in jobs.JOBS}
stop_events = {name: threading.Event() for name in jobs.JOBS}
for event in control_events.values():
    event.set()


class CacheLimit(BaseModel):
    gigabytes: float = Field(ge=0.25, le=100)


@app.on_event("startup")
def migrate_efficient_preview_cache() -> None:
    jobs.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS preview_cache_files (
                path TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
                last_access REAL NOT NULL, prewarmed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(path,filename)
            );
            CREATE TABLE IF NOT EXISTS index_job_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO index_job_settings(key,value) VALUES('preview_cache_limit_bytes','536870912');
            CREATE TABLE IF NOT EXISTS feature_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        db.execute("UPDATE index_job_settings SET value='536870912' WHERE key='preview_cache_limit_bytes' AND value='5368709120'")
        migrated = db.execute("SELECT 1 FROM feature_migrations WHERE name='preview_cache_policy_v2'").fetchone()
        if not migrated:
            db.execute("DELETE FROM preview_cache_index")
            db.execute("DELETE FROM preview_cache_files")
            db.execute("INSERT INTO feature_migrations(name) VALUES('preview_cache_policy_v2')")
    if not migrated:
        shutil.rmtree(jobs.CACHE_DIR, ignore_errors=True)
        jobs.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("index_job=previews event=cache_policy_migrated policy=first_25_seconds_64k_mono old_cache=cleared")


def cache_limit() -> int:
    with connection() as db:
        row = db.execute("SELECT value FROM index_job_settings WHERE key='preview_cache_limit_bytes'").fetchone()
    return int(row[0]) if row else DEFAULT_CACHE_LIMIT


def encode_audio(path: Path, type_index: int, segment: int) -> bytes:
    try:
        result = subprocess.run([
            "nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", str(segment * 25), "-i", str(path), "-map", f"0:a:{type_index}",
            "-vn", "-t", "25", "-ac", "1", "-ar", "32000", "-b:a", "64k", "-f", "mp3", "pipe:1",
        ], capture_output=True, timeout=120, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes): detail = detail.decode("utf-8", errors="replace")
        raise HTTPException(422, (detail or "Could not create audio preview")[-1200:]) from exc
    return result.stdout


def register_file(path: str, file: Path, prewarmed: bool) -> None:
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO preview_cache_files(path,filename,size,last_access,prewarmed) VALUES(?,?,?,?,?)", (path, file.name, file.stat().st_size, time.time(), int(prewarmed)))


def enforce_lru() -> None:
    limit = cache_limit()
    cutoff = time.time() - 7 * 86400
    removed_files = removed_bytes = 0
    with connection() as db:
        total = db.execute("SELECT coalesce(sum(size),0) FROM preview_cache_files").fetchone()[0]
        rows = db.execute("SELECT path,filename,size,last_access FROM preview_cache_files ORDER BY prewarmed ASC,last_access ASC").fetchall()
        for row in rows:
            expired = row["last_access"] < cutoff
            if not expired and total <= limit:
                continue
            file = jobs.cache_folder(row["path"]) / row["filename"]
            file.unlink(missing_ok=True)
            db.execute("DELETE FROM preview_cache_files WHERE path=? AND filename=?", (row["path"], row["filename"]))
            total -= row["size"]; removed_files += 1; removed_bytes += row["size"]
    if removed_files:
        logger.info("preview_cache event=lru_cleanup files=%d bytes=%d limit=%d max_age_days=7", removed_files, removed_bytes, limit)


def efficient_preview_index(item: dict) -> None:
    path = str(item["path"])
    shutil.rmtree(jobs.cache_folder(path), ignore_errors=True)
    with connection() as db:
        db.execute("DELETE FROM preview_cache_files WHERE path=?", (path,))
        db.execute("INSERT OR REPLACE INTO preview_cache_index(path,modified,size,audio_files,subtitle_files,cache_bytes,indexed_at) VALUES(?,?,?,0,0,0,datetime('now'))", (path, item["modified"], item["size"]))
    logger.info("preview_cache event=media_invalidated file=%s", path.replace("\n", "\\n"))


jobs.processors["previews"] = efficient_preview_index


def controlled_worker(job: str, items: list[dict]) -> None:
    errors = 0; stopped = False
    logger.info("index_job=%s event=started pending=%d", job, len(items))
    interval = max(1, len(items) // 20)
    try:
        for number, item in enumerate(items, 1):
            if stop_events[job].is_set(): stopped = True; break
            while not control_events[job].wait(.5):
                if stop_events[job].is_set(): stopped = True; break
            if stopped: break
            with jobs.locks[job]: jobs.states[job].update(current=item.get("title") or Path(item["path"]).name, paused=False)
            try: jobs.processors[job](item)
            except Exception as exc:
                errors += 1; logger.warning("index_job=%s event=media_failed completed=%d total=%d file=%s error=%s", job, number, len(items), str(item["path"]).replace("\n", "\\n"), str(exc).replace("\n", " ")[-500:])
            with jobs.locks[job]: jobs.states[job].update(completed=number, errors=errors)
            if number == 1 or number == len(items) or number % interval == 0:
                logger.info("index_job=%s event=progress completed=%d total=%d errors=%d", job, number, len(items), errors)
    finally:
        with jobs.locks[job]: jobs.states[job].update(running=False, paused=False, current="")
        stop_events[job].clear(); control_events[job].set()
        logger.info("index_job=%s event=%s completed=%d total=%d errors=%d", job, "stopped" if stopped else "completed", jobs.states[job].get("completed", 0), len(items), errors)


jobs.worker = controlled_worker
for state in jobs.states.values(): state.setdefault("paused", False)


_original_status = jobs.status
def enhanced_status(job: str) -> dict:
    result = _original_status(job)
    result["paused"] = not control_events[job].is_set() and result["running"]
    if job == "previews":
        with connection() as db:
            row = db.execute("SELECT count(*),coalesce(sum(size),0) FROM preview_cache_files").fetchone()
        result.update(cache_files=row[0], cache_bytes=row[1], cache_limit=cache_limit())
    return result
jobs.status = enhanced_status


@app.post("/api/v63/setup/index/{job}/pause")
def pause_job(job: str) -> dict:
    if job not in jobs.JOBS: raise HTTPException(404, "Unknown indexing job")
    if not jobs.states[job]["running"]: raise HTTPException(409, "Indexing job is not running")
    control_events[job].clear()
    with jobs.locks[job]: jobs.states[job]["paused"] = True
    logger.info("index_job=%s event=paused", job)
    return enhanced_status(job)


@app.post("/api/v63/setup/index/{job}/resume")
def resume_job(job: str) -> dict:
    if job not in jobs.JOBS: raise HTTPException(404, "Unknown indexing job")
    control_events[job].set()
    with jobs.locks[job]: jobs.states[job]["paused"] = False
    logger.info("index_job=%s event=resumed", job)
    return enhanced_status(job)


@app.post("/api/v63/setup/index/{job}/stop")
def stop_job(job: str) -> dict:
    if job not in jobs.JOBS: raise HTTPException(404, "Unknown indexing job")
    stop_events[job].set(); control_events[job].set()
    logger.info("index_job=%s event=stop_requested", job)
    return enhanced_status(job)


@app.get("/api/v63/setup/preview-cache")
def preview_cache_settings() -> dict:
    return {"gigabytes": round(cache_limit() / 1024**3, 2)}


@app.put("/api/v63/setup/preview-cache")
def update_preview_cache_settings(request: CacheLimit) -> dict:
    value = int(request.gigabytes * 1024**3)
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO index_job_settings(key,value) VALUES('preview_cache_limit_bytes',?)", (str(value),))
    enforce_lru()
    logger.info("index_job=previews event=cache_limit_changed gigabytes=%.2f", request.gigabytes)
    return {"gigabytes": request.gigabytes}


@app.get("/api/v63/stream-preview/audio")
def efficient_cached_audio(path: str, type_index: int, segment: int = 0) -> Response:
    media = authorized_import_file(path); folder = jobs.cache_folder(str(media)); file = folder / f"audio-{type_index}-{segment}.mp3"
    cache_state = "hit"
    if not file.is_file():
        cache_state = "miss"; folder.mkdir(parents=True, exist_ok=True)
        temporary = file.with_suffix(".tmp"); temporary.write_bytes(encode_audio(media, type_index, segment)); os.replace(temporary, file)
        register_file(str(media), file, False); enforce_lru()
    else:
        with connection() as db: db.execute("UPDATE preview_cache_files SET last_access=? WHERE path=? AND filename=?", (time.time(), str(media), file.name))
    duration_file = folder / "duration.txt"
    duration = duration_file.read_text(encoding="ascii") if duration_file.is_file() else str((probe(media).get("format") or {}).get("duration") or 0)
    return Response(file.read_bytes(), media_type="audio/mpeg", headers={"Cache-Control": "private,max-age=3600", "X-Preview-Cache": cache_state, "X-Media-Duration": duration})


@app.get("/api/v63/stream-preview/subtitle")
def efficient_cached_subtitle(path: str, type_index: int = 0, external_path: str | None = None, page: int = 0) -> dict:
    return cached_subtitle_preview(path, type_index, external_path, page)
