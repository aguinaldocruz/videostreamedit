from __future__ import annotations

import hashlib
import logging
import math
import shutil
import subprocess
import threading
from pathlib import Path

from fastapi import HTTPException

import app.v38 as legacy_index
from app.v2 import CONFIG_DIR, probe
from app.v11 import connection
from app.v51 import inspect_extended
from app.v53 import app


logger = logging.getLogger("uvicorn.error")
CACHE_DIR = CONFIG_DIR / "preview-cache"
JOBS = ("core", "subtitles", "previews")
locks = {name: threading.Lock() for name in JOBS}
states = {name: {"running": False, "total": 0, "completed": 0, "errors": 0, "current": ""} for name in JOBS}


@app.on_event("startup")
def initialize_split_indexes() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS subtitle_extended_media (
                path TEXT PRIMARY KEY, modified INTEGER NOT NULL, size INTEGER NOT NULL,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS preview_cache_index (
                path TEXT PRIMARY KEY, modified INTEGER NOT NULL, size INTEGER NOT NULL,
                audio_files INTEGER NOT NULL DEFAULT 0, subtitle_files INTEGER NOT NULL DEFAULT 0,
                cache_bytes INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)


def media_rows() -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute("SELECT path,modified,size,title FROM plex_media WHERE kind='movie' ORDER BY title COLLATE NOCASE,path")]


def pending(job: str) -> list[dict]:
    table = {"core": "movie_stream_index", "subtitles": "subtitle_extended_media", "previews": "preview_cache_index"}[job]
    with connection() as db:
        rows = db.execute(f"""
            SELECT media.path,media.modified,media.size,media.title FROM plex_media media
            LEFT JOIN {table} cached ON cached.path=media.path
            WHERE media.kind='movie' AND (cached.path IS NULL OR cached.modified!=media.modified OR cached.size!=media.size)
            ORDER BY media.title COLLATE NOCASE,media.path
        """).fetchall()
    return [dict(row) for row in rows]


def cache_key(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def cache_folder(path: str) -> Path:
    return CACHE_DIR / cache_key(path)


def run_quiet(command: list[str], timeout: int = 180) -> bytes:
    result = subprocess.run(["nice", "-n", "10", *command], capture_output=True, timeout=timeout, check=True)
    return result.stdout


def index_core(item: dict) -> None:
    path = Path(item["path"])
    values = legacy_index._inspect_movie(path)
    with connection() as db:
        db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (str(path),))
        db.executemany("INSERT INTO movie_stream_index_value(path,stream_type,language,track_name) VALUES(?,?,?,?)", [(str(path), *value) for value in values])
        db.execute("INSERT OR REPLACE INTO movie_stream_index(path,modified,size,indexed_at) VALUES(?,?,?,datetime('now'))", (str(path), item["modified"], item["size"]))


def index_subtitles(item: dict) -> None:
    path = Path(item["path"])
    values = inspect_extended(path)
    with connection() as db:
        db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (str(path),))
        db.executemany("INSERT INTO subtitle_extended_index(path,source,type_index,external_path,codec,encoding,markup) VALUES(?,?,?,?,?,?,?)", values)
        db.execute("INSERT OR REPLACE INTO subtitle_extended_media(path,modified,size,indexed_at) VALUES(?,?,?,datetime('now'))", (str(path), item["modified"], item["size"]))


