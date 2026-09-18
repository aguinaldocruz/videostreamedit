from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
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
PREVIEW_SAMPLE_SECONDS = 25
PREVIEW_START_SECONDS = 5 * 60
PREVIEW_AUDIO_RATE = 32000
PREVIEW_AUDIO_CHANNELS = 1
PREVIEW_AUDIO_BITRATE = "64k"
PREVIEW_ENCODER_VERSION = "audio-preview-v2"
PREVIEW_MIN_FREE_BYTES = int(float(os.getenv("PREVIEW_MIN_FREE_GB", "1")) * 1024**3)
try:
    PREVIEW_MAX_CONCURRENT = max(1, min(4, int(os.getenv("PREVIEW_MAX_CONCURRENT", "1"))))
except ValueError:
    PREVIEW_MAX_CONCURRENT = 1
# Preview extraction is intentionally low-concurrency: one FFmpeg process by
# default, with per-key locks preventing duplicate work for the same segment.
_preview_slot = threading.BoundedSemaphore(PREVIEW_MAX_CONCURRENT)
_preview_lock_guard = threading.Lock()
_preview_key_locks: dict[tuple[str, int, int], threading.Lock] = {}
_preview_metric_lock = threading.Lock()
_preview_metrics = {"hits": 0, "misses": 0, "failures": 0, "extractions": 0, "extraction_seconds": 0.0, "last_extraction_seconds": 0.0}
control_events = {name: threading.Event() for name in jobs.JOBS}
stop_events = {name: threading.Event() for name in jobs.JOBS}
for event in control_events.values():
    event.set()


class CacheLimit(BaseModel):
    gigabytes: float = Field(ge=0.25, le=100)


@app.on_event("startup")
def migrate_efficient_preview_cache() -> None:
    jobs.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Preview files are disposable and now belong on the data volume. Move
    # legacy artifacts once when the new location is empty; never scan media.
    legacy = jobs.LEGACY_CACHE_DIR
    if legacy != jobs.CACHE_DIR and legacy.is_dir():
        moved = 0
        for child in list(legacy.iterdir()):
            target = jobs.CACHE_DIR / child.name
            if target.exists():
                continue
            try:
                child.rename(target)
                moved += 1
            except OSError:
                pass
        try:
            legacy.rmdir()
        except OSError:
            pass
        if moved:
            logger.info("preview_cache event=storage_migrated old=%s new=%s entries=%d", legacy, jobs.CACHE_DIR, moved)
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS preview_cache_files (
                path TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
                last_access REAL NOT NULL, prewarmed INTEGER NOT NULL DEFAULT 0,
                media_signature TEXT NOT NULL DEFAULT '', encoder_version TEXT NOT NULL DEFAULT '',
                duration REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(path,filename)
            );
            CREATE TABLE IF NOT EXISTS index_job_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO index_job_settings(key,value) VALUES('preview_cache_limit_bytes','536870912');
            CREATE TABLE IF NOT EXISTS feature_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        db.execute("UPDATE index_job_settings SET value='536870912' WHERE key='preview_cache_limit_bytes' AND value='5368709120'")
        for column, definition in (("media_signature", "TEXT NOT NULL DEFAULT ''"), ("encoder_version", "TEXT NOT NULL DEFAULT ''"), ("duration", "REAL NOT NULL DEFAULT 0"), ("created_at", "REAL NOT NULL DEFAULT 0")):
            if not jobs.column_exists(db, "preview_cache_files", column):
                db.execute(f"ALTER TABLE preview_cache_files ADD COLUMN {column} {definition}")
        db.execute("UPDATE preview_cache_files SET created_at=last_access WHERE created_at=0")
        migrated = db.execute("SELECT 1 FROM feature_migrations WHERE name='preview_cache_policy_v2'").fetchone()
        if not migrated:
            db.execute("DELETE FROM preview_cache_index")
            db.execute("DELETE FROM preview_cache_files")
            db.execute("INSERT INTO feature_migrations(name) VALUES('preview_cache_policy_v2')")
        subtitle_cache_removed = db.execute("SELECT 1 FROM feature_migrations WHERE name='subtitle_preview_cache_removed_v1'").fetchone()
        if not subtitle_cache_removed:
            # Remove legacy subtitle preview artifacts while retaining audio cache.
            db.execute("DELETE FROM preview_cache_files WHERE filename LIKE 'subtitle-%'")
            db.execute("UPDATE preview_cache_index SET cache_bytes=COALESCE((SELECT SUM(size) FROM preview_cache_files f WHERE f.path=preview_cache_index.path),0)")
            db.execute("INSERT INTO feature_migrations(name) VALUES('subtitle_preview_cache_removed_v1')")
    if not migrated:
        shutil.rmtree(jobs.CACHE_DIR, ignore_errors=True)
        jobs.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("index_job=previews event=cache_policy_migrated policy=first_25_seconds_64k_mono old_cache=cleared")
    # Legacy subtitle files may not have been registered in the database.
    removed_subtitle_files = 0
    for folder in jobs.CACHE_DIR.iterdir():
        if not folder.is_dir():
            continue
        for pattern in ("subtitle-*.srt", "subtitle-*-page-*.json"):
            for file in folder.glob(pattern):
                try:
                    file.unlink()
                    removed_subtitle_files += 1
                except OSError:
                    pass
    if removed_subtitle_files:
        logger.info("index_job=previews event=subtitle_cache_removed files=%d", removed_subtitle_files)


