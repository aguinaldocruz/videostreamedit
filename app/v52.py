from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

import app.v38 as movie_index
import app.v39 as index_routes
from app.v11 import connection
from app.v39 import RefreshMovieIndex
from app.v51 import app, inspect_extended


@app.on_event("startup")
def schedule_extended_subtitle_index_migration() -> None:
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS feature_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        applied = db.execute("SELECT 1 FROM feature_migrations WHERE name='subtitle_extended_index_v1'").fetchone()
        if not applied:
            db.execute("DELETE FROM movie_stream_index")
            db.execute("DELETE FROM movie_stream_index_value")
            db.execute("DELETE FROM subtitle_extended_index")
            db.execute("INSERT INTO feature_migrations(name) VALUES('subtitle_extended_index_v1')")


@app.post("/api/v52/setup/movie-index/rebuild")
def rebuild_all_movie_indexes() -> dict:
    with connection() as db:
        db.execute("DELETE FROM subtitle_extended_index")
    return index_routes.rebuild_movie_index()


@app.post("/api/v52/movies/stream-filter-refresh")
def refresh_movie_indexes(payload: RefreshMovieIndex) -> dict:
    with connection() as db:
        item = db.execute("SELECT path,modified,size FROM plex_media WHERE kind='movie' AND path=?", (payload.path,)).fetchone()
    if not item:
        return {"indexed": False}
    path = Path(item["path"])
    if not path.is_file():
        raise HTTPException(404, "Movie file is not accessible")
    base_values = movie_index._inspect_movie(path)
    extended_values = inspect_extended(path)
    with connection() as db:
        db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (str(path),))
        db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (str(path),))
        db.executemany("INSERT INTO movie_stream_index_value(path,stream_type,language,track_name) VALUES(?,?,?,?)", [(str(path), *value) for value in base_values])
        db.executemany("INSERT INTO subtitle_extended_index(path,source,type_index,external_path,codec,encoding,markup) VALUES(?,?,?,?,?,?,?)", extended_values)
        db.execute("INSERT OR REPLACE INTO movie_stream_index(path,modified,size,indexed_at) VALUES(?,?,?,datetime('now'))", (str(path), item["modified"], item["size"]))
    return {"indexed": True}
