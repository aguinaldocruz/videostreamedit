from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v65 as tasks
from app.v5 import split_tag
from app.v11 import connection
from app.v76 import app
from app.v7 import probe


logger = logging.getLogger("videostreamedit")


class BulkMovieTrackNameRequest(BaseModel):
    paths: list[str] = Field(min_length=6, max_length=10000)
    stream_type: Literal["audio", "subtitle"]
    language: str = Field(min_length=1, max_length=64)
    track_name: str = Field(min_length=1, max_length=512)
    new_track_name: str = Field(min_length=1, max_length=512)


def matching_index_paths(request: BulkMovieTrackNameRequest) -> set[str]:
    result = set()
    with connection() as db:
        for start in range(0, len(request.paths), 800):
            paths = request.paths[start:start + 800]
            placeholders = ",".join("?" for _ in paths)
            rows = db.execute(
                f"""SELECT DISTINCT media.path
                      FROM plex_media media
                      JOIN movie_stream_index_value value ON value.path=media.path
                     WHERE media.kind='movie' AND value.stream_type=?
                       AND value.language=? AND value.track_name=?
                       AND media.path IN ({placeholders})""",
                [request.stream_type, request.language, request.track_name, *paths],
            ).fetchall()
            result.update(row["path"] for row in rows)
    return result


def queued_edit(path: str, request: BulkMovieTrackNameRequest) -> tuple[dict, int]:
    media = Path(path)
    if not media.is_file():
        raise FileNotFoundError(path)
    streams = probe(media).get("streams", [])
    counters = {"audio": 0, "subtitle": 0}
    tracks = []
    order = []
    selections = {"default_audio": None, "forced_audio": None, "default_subtitle": None, "forced_subtitle": None}
    changed = 0
    for stream in streams:
        stream_type = stream.get("codec_type")
        if stream_type not in counters:
            continue
        type_index = counters[stream_type]
        counters[stream_type] += 1
        identifier = f"embedded:{stream_type}:{type_index}"
        order.append({"source": "embedded", "codec_type": stream_type, "type_index": type_index})
        disposition = stream.get("disposition") or {}
        if disposition.get("default"):
            selections[f"default_{stream_type}"] = identifier
        if disposition.get("forced"):
            selections[f"forced_{stream_type}"] = identifier
        if stream_type != request.stream_type:
            continue
        tags = stream.get("tags") or {}
        language, _ = split_tag(str(tags.get("language") or ""))
        title = str(tags.get("title") or "").strip()
        if language.strip() == request.language and title == request.track_name:
            tracks.append({"codec_type": stream_type, "type_index": type_index, "title": request.new_track_name.strip()})
            changed += 1
    return {
        "path": path,
        "tracks": tracks,
        "external_subtitles": [],
        "order": order,
        **selections,
        "remove": [],
    }, changed


@app.post("/api/v77/movies/bulk-track-name")
def bulk_movie_track_name(request: BulkMovieTrackNameRequest) -> dict:
    request.language = request.language.strip()
    request.track_name = request.track_name.strip()
    request.new_track_name = request.new_track_name.strip()
    if not request.language or not request.track_name or not request.new_track_name:
        raise HTTPException(400, "Language, current track name, and new track name are required")
    if request.new_track_name == request.track_name:
        raise HTTPException(400, "The new track name must be different")
    unique_paths = list(dict.fromkeys(request.paths))
    if len(unique_paths) <= 5:
        raise HTTPException(400, "Bulk track-name changes require more than five matching movies")
    request.paths = unique_paths
    allowed = matching_index_paths(request)
    if len(allowed) != len(unique_paths):
        raise HTTPException(409, "The movie filter changed. Refresh the Movies screen and confirm again")
    queued = streams = 0
    skipped = []
    for path in unique_paths:
        try:
            edit, changed = queued_edit(path, request)
        except (OSError, HTTPException) as exc:
            skipped.append({"path": path, "reason": str(getattr(exc, "detail", exc))})
            continue
        if not changed:
            skipped.append({"path": path, "reason": "No current stream exactly matches the filter"})
            continue
        title = Path(path).name
        tasks.enqueue("media_edit", {"edit": edit, "filename": title, "reindex_indexes": ["core"]}, f"Bulk track name · {title}")
        queued += 1
        streams += changed
    logger.info(
        "task_queue event=bulk_track_name_added media=%d streams=%d skipped=%d type=%s language=%s from=%s to=%s",
        queued, streams, len(skipped), request.stream_type, request.language,
        request.track_name.replace("\n", "\\n"), request.new_track_name.replace("\n", "\\n"),
    )
    if not queued:
        raise HTTPException(409, "None of the current media streams still match this filter")
    return {"queued": queued, "streams": streams, "skipped": skipped}
