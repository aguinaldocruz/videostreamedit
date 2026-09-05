from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.v11 import connection
from app.v39 import RefreshMovieIndex
from app.v54 import cache_folder, index_core
from app.v55 import app


logger = logging.getLogger("uvicorn.error")


@app.post("/api/v57/index/media-refresh")
def refresh_changed_media(payload: RefreshMovieIndex) -> dict:
    with connection() as db:
        row = db.execute("SELECT path,modified,size,title FROM plex_media WHERE kind='movie' AND path=?", (payload.path,)).fetchone()
    if not row:
        return {"indexed": False, "reason": "Media is not in the synchronized Plex movie catalog"}
    item = dict(row)
    path = Path(item["path"])
    if not path.is_file():
        return {"indexed": False, "reason": "Media file is not accessible"}

    # Refresh the lightweight index immediately so active filters stay accurate.
    index_core(item)

    # The slower caches remain incremental and will refresh on their next Check.
    with connection() as db:
        db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (payload.path,))
        db.execute("DELETE FROM subtitle_extended_media WHERE path=?", (payload.path,))
        db.execute("DELETE FROM preview_cache_index WHERE path=?", (payload.path,))
    shutil.rmtree(cache_folder(payload.path), ignore_errors=True)
    logger.info("index_event=media_refreshed file=%s core=updated subtitles=invalidated previews=invalidated", payload.path.replace("\n", "\\n"))
    return {"indexed": True, "path": payload.path}
