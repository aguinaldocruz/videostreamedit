from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from fastapi import Query
from pydantic import BaseModel

from app.v2 import probe
from app.v5 import external_subtitles, split_tag
from app.v11 import connection
from app.v37 import app


logger = logging.getLogger("videostreamedit")
_index_lock = threading.Lock()
_index_state = {"running": False, "total": 0, "completed": 0, "errors": 0}


class MovieStreamIndexInvalidate(BaseModel):
    path: str


@app.on_event("startup")
def initialize_movie_stream_filter_index() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS movie_stream_index (
                path TEXT PRIMARY KEY, modified INTEGER NOT NULL, size INTEGER NOT NULL,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS movie_stream_index_value (
                path TEXT NOT NULL, stream_type TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '', track_name TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS movie_stream_value_path
                ON movie_stream_index_value(path);
            CREATE INDEX IF NOT EXISTS movie_stream_value_filter
                ON movie_stream_index_value(stream_type, language, track_name);
        """)


def _pending_movies() -> list[dict]:
    with connection() as db:
        db.execute("DELETE FROM movie_stream_index_value WHERE path NOT IN (SELECT path FROM plex_media WHERE kind='movie')")
        db.execute("DELETE FROM movie_stream_index WHERE path NOT IN (SELECT path FROM plex_media WHERE kind='movie')")
        rows = db.execute("""
            SELECT media.path, media.modified, media.size
              FROM plex_media AS media
              LEFT JOIN movie_stream_index AS cached ON cached.path = media.path
             WHERE media.kind='movie'
               AND (cached.path IS NULL OR cached.modified != media.modified OR cached.size != media.size)
             ORDER BY media.title COLLATE NOCASE, media.path
        """).fetchall()
    return [dict(row) for row in rows]


def _inspect_movie(path: Path) -> list[tuple[str, str, str]]:
    values: list[tuple[str, str, str]] = []
    for stream in probe(path).get("streams", []):
        stream_type = stream.get("codec_type")
        if stream_type not in {"audio", "subtitle"}:
            continue
        tags = stream.get("tags") or {}
        language, _ = split_tag(str(tags.get("language") or ""))
        values.append((stream_type, language.strip(), str(tags.get("title") or "").strip()))
    for stream in external_subtitles(path):
        values.append(("subtitle", str(stream.get("language") or "").strip(), str(stream.get("title") or "").strip()))
    return values


def _run_index(items: list[dict]) -> None:
    errors = 0
    try:
        for completed, item in enumerate(items, 1):
            try:
                path = Path(item["path"])
                if not path.is_file():
                    raise FileNotFoundError(str(path))
                values = _inspect_movie(path)
                with connection() as db:
                    db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (str(path),))
                    db.executemany(
                        "INSERT INTO movie_stream_index_value(path,stream_type,language,track_name) VALUES(?,?,?,?)",
                        [(str(path), *value) for value in values],
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO movie_stream_index(path,modified,size,indexed_at) VALUES(?,?,?,datetime('now'))",
                        (str(path), item["modified"], item["size"]),
                    )
            except Exception as exc:  # one unreadable movie must not stop the catalog
                errors += 1
                logger.warning("change=movie_stream_index_failed path=%s error=%s", item["path"], exc)
                with connection() as db:
                    db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (item["path"],))
                    db.execute("INSERT OR REPLACE INTO movie_stream_index(path,modified,size,indexed_at) VALUES(?,?,?,datetime('now'))", (item["path"], item["modified"], item["size"]))
            with _index_lock:
                _index_state.update(completed=completed, errors=errors)
            time.sleep(0.08)
    finally:
        with _index_lock:
            _index_state["running"] = False
        logger.info("change=movie_stream_index_completed media=%d errors=%d", len(items), errors)


def _start_index_if_needed() -> None:
    with _index_lock:
        if _index_state["running"]:
            return
        items = _pending_movies()
        _index_state.update(running=bool(items), total=len(items), completed=0, errors=0)
        if items:
            threading.Thread(target=_run_index, args=(items,), name="movie-stream-index", daemon=True).start()


def _value_groups(rows, field: str) -> dict[str, list[str]]:
    result = {"all": [], "audio": [], "subtitle": []}
    for row in rows:
        value = row[field]
        if not value:
            continue
        result[row["stream_type"]].append(value)
        result["all"].append(value)
    return {key: sorted(set(values), key=str.casefold) for key, values in result.items()}


@app.get("/api/v38/movies/stream-filter-values")
def movie_stream_filter_values() -> dict:
    _start_index_if_needed()
    with connection() as db:
        language_rows = db.execute("SELECT DISTINCT stream_type,language FROM movie_stream_index_value WHERE language != ''").fetchall()
        name_rows = db.execute("SELECT DISTINCT stream_type,track_name FROM movie_stream_index_value WHERE track_name != ''").fetchall()
        indexed = db.execute("SELECT count(*) FROM movie_stream_index").fetchone()[0]
        movies = db.execute("SELECT count(*) FROM plex_media WHERE kind='movie'").fetchone()[0]
    with _index_lock:
        status = dict(_index_state)
    return {
        "languages": _value_groups(language_rows, "language"),
        "track_names": _value_groups(name_rows, "track_name"),
        "status": {**status, "indexed": indexed, "movies": movies},
    }


@app.get("/api/v38/movies/stream-filter-matches")
def movie_stream_filter_matches(
    stream_type: str = Query(default="all", pattern="^(all|audio|subtitle)$"),
    language: str = "",
    track_name: str = "",
) -> dict:
    clauses = ["media.kind='movie'"]
    values: list[str] = []
    if stream_type != "all":
        clauses.append("value.stream_type=?")
        values.append(stream_type)
    if language:
        clauses.append("value.language=?")
        values.append(language)
    if track_name:
        clauses.append("value.track_name=?")
        values.append(track_name)
    with connection() as db:
        rows = db.execute(
            "SELECT DISTINCT media.path FROM plex_media media JOIN movie_stream_index_value value ON value.path=media.path WHERE " + " AND ".join(clauses),
            values,
        ).fetchall()
    return {"paths": [row["path"] for row in rows]}


@app.post("/api/v38/movies/stream-filter-invalidate")
def invalidate_movie_stream_filter(payload: MovieStreamIndexInvalidate) -> dict:
    with connection() as db:
        db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (payload.path,))
        db.execute("DELETE FROM movie_stream_index WHERE path=?", (payload.path,))
    logger.info("change=movie_stream_index_invalidated path=%s", payload.path)
    return {"invalidated": True}
