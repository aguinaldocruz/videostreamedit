from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

from app.v5 import external_subtitles
from app.v11 import connection, plex_authorized_file
from app.v35 import app


logger = logging.getLogger("videostreamedit")


class MediaRenameRequest(BaseModel):
    path: str
    filename: str


def renamed_subtitle_path(source: Path, target: Path, subtitle: Path) -> Path:
    suffix = subtitle.name[len(source.stem):] if subtitle.name.casefold().startswith(source.stem.casefold()) else subtitle.suffix
    return target.with_name(target.stem + suffix)


@app.post("/api/v37/media/rename")
def rename_media(request: MediaRenameRequest) -> dict:
    source = plex_authorized_file(request.path)
    filename = request.filename.strip()
    if not filename or filename != Path(filename).name or filename in {".", ".."}:
        raise HTTPException(400, "Filename cannot be empty or contain folders")
    if Path(filename).suffix.lower() != source.suffix.lower():
        raise HTTPException(400, f"Filename must keep the {source.suffix} extension")
    target = source.with_name(filename)
    if target == source:
        return {"renamed": False, "path": str(source), "external_subtitles": []}
    subtitles = [Path(item["path"]) for item in external_subtitles(source)]
    subtitle_moves = [(subtitle, renamed_subtitle_path(source, target, subtitle)) for subtitle in subtitles]
    targets = [target, *(new for _, new in subtitle_moves)]
    if len(set(targets)) != len(targets):
        raise HTTPException(409, "The new filename would give multiple files the same name")
    collisions = [path.name for path in targets if path.exists()]
    if collisions:
        raise HTTPException(409, "Target already exists: " + ", ".join(collisions))
    completed: list[tuple[Path, Path]] = []
    try:
        source.rename(target)
        completed.append((source, target))
        for old, new in subtitle_moves:
            old.rename(new)
            completed.append((old, new))
        with connection() as db:
            cursor = db.execute("UPDATE plex_media SET path=? WHERE path=?", (str(target), str(source)))
            if cursor.rowcount != 1:
                raise HTTPException(409, "The synchronized Plex item changed; synchronize and try again")
    except Exception:
        for old, new in reversed(completed):
            if new.exists() and not old.exists():
                new.rename(old)
        raise
    logger.info("change=media_file_renamed from=%s to=%s", str(source).replace("\n", "\\n"), str(target).replace("\n", "\\n"))
    for old, new in subtitle_moves:
        logger.info("change=external_subtitle_renamed from=%s to=%s", str(old).replace("\n", "\\n"), str(new).replace("\n", "\\n"))
    return {"renamed": True, "path": str(target), "external_subtitles": [str(new) for _, new in subtitle_moves]}
