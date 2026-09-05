from __future__ import annotations

import logging

from app.v11 import connection
from app.v69 import app


logger = logging.getLogger("uvicorn.error")


@app.on_event("startup")
def preserve_existing_preview_samples() -> None:
    with connection() as db:
        migrated = db.execute("SELECT 1 FROM feature_migrations WHERE name='preview_anchor_5min_v1'").fetchone()
        if migrated:
            return
        changed = db.execute("""
            UPDATE preview_cache_index
            SET modified=(SELECT modified FROM plex_media WHERE plex_media.path=preview_cache_index.path),
                size=(SELECT size FROM plex_media WHERE plex_media.path=preview_cache_index.path)
            WHERE EXISTS(SELECT 1 FROM plex_media WHERE plex_media.path=preview_cache_index.path)
        """).rowcount
        db.execute("INSERT INTO feature_migrations(name) VALUES('preview_anchor_5min_v1')")
    logger.info("index_job=previews event=five_minute_anchor_migrated preserved_media=%d cache_files=unchanged", changed)
