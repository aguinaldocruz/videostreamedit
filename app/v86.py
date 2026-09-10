from pydantic import BaseModel, Field
from typing import Literal
import logging

from app.v11 import connection
from app.v85 import app

logger = logging.getLogger("videostreamedit")


class LanguageRegionUse(BaseModel):
    value: str = Field(min_length=1, max_length=32)


class MediaNoteRequest(BaseModel):
    entity_type: Literal["movie", "tv"]
    entity_key: str = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=4000)
    reviewed: bool | None = None


@app.on_event("startup")
def initialize_language_region_usage() -> None:
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS language_region_selection_usage (
                value TEXT PRIMARY KEY,
                use_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS media_notes (
                entity_type TEXT NOT NULL CHECK(entity_type IN ('movie','tv')),
                entity_key TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                reviewed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(entity_type, entity_key)
            )
        """)
        try:
            db.execute("ALTER TABLE media_notes ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass


@app.post("/api/v86/language-region-use")
def record_language_region_use(request: LanguageRegionUse) -> dict:
    value = request.value.strip()
    with connection() as db:
        db.execute(
            """INSERT INTO language_region_selection_usage(value, use_count)
               VALUES (?, 1)
               ON CONFLICT(value) DO UPDATE SET use_count = use_count + 1""",
            (value,),
        )
        count = db.execute(
            "SELECT use_count FROM language_region_selection_usage WHERE value=?",
            (value,),
        ).fetchone()["use_count"]
    return {"value": value, "use_count": count}


@app.get("/api/v86/dashboard/stats")
def dashboard_stats() -> dict:
    """Return compact, indexed collection statistics for the dashboard."""
    with connection() as db:
        counts = db.execute("""
            SELECT
              (SELECT count(*) FROM plex_media WHERE kind='movie') AS movies,
              (SELECT count(*) FROM plex_media WHERE kind='episode') AS episodes,
              (SELECT count(DISTINCT show_title) FROM plex_media WHERE kind='episode' AND show_title IS NOT NULL AND show_title!='') AS shows,
              (SELECT coalesce(sum(size),0) FROM plex_media WHERE kind='movie') AS movie_bytes,
              (SELECT coalesce(sum(size),0) FROM plex_media WHERE kind='episode') AS episode_bytes,
              (SELECT count(DISTINCT path) FROM media_stream_index) AS indexed_media,
              (SELECT count(*) FROM plex_media) AS catalog_media
        """).fetchone()
        language_rows = db.execute("""
            SELECT p.kind, i.stream_type, lower(trim(i.language)) AS language, count(DISTINCT i.path) AS media_count
              FROM media_stream_index i JOIN plex_media p ON p.path=i.path
             WHERE i.stream_type IN ('audio','subtitle') AND trim(coalesce(i.language,''))!=''
             GROUP BY p.kind, i.stream_type, lower(trim(i.language))
             ORDER BY p.kind, i.stream_type, media_count DESC, language
        """).fetchall()
        libraries = db.execute("""
            SELECT library_name, kind, count(*) AS media_count
              FROM plex_media GROUP BY library_name, kind ORDER BY media_count DESC, library_name
        """).fetchall()
    distributions = {'movies': {'audio': [], 'subtitle': []}, 'tv': {'audio': [], 'subtitle': []}}
    for row in language_rows:
        target = 'movies' if row['kind'] == 'movie' else 'tv'
        distributions[target][row['stream_type']].append({'language': row['language'], 'media_count': row['media_count']})
    return {
        'counts': {key: counts[key] for key in ('movies','shows','episodes','movie_bytes','episode_bytes','indexed_media','catalog_media')},
        'languages': distributions,
        'libraries': [dict(row) for row in libraries],
    }


@app.get("/api/v86/notes")
def list_media_notes(entity_type: Literal["movie", "tv"] | None = None) -> dict:
    with connection() as db:
        query = "SELECT entity_type,entity_key,note,reviewed FROM media_notes WHERE (note!='' OR reviewed=1)"
        args = []
        if entity_type:
            query += " AND entity_type=?"; args.append(entity_type)
        rows = db.execute(query, args).fetchall()
    return {"items": {f"{row['entity_type']}:{row['entity_key']}": {"note": row["note"], "reviewed": bool(row["reviewed"])} for row in rows}, "by_key": {row["entity_key"]: {"note": row["note"], "reviewed": bool(row["reviewed"])} for row in rows}}


@app.get("/api/v86/note")
def get_media_note(entity_type: Literal["movie", "tv"], entity_key: str) -> dict:
    with connection() as db:
        row = db.execute("SELECT note,reviewed FROM media_notes WHERE entity_type=? AND entity_key=?", (entity_type, entity_key)).fetchone()
    return {"entity_type": entity_type, "entity_key": entity_key, "note": row["note"] if row else "", "reviewed": bool(row["reviewed"]) if row else False}


@app.put("/api/v86/note")
def save_media_note(request: MediaNoteRequest) -> dict:
    note = request.note.strip()
    with connection() as db:
        current = db.execute("SELECT reviewed FROM media_notes WHERE entity_type=? AND entity_key=?", (request.entity_type, request.entity_key)).fetchone()
        reviewed = bool(request.reviewed) if request.reviewed is not None else bool(current["reviewed"]) if current else False
        if note or reviewed:
            db.execute("INSERT INTO media_notes(entity_type,entity_key,note,reviewed,updated_at) VALUES(?,?,?, ?,CURRENT_TIMESTAMP) ON CONFLICT(entity_type,entity_key) DO UPDATE SET note=excluded.note,reviewed=excluded.reviewed,updated_at=CURRENT_TIMESTAMP", (request.entity_type, request.entity_key, note, int(reviewed)))
        else:
            db.execute("DELETE FROM media_notes WHERE entity_type=? AND entity_key=?", (request.entity_type, request.entity_key))
    logger.info("change=media_note_saved type=%s key=%s present=%s reviewed=%s", request.entity_type, request.entity_key.replace("\n", " ")[:200], bool(note), reviewed)
    return {"entity_type": request.entity_type, "entity_key": request.entity_key, "note": note, "reviewed": reviewed}
