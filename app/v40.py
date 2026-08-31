from __future__ import annotations

import logging

from pydantic import BaseModel

import app.v7 as media_editor
import app.v28 as movie_import
from app.v11 import connection
from app.v39 import app


logger = logging.getLogger("videostreamedit")


class TrackNameCandidate(BaseModel):
    stream_type: str
    value: str = ""


class TrackNameSuggestionRequest(BaseModel):
    values: list[TrackNameCandidate] = []


@app.on_event("startup")
def initialize_track_name_corrections() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS track_name_correction_history (
                stream_type TEXT NOT NULL CHECK(stream_type IN ('audio','subtitle')),
                old_value TEXT NOT NULL, new_value TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 1,
                last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(stream_type, old_value, new_value)
            );
            CREATE INDEX IF NOT EXISTS track_name_correction_lookup
                ON track_name_correction_history(stream_type, old_value, use_count DESC);
        """)


def existing_track_names(request: media_editor.ReorderEditRequest) -> dict[tuple[str, int], str]:
    source = media_editor.authorized_file(request.path)
    counters = {"audio": 0, "subtitle": 0}
    result: dict[tuple[str, int], str] = {}
    for stream in media_editor.probe(source).get("streams", []):
        stream_type = stream.get("codec_type")
        if stream_type not in counters:
            continue
        type_index = counters[stream_type]
        counters[stream_type] += 1
        result[(stream_type, type_index)] = str((stream.get("tags") or {}).get("title") or "").strip()
    return result


def record_track_name_corrections(request: media_editor.ReorderEditRequest, before: dict[tuple[str, int], str]) -> None:
    removed = set(request.remove)
    changes: list[tuple[str, str, str]] = []
    for track in request.tracks:
        if track.title is None or f"embedded:{track.codec_type}:{track.type_index}" in removed:
            continue
        old_value = before.get((track.codec_type, track.type_index), "")
        new_value = track.title.strip()
        if old_value != new_value:
            changes.append((track.codec_type, old_value, new_value))
    for subtitle in request.external_subtitles:
        key = f"external:{subtitle.path}"
        if subtitle.embed and key not in removed and subtitle.title.strip():
            changes.append(("subtitle", "", subtitle.title.strip()))
    if not changes:
        return
    with connection() as db:
        for stream_type, old_value, new_value in changes:
            db.execute(
                """INSERT INTO track_name_correction_history(stream_type,old_value,new_value,use_count)
                   VALUES(?,?,?,1)
                   ON CONFLICT(stream_type,old_value,new_value) DO UPDATE
                   SET use_count=use_count+1,last_used=CURRENT_TIMESTAMP""",
                (stream_type, old_value, new_value),
            )
            logger.info(
                "change=track_name_correction_learned stream=%s from=%s to=%s",
                stream_type, old_value.replace("\n", "\\n"), new_value.replace("\n", "\\n"),
            )


@app.post("/api/v40/media/edit")
def edit_and_learn_track_names(request: media_editor.ReorderEditRequest) -> dict:
    before = existing_track_names(request)
    result = media_editor.reorder_edit(request)
    record_track_name_corrections(request, before)
    return result


movie_import.reorder_edit = edit_and_learn_track_names


@app.post("/api/v40/track-name-suggestions")
def track_name_suggestions(request: TrackNameSuggestionRequest) -> dict:
    unique = {(item.stream_type, item.value.strip()) for item in request.values if item.stream_type in {"audio", "subtitle"}}
    suggestions = []
    with connection() as db:
        for stream_type, old_value in unique:
            rows = db.execute(
                """SELECT new_value,use_count
                     FROM track_name_correction_history
                    WHERE stream_type=? AND old_value=?
                    ORDER BY use_count DESC,last_used DESC,new_value COLLATE NOCASE""",
                (stream_type, old_value),
            ).fetchall()
            if not rows:
                continue
            total = sum(row["use_count"] for row in rows)
            best = rows[0]
            if best["use_count"] >= 2 and best["use_count"] / total >= 0.60:
                suggestions.append({
                    "stream_type": stream_type,
                    "old_value": old_value,
                    "new_value": best["new_value"],
                    "use_count": best["use_count"],
                })
    return {"suggestions": suggestions}
