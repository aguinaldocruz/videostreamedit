from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

import app.v11 as plex
import app.v54 as indexes
import app.v65 as tasks
import app.v68 as plex_sync
from fastapi import HTTPException
from app.plex_secret import decrypt_token
from app.v73 import app


logger = logging.getLogger("uvicorn.error")


def plex_scan(library_key: str, folder: Path) -> None:
    saved = plex.config_row()
    if not saved:
        raise RuntimeError("Plex is not configured")
    url = str(saved["url"]).rstrip("/") + f"/library/sections/{urllib.parse.quote(library_key)}/refresh?path={urllib.parse.quote(str(folder))}"
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Plex-Token": decrypt_token(saved["token"]),
        "X-Plex-Client-Identifier": "videostreamedit",
        "X-Plex-Product": "VideoStreamEdit",
        "X-Plex-Version": "0.11",
    })
    with urllib.request.urlopen(request, timeout=45) as response:
        response.read()


def item_for_path(items: list[dict], target: str, rating_key: str) -> dict | None:
    for item in items:
        paths = [part.get("file") for media in item.get("Media", []) for part in media.get("Part", [])]
        if target in paths and (not rating_key or str(item.get("ratingKey") or "") == rating_key):
            return item
    return None


def process_plex_import_refresh(task_id: int, payload: dict) -> dict:
    target = Path(str(payload.get("path") or "")).resolve()
    library_key = str(payload.get("library_key") or "")
    rating_key = str(payload.get("rating_key") or "")
    if not target.is_file():
        raise RuntimeError(f"Imported media is not accessible: {target}")
    with plex.connection() as db:
        library = db.execute(
            "SELECT library_key,title,kind FROM plex_libraries WHERE library_key=? AND selected=1",
            (library_key,),
        ).fetchone()
    if not library or library["kind"] != "movie":
        raise RuntimeError("The destination Plex movie library is not selected")
    tasks.update_progress(task_id, 0, 4, "Requesting Plex destination scan")
    plex_scan(library_key, target.parent)
    logger.info("plex_sync event=post_import_scan_requested library=%s folder=%s", library_key, str(target.parent).replace("\n", "\\n"))
    found = None
    started = int(time.time()) - 10
    for attempt in range(1, 19):
        tasks.update_progress(task_id, 1, 4, f"Waiting for Plex to discover movie ({attempt}/18)")
        time.sleep(5 if attempt > 1 else 2)
        if rating_key:
            try:
                metadata = plex.plex_request(
                    f"/library/metadata/{urllib.parse.quote(rating_key)}"
                ).get("MediaContainer", {}).get("Metadata", [])
            except HTTPException:
                metadata = []
            found = item_for_path(metadata, str(target), rating_key)
        if not found:
            changed = plex_sync.changed_library_items(dict(library), started)
            found = item_for_path(changed, str(target), "")
        if not found and (attempt == 1 or attempt % 4 == 0):
            # Plex may assign a new rating key while preserving old added/updated
            # timestamps after a filesystem rename. A bounded full lookup is the
            # only reliable fallback for that case.
            found = item_for_path(plex_sync.paged_library(library_key, "movie"), str(target), "")
        if found:
            break
    if not found:
        raise RuntimeError("Plex did not expose the imported path within 87 seconds; retry this task after its scan completes")
    tasks.update_progress(task_id, 2, 4, "Updating local Plex catalog")
    records, aliases = plex_sync.rows_for_items(dict(library), [found])
    plex_sync.persist_library(dict(library), records, aliases, int(time.time()), False)
    stat = target.stat()
    item = {"path": str(target), "title": found.get("title") or target.stem, "modified": int(stat.st_mtime), "size": stat.st_size}
    tasks.update_progress(task_id, 3, 4, "Queueing media indexes")
    from app.v80 import request_media_indexes
    request_media_indexes(str(target), ["core", "subtitles", "previews"], "Plex import discovered")
    tasks.update_progress(task_id, 4, 4, "Plex catalog updated; indexes queued")
    logger.info("plex_sync event=post_import_completed library=%s rating_key=%s file=%s", library_key, str(found.get("ratingKey") or ""), str(target).replace("\n", "\\n"))
    return {"path": str(target), "library_key": library_key, "rating_key": str(found.get("ratingKey") or ""), "indexes": ["core", "subtitles", "previews"]}


tasks.TASK_HANDLERS["plex_import_refresh"] = process_plex_import_refresh