def cache_limit() -> int:
    with connection() as db:
        row = db.execute("SELECT value FROM index_job_settings WHERE key='preview_cache_limit_bytes'").fetchone()
    return int(row[0]) if row else DEFAULT_CACHE_LIMIT


@contextmanager
def preview_claim(path: str, type_index: int, segment: int):
    key = (str(path), int(type_index), int(segment))
    with _preview_lock_guard:
        lock = _preview_key_locks.setdefault(key, threading.Lock())
    try:
        with lock:
            with _preview_slot:
                yield
    finally:
        # Keep the lock map bounded after the request has completed. A racing
        # request may retain the lock briefly; removing only unlocked entries
        # avoids unbounded growth without invalidating active claims.
        with _preview_lock_guard:
            if not lock.locked():
                _preview_key_locks.pop(key, None)


def audio_command(path: Path, type_index: int, segment: int) -> list[str]:
    return [
        "nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", str(max(0, segment) * PREVIEW_SAMPLE_SECONDS), "-i", str(path), "-map", f"0:a:{type_index}",
        "-vn", "-t", str(PREVIEW_SAMPLE_SECONDS), "-ac", str(PREVIEW_AUDIO_CHANNELS),
        "-ar", str(PREVIEW_AUDIO_RATE), "-b:a", PREVIEW_AUDIO_BITRATE, "-f", "mp3", "pipe:1",
    ]


def encode_audio(path: Path, type_index: int, segment: int) -> bytes:
    try:
        result = subprocess.run(audio_command(path, type_index, segment), capture_output=True, timeout=120, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes): detail = detail.decode("utf-8", errors="replace")
        raise HTTPException(422, (detail or "Could not create audio preview")[-1200:]) from exc
    return result.stdout


def record_preview_metric(name: str, value: float = 1) -> None:
    with _preview_metric_lock:
        if name in _preview_metrics:
            _preview_metrics[name] += value


def preview_metrics() -> dict:
    with _preview_metric_lock:
        result = dict(_preview_metrics)
    total = result["hits"] + result["misses"]
    result["hit_rate"] = round(result["hits"] / total, 4) if total else None
    result["average_extraction_seconds"] = round(result["extraction_seconds"] / result["extractions"], 3) if result["extractions"] else None
    result["last_extraction_seconds"] = round(result["last_extraction_seconds"], 3)
    return result


