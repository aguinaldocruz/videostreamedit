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
        languages = db.execute("SELECT DISTINCT CASE WHEN stream_type='external' THEN 'subtitle' ELSE stream_type END AS stream_type,language FROM media_stream_index WHERE language!=''").fetchall()
        names = db.execute("SELECT DISTINCT CASE WHEN stream_type='external' THEN 'subtitle' ELSE stream_type END AS stream_type,track_name FROM media_stream_index WHERE track_name!=''").fetchall()
        encodings = sorted({row[0] for row in db.execute("SELECT DISTINCT encoding FROM subtitle_extended_index WHERE encoding!=''")}, key=str.casefold)
        markup = sorted({row[0] for row in db.execute("SELECT DISTINCT markup FROM subtitle_extended_index WHERE markup!=''")}, key=str.casefold)
    return {"languages": groups(languages, "language"), "track_names": groups(names, "track_name"), "subtitle_encodings": encodings, "subtitle_markup": markup, "status": index_status()}
