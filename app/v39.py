from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

import app.v38 as index
from app.v11 import connection

app = index.app


class RefreshMovieIndex(BaseModel):
    path: str


def index_status(include_pending: bool = True) -> dict:
    with index._index_lock:
        state = dict(index._index_state)
    with connection() as db:
        state["indexed"] = db.execute("SELECT count(*) FROM media_stream_index WHERE path IN (SELECT path FROM plex_media WHERE kind='movie')").fetchone()[0]
        state["movies"] = db.execute("SELECT count(*) FROM plex_media WHERE kind='movie'").fetchone()[0]
    state["pending"] = len(index._pending_movies()) if include_pending and not state["running"] else max(0, state["total"] - state["completed"])
    return state


@app.get("/api/v39/setup/movie-index/status")
def movie_index_status() -> dict:
    return index_status()


@app.post("/api/v39/setup/movie-index/check")
def check_movie_index() -> dict:
    index._start_index_if_needed()
    return index_status(False)


@app.post("/api/v39/setup/movie-index/rebuild")
def rebuild_movie_index() -> dict:
    with index._index_lock:
        if index._index_state["running"]:
            raise HTTPException(409, "The movie stream index is already running")
        with connection() as db:
            db.execute("DELETE FROM movie_stream_index_value")
            db.execute("DELETE FROM movie_stream_index")
        index._index_state.update(running=False, total=0, completed=0, errors=0)
    index._start_index_if_needed()
    return index_status(False)


@app.get("/api/v39/movies/stream-filter-values")
def stable_movie_stream_filter_values() -> dict:
    with connection() as db:
        language_rows = db.execute("SELECT DISTINCT CASE WHEN stream_type='external' THEN 'subtitle' ELSE stream_type END AS stream_type,language FROM media_stream_index WHERE language != ''").fetchall()
        name_rows = db.execute("SELECT DISTINCT CASE WHEN stream_type='external' THEN 'subtitle' ELSE stream_type END AS stream_type,track_name FROM media_stream_index WHERE track_name != ''").fetchall()
    return {
        "languages": index._value_groups(language_rows, "language"),
        "track_names": index._value_groups(name_rows, "track_name"),
    }


@app.post("/api/v39/movies/stream-filter-refresh")
def refresh_one_movie_filter(payload: RefreshMovieIndex) -> dict:
    with connection() as db:
        item = db.execute("SELECT path,modified,size FROM plex_media WHERE kind='movie' AND path=?", (payload.path,)).fetchone()
    if not item:
        return {"indexed": False}
    path = Path(item["path"])
    if not path.is_file():
        raise HTTPException(404, "Movie file is not accessible")
    from app.v80 import enqueue
    added = enqueue("core", str(path), "Legacy refresh requested; canonical core index", None)
    return {"indexed": False, "queued": bool(added), "path": str(path)}