def reset_preview_metrics() -> None:
    with _preview_metric_lock:
        for key in _preview_metrics:
            _preview_metrics[key] = 0.0 if key.endswith("seconds") else 0


def preview_media_signature(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    with path.open("rb") as stream:
        digest.update(stream.read(65536))
        if stat.st_size > 65536:
            stream.seek(max(0, stat.st_size - 65536))
            digest.update(stream.read(65536))
    return digest.hexdigest()


def register_file(path: str, file: Path, prewarmed: bool, media_signature: str = "", duration: float = 0) -> None:
    now = time.time()
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO preview_cache_files(path,filename,size,last_access,prewarmed,media_signature,encoder_version,duration,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (path, file.name, file.stat().st_size, now, int(prewarmed), media_signature, PREVIEW_ENCODER_VERSION, float(duration or 0), now))


def cache_entry_valid(path: str, filename: str, media_signature: str) -> bool:
    with connection() as db:
        row = db.execute("SELECT media_signature,encoder_version FROM preview_cache_files WHERE path=? AND filename=?", (path, filename)).fetchone()
    return bool(row and str(row[0] or '') == media_signature and str(row[1] or '') == PREVIEW_ENCODER_VERSION)


def enforce_lru() -> None:
    limit = cache_limit()
    cutoff = time.time() - 7 * 86400
    removed_files = removed_bytes = 0
    try:
        free_bytes = shutil.disk_usage(jobs.CACHE_DIR).free
    except OSError:
        free_bytes = PREVIEW_MIN_FREE_BYTES
    with connection() as db:
        total = db.execute("SELECT coalesce(sum(size),0) FROM preview_cache_files").fetchone()[0]
        rows = db.execute(
            "SELECT path,filename,size,last_access FROM preview_cache_files "
            "WHERE last_access < ? OR ? > ? ORDER BY prewarmed ASC,last_access ASC LIMIT 512",
            (cutoff, total, limit),
        ).fetchall()
        for row in rows:
            expired = row["last_access"] < cutoff
            free_low = free_bytes + removed_bytes < PREVIEW_MIN_FREE_BYTES
            if not expired and total <= limit and not free_low:
                continue
            file = jobs.cache_folder(row["path"]) / row["filename"]
            file.unlink(missing_ok=True)
            db.execute("DELETE FROM preview_cache_files WHERE path=? AND filename=?", (row["path"], row["filename"]))
            total -= row["size"]; removed_files += 1; removed_bytes += row["size"]
        # Drop database entries whose disposable files disappeared externally.
        for row in db.execute("SELECT path,filename FROM preview_cache_files ORDER BY last_access ASC LIMIT 512").fetchall():
            if not (jobs.cache_folder(row["path"]) / row["filename"]).is_file():
                db.execute("DELETE FROM preview_cache_files WHERE path=? AND filename=?", (row["path"], row["filename"]))
    if removed_files:
        logger.info("preview_cache event=lru_cleanup files=%d bytes=%d limit=%d min_free=%d max_age_days=7", removed_files, removed_bytes, limit, PREVIEW_MIN_FREE_BYTES)


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
    return {"gigabytes": round(cache_limit() / 1024**3, 2), "directory": str(jobs.CACHE_DIR), "legacy_directory": str(jobs.LEGACY_CACHE_DIR)}


@app.get("/api/v63/setup/preview-cache/status")
def preview_cache_status() -> dict:
    try:
        usage = shutil.disk_usage(jobs.CACHE_DIR)
        free_bytes = usage.free
        total_bytes = usage.total
    except OSError:
        free_bytes = total_bytes = 0
    with connection() as db:
        row = db.execute("SELECT count(*) AS cache_files,coalesce(sum(size),0) AS cache_bytes,coalesce(sum(CASE WHEN prewarmed=0 THEN 1 ELSE 0 END),0) AS on_demand_files FROM preview_cache_files").fetchone()
    return {
        "cache_files": int(row["cache_files"] or 0),
        "cache_bytes": int(row["cache_bytes"] or 0),
        "on_demand_files": int(row["on_demand_files"] or 0),
        "limit_bytes": cache_limit(),
        "free_bytes": int(free_bytes),
        "disk_total_bytes": int(total_bytes),
        "minimum_free_bytes": PREVIEW_MIN_FREE_BYTES,
        "directory": str(jobs.CACHE_DIR),
        "mode": "on-demand",
        "metrics": preview_metrics(),
        "max_concurrent": PREVIEW_MAX_CONCURRENT,
    }


@app.post("/api/v63/setup/preview-cache/clear")
def clear_preview_cache() -> dict:
    """Clear disposable audio samples only; no media or index work is queued."""
    shutil.rmtree(jobs.CACHE_DIR, ignore_errors=True)
    jobs.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        removed = db.execute("DELETE FROM preview_cache_files").rowcount
        db.execute("UPDATE preview_cache_index SET audio_files=0,subtitle_files=0,cache_bytes=0")
    reset_preview_metrics()
    logger.info("preview_cache event=cleared mode=on_demand files=%d", removed)
    return {"cleared": int(removed or 0), "directory": str(jobs.CACHE_DIR)}


@app.post("/api/v63/setup/preview-cache/maintenance")
def maintain_preview_cache() -> dict:
    """Reconcile disposable cache files without probing or queueing media."""
    enforce_lru()
    with connection() as db:
        row = db.execute("SELECT count(*),coalesce(sum(size),0) FROM preview_cache_files").fetchone()
    return {"cache_files": int(row[0] or 0), "cache_bytes": int(row[1] or 0), "limit": cache_limit(), "directory": str(jobs.CACHE_DIR)}


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
    media = authorized_import_file(path); folder = jobs.cache_folder(str(media)); segment = max(0, segment); file = folder / f"audio-{type_index}-{segment}.mp3"
    media_signature = preview_media_signature(media)
    cache_state = "hit" if file.is_file() and cache_entry_valid(str(media), file.name, media_signature) else "miss"
    if cache_state == "miss":
        with preview_claim(str(media), type_index, segment):
            if file.is_file():
                cache_state = "hit"
            else:
                cache_state = "miss"; record_preview_metric("misses"); folder.mkdir(parents=True, exist_ok=True)
                started = time.monotonic()
                temporary = file.with_suffix(".tmp"); temporary.write_bytes(encode_audio(media, type_index, segment)); os.replace(temporary, file)
                elapsed = time.monotonic() - started; record_preview_metric("extractions"); record_preview_metric("extraction_seconds", elapsed); record_preview_metric("last_extraction_seconds", elapsed)
                register_file(str(media), file, False, media_signature, float((probe(media).get("format") or {}).get("duration") or 0)); enforce_lru()
    if cache_state == "hit":
        record_preview_metric("hits")
        with connection() as db: db.execute("UPDATE preview_cache_files SET last_access=? WHERE path=? AND filename=?", (time.time(), str(media), file.name))
    duration_file = folder / "duration.txt"
    duration = duration_file.read_text(encoding="ascii") if duration_file.is_file() else str((probe(media).get("format") or {}).get("duration") or 0)
    return Response(file.read_bytes(), media_type="audio/mpeg", headers={"Cache-Control": "private,max-age=3600", "X-Preview-Cache": cache_state, "X-Media-Duration": duration, "X-Preview-Encoder": PREVIEW_ENCODER_VERSION})


@app.get("/api/v63/stream-preview/subtitle")
def efficient_cached_subtitle(path: str, type_index: int = 0, external_path: str | None = None, page: int = 0) -> dict:
    return cached_subtitle_preview(path, type_index, external_path, page)