def index_previews(item: dict) -> None:
    path = Path(item["path"])
    folder = cache_folder(str(path))
    temporary = folder.with_name(folder.name + ".building")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True, exist_ok=True)
    details = probe(path)
    try:
        duration = float((details.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    (temporary / "duration.txt").write_text(str(duration), encoding="ascii")
    audio_index = subtitle_index = audio_files = subtitle_files = 0
    try:
        for stream in details.get("streams", []):
            kind = stream.get("codec_type")
            if kind == "audio":
                segments = 12 if not duration else min(12, max(1, math.ceil(min(duration, 300) / 25)))
                for segment in range(segments):
                    target = temporary / f"audio-{audio_index}-{segment}.mp3"
                    data = run_quiet(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(segment * 25), "-i", str(path), "-map", f"0:a:{audio_index}", "-vn", "-t", "25", "-ac", "2", "-ar", "44100", "-b:a", "128k", "-f", "mp3", "pipe:1"])
                    if data:
                        target.write_bytes(data); audio_files += 1
                audio_index += 1
            elif kind == "subtitle":
                codec = str(stream.get("codec_name") or "")
                if codec in {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}:
                    data = run_quiet(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", f"0:s:{subtitle_index}", "-t", "300", "-f", "srt", "pipe:1"])
                    if data:
                        (temporary / f"subtitle-{subtitle_index}.srt").write_bytes(data); subtitle_files += 1
                subtitle_index += 1
        shutil.rmtree(folder, ignore_errors=True)
        temporary.rename(folder)
        total_bytes = sum(file.stat().st_size for file in folder.iterdir() if file.is_file())
        with connection() as db:
            db.execute("INSERT OR REPLACE INTO preview_cache_index(path,modified,size,audio_files,subtitle_files,cache_bytes,indexed_at) VALUES(?,?,?,?,?,?,datetime('now'))", (str(path), item["modified"], item["size"], audio_files, subtitle_files, total_bytes))
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


processors = {"core": index_core, "subtitles": index_subtitles, "previews": index_previews}


def worker(job: str, items: list[dict]) -> None:
    errors = 0
    logger.info("index_job=%s event=started pending=%d", job, len(items))
    try:
        interval = max(1, len(items) // 20)
        for number, item in enumerate(items, 1):
            with locks[job]:
                states[job]["current"] = item.get("title") or Path(item["path"]).name
            try:
                processors[job](item)
            except Exception as exc:
                errors += 1
                logger.warning("index_job=%s event=media_failed completed=%d total=%d file=%s error=%s", job, number, len(items), str(item["path"]).replace("\n", "\\n"), str(exc).replace("\n", " ")[-500:])
            with locks[job]:
                states[job].update(completed=number, errors=errors)
            if number == 1 or number == len(items) or number % interval == 0:
                logger.info("index_job=%s event=progress completed=%d total=%d errors=%d", job, number, len(items), errors)
    finally:
        with locks[job]:
            states[job].update(running=False, current="")
        logger.info("index_job=%s event=completed total=%d errors=%d", job, len(items), errors)


def start(job: str) -> None:
    if job not in JOBS:
        raise HTTPException(404, "Unknown indexing job")
    with locks[job]:
        if states[job]["running"]:
            return
        items = pending(job)
        states[job].update(running=bool(items), total=len(items), completed=0, errors=0, current="")
    if items:
        threading.Thread(target=worker, args=(job, items), name=f"vse-index-{job}", daemon=True).start()


def status(job: str) -> dict:
    if job not in JOBS:
        raise HTTPException(404, "Unknown indexing job")
    with locks[job]:
        result = dict(states[job])
    with connection() as db:
        table = {"core": "movie_stream_index", "subtitles": "subtitle_extended_media", "previews": "preview_cache_index"}[job]
        result["indexed"] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        result["movies"] = db.execute("SELECT count(*) FROM plex_media WHERE kind='movie'").fetchone()[0]
        if job == "previews":
            row = db.execute("SELECT coalesce(sum(audio_files),0),coalesce(sum(subtitle_files),0),coalesce(sum(cache_bytes),0) FROM preview_cache_index").fetchone()
            result.update(audio_files=row[0], subtitle_files=row[1], cache_bytes=row[2])
    result["pending"] = max(0, result["total"] - result["completed"]) if result["running"] else len(pending(job))
    return result


@app.get("/api/v54/setup/index/{job}/status")
def job_status(job: str) -> dict:
    return status(job)


@app.post("/api/v54/setup/index/{job}/check")
def check_job(job: str) -> dict:
    start(job)
    return status(job)


@app.post("/api/v54/setup/index/{job}/rebuild")
def rebuild_job(job: str) -> dict:
    if job not in JOBS:
        raise HTTPException(404, "Unknown indexing job")
    with locks[job]:
        if states[job]["running"]:
            raise HTTPException(409, f"The {job} indexing job is already running")
    with connection() as db:
        if job == "core":
            db.execute("DELETE FROM movie_stream_index_value"); db.execute("DELETE FROM movie_stream_index")
        elif job == "subtitles":
            db.execute("DELETE FROM subtitle_extended_index"); db.execute("DELETE FROM subtitle_extended_media")
        else:
            db.execute("DELETE FROM preview_cache_index"); shutil.rmtree(CACHE_DIR, ignore_errors=True); CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("index_job=%s event=rebuild_requested", job)
    start(job)
    return status(job)


@app.post("/api/v54/index/invalidate")
def invalidate_indexes(payload: legacy_index.MovieStreamIndexInvalidate) -> dict:
    with connection() as db:
        db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (payload.path,)); db.execute("DELETE FROM movie_stream_index WHERE path=?", (payload.path,))
        db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (payload.path,)); db.execute("DELETE FROM subtitle_extended_media WHERE path=?", (payload.path,))
        db.execute("DELETE FROM preview_cache_index WHERE path=?", (payload.path,))
    shutil.rmtree(cache_folder(payload.path), ignore_errors=True)
    logger.info("index_event=media_invalidated file=%s jobs=core,subtitles,previews", payload.path.replace("\n", "\\n"))
    return {"invalidated": True}
