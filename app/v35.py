from __future__ import annotations

import logging

from app.v2 import connection
from app.v34 import app


logger = logging.getLogger("videostreamedit")


@app.on_event("startup")
def split_saved_track_names() -> None:
    with connection() as db:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reusable_stream_values'"
        ).fetchone()
        schema = row["sql"] if row else ""
        if "title_audio" in schema and "title_subtitle" in schema:
            return
        db.executescript("""
            ALTER TABLE reusable_stream_values RENAME TO reusable_stream_values_legacy;
            CREATE TABLE reusable_stream_values (
                field TEXT NOT NULL CHECK(field IN ('language', 'region', 'title_audio', 'title_subtitle')),
                value TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0,
                saved INTEGER NOT NULL DEFAULT 0,
                prompted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(field, value)
            );
            INSERT INTO reusable_stream_values(field,value,use_count,saved,prompted)
                SELECT field,value,use_count,saved,prompted
                FROM reusable_stream_values_legacy WHERE field IN ('language','region');
            INSERT INTO reusable_stream_values(field,value,use_count,saved,prompted)
                SELECT 'title_audio',value,use_count,saved,prompted
                FROM reusable_stream_values_legacy WHERE field='title';
            INSERT INTO reusable_stream_values(field,value,use_count,saved,prompted)
                SELECT 'title_subtitle',value,use_count,saved,prompted
                FROM reusable_stream_values_legacy WHERE field='title';
            DROP TABLE reusable_stream_values_legacy;
        """)
    logger.info("change=saved_track_names_split migration=audio_and_subtitle")
