from __future__ import annotations

from app.v11 import connection
from app.v39 import index_status
from app.v52 import app


def groups(rows, field: str) -> dict[str, list[str]]:
    result = {"all": [], "audio": [], "subtitle": []}
    for row in rows:
        value = row[field]
        if value:
            result[row["stream_type"]].append(value)
            result["all"].append(value)
    return {key: sorted(set(values), key=str.casefold) for key, values in result.items()}


@app.get("/api/v53/movies/stream-filter-values")
def stable_extended_filter_values() -> dict:
    with connection() as db:
        languages = db.execute("SELECT DISTINCT stream_type,language FROM movie_stream_index_value WHERE language!=''").fetchall()
        names = db.execute("SELECT DISTINCT stream_type,track_name FROM movie_stream_index_value WHERE track_name!=''").fetchall()
        encodings = [row[0] for row in db.execute("SELECT DISTINCT encoding FROM subtitle_extended_index WHERE encoding!='' ORDER BY encoding COLLATE NOCASE")]
        markup = [row[0] for row in db.execute("SELECT DISTINCT markup FROM subtitle_extended_index WHERE markup!='' ORDER BY markup COLLATE NOCASE")]
    return {"languages": groups(languages, "language"), "track_names": groups(names, "track_name"), "subtitle_encodings": encodings, "subtitle_markup": markup, "status": index_status()}
