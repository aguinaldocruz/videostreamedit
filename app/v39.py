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
    with connection() as db:
        counts = {row['status']: int(row['n']) for row in db.execute("SELECT status,count(*) AS n FROM index_task_queue WHERE job='core' GROUP BY status")}
        state = {'running': counts.get('running', 0), 'total': sum(counts.values()),
                 'completed': counts.get('succeeded', 0), 'errors': counts.get('failed', 0)}
        # The legacy card used to count stream rows, not movies, and read a
        # projection no longer maintained by the canonical core queue.  Use
        # one media row per movie from the canonical index state instead.
        state["indexed"] = int(db.execute("SELECT count(*) FROM media_stream_index_state s JOIN plex_media p ON p.path=s.path WHERE p.kind='movie'").fetchone()[0] or 0)
        state["movies"] = int(db.execute("SELECT count(*) FROM plex_media WHERE kind='movie'").fetchone()[0] or 0)
        state["tv_shows"] = int(db.execute("SELECT count(*) FROM (SELECT DISTINCT library_key, show_title FROM plex_media WHERE kind='episode') shows").fetchone()[0] or 0)
        state["tv_shows_indexed"] = int(db.execute("SELECT count(*) FROM (SELECT DISTINCT p.library_key, p.show_title FROM plex_media p JOIN media_stream_index_state s ON p.path=s.path WHERE p.kind='episode') shows").fetchone()[0] or 0)
        state["episodes"] = int(db.execute("SELECT count(*) FROM plex_media WHERE kind='episode'").fetchone()[0] or 0)
        state["episodes_indexed"] = int(db.execute("SELECT count(*) FROM media_stream_index_state s JOIN plex_media p ON p.path=s.path WHERE p.kind='episode'").fetchone()[0] or 0)
        state["pending_movies"] = int(db.execute("SELECT count(*) FROM index_task_queue q JOIN plex_media p ON p.path=q.path WHERE q.job='core' AND q.status IN ('pending','running') AND p.kind='movie'").fetchone()[0] or 0)
        state["pending_tv"] = int(db.execute("SELECT count(*) FROM index_task_queue q JOIN plex_media p ON p.path=q.path WHERE q.job='core' AND q.status IN ('pending','running') AND p.kind='episode'").fetchone()[0] or 0)
        state["pending"] = state["pending_movies"] + state["pending_tv"]
    return state


@app.get("/api/v39/setup/movie-index/status")
def movie_index_status() -> dict:
    return index_status()


@app.post("/api/v39/setup/movie-index/check")
def check_movie_index() -> dict:
    from app.v80 import check_index_queue
    check_index_queue('core')
    return index_status(False)


@app.post("/api/v39/setup/movie-index/rebuild")
def rebuild_movie_index() -> dict:
    from app.v80 import rebuild_index_queue
    return rebuild_index_queue('core')


@app.get("/api/v39/movies/stream-filter-values")
def stable_movie_stream_filter_values() -> dict:
    with connection() as db:
        language_rows = db.execute("SELECT DISTINCT CASE WHEN stream_type='external' THEN 'subtitle' ELSE stream_type END AS stream_type,language FROM media_stream_index WHERE language != '' AND path IN (SELECT path FROM plex_media WHERE kind='movie')").fetchall()
        name_rows = db.execute("SELECT DISTINCT CASE WHEN stream_type='external' THEN 'subtitle' ELSE stream_type END AS stream_type,track_name FROM media_stream_index WHERE track_name != '' AND path IN (SELECT path FROM plex_media WHERE kind='movie')").fetchall()
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
