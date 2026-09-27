from __future__ import annotations

import logging

from fastapi import Query
from pydantic import BaseModel

from app.v11 import connection
from app.v37 import app

logger = logging.getLogger("videostreamedit")


class MovieStreamIndexInvalidate(BaseModel):
    path: str


def _value_groups(rows, field: str) -> dict[str, list[str]]:
    result = {"all": [], "audio": [], "subtitle": []}
    for row in rows:
        value = row[field]
        if not value or row['stream_type'] not in ('audio', 'subtitle'):
            continue
        result[row["stream_type"]].append(value)
        result["all"].append(value)
    return {key: sorted(set(values), key=str.casefold) for key, values in result.items()}


@app.get("/api/v38/movies/stream-filter-values")
def movie_stream_filter_values() -> dict:
    with connection() as db:
        language_rows = db.execute("SELECT DISTINCT CASE WHEN stream_type='external' THEN 'subtitle' ELSE stream_type END AS stream_type,language FROM media_stream_index WHERE language != '' AND path IN (SELECT path FROM plex_media WHERE kind='movie')").fetchall()
        name_rows = db.execute("SELECT DISTINCT CASE WHEN stream_type='external' THEN 'subtitle' ELSE stream_type END AS stream_type,track_name FROM media_stream_index WHERE track_name != '' AND path IN (SELECT path FROM plex_media WHERE kind='movie')").fetchall()
        indexed = db.execute("SELECT count(*) FROM media_stream_index_state s JOIN plex_media p ON p.path=s.path WHERE p.kind=\'movie\'").fetchone()[0]
        movies = db.execute("SELECT count(*) FROM plex_media WHERE kind='movie'").fetchone()[0]
    from app.v39 import index_status
    status = index_status()
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
        if stream_type == "subtitle":
            clauses.append("(value.stream_type='subtitle' OR value.stream_type='external')")
        else:
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
            "SELECT DISTINCT media.path FROM plex_media media JOIN media_stream_index value ON value.path=media.path WHERE " + " AND ".join(clauses),
            values,
        ).fetchall()
    return {"paths": [row["path"] for row in rows]}


@app.post("/api/v38/movies/stream-filter-invalidate")
def invalidate_movie_stream_filter(payload: MovieStreamIndexInvalidate) -> dict:
    from app.v80 import request_media_indexes
    request_media_indexes(payload.path, ["core"], "Movie stream index refresh requested", defer_detection=True)
    logger.info("change=movie_stream_index_invalidated path=%s", payload.path)
    return {"invalidated": True}
