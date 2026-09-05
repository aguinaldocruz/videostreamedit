import logging
from pathlib import Path

from app.v11 import connection
from app.v71 import app


logger = logging.getLogger("uvicorn.error")


@app.on_event("startup")
def remove_duplicate_stale_plex_paths() -> None:
    """Remove only missing paths that duplicate an accessible Plex item."""
    removed: list[str] = []
    with connection() as db:
        groups = db.execute("""
            SELECT library_key,rating_key FROM plex_media
            WHERE rating_key!='' GROUP BY library_key,rating_key HAVING count(*)>1
        """).fetchall()
        for library_key, rating_key in groups:
            rows = db.execute(
                "SELECT path FROM plex_media WHERE library_key=? AND rating_key=?",
                (library_key, rating_key),
            ).fetchall()
            existing = [row[0] for row in rows if Path(row[0]).is_file()]
            if not existing:
                continue
            stale = [row[0] for row in rows if not Path(row[0]).is_file()]
            db.executemany("DELETE FROM plex_media WHERE path=?", [(path,) for path in stale])
            db.executemany("DELETE FROM plex_title_aliases WHERE path=?", [(path,) for path in stale])
            removed.extend(stale)
    if removed:
        logger.info("plex_sync event=stale_duplicate_paths_removed count=%d", len(removed))
