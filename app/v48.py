from __future__ import annotations

import logging
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

from app.v11 import connection
from app.v40 import TrackNameSuggestionRequest
from app.v44 import app


logger = logging.getLogger("uvicorn.error")


class LearnedSuggestionAction(BaseModel):
    stream_type: Literal["audio", "subtitle"]
    old_value: str
    new_value: str
    action: Literal["pause", "resume", "reset", "rename"]
    replacement: str | None = None


class LearnedSuggestionDelete(BaseModel):
    stream_type: Literal["audio", "subtitle"]
    old_value: str
    new_value: str


@app.on_event("startup")
def initialize_learned_suggestion_maintenance() -> None:
    with connection() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(track_name_correction_history)")}
        if "enabled" not in columns:
            db.execute("ALTER TABLE track_name_correction_history ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")


def learned_suggestions() -> list[dict]:
    with connection() as db:
        rows = db.execute("""
            SELECT stream_type,old_value,new_value,use_count,last_used,enabled
              FROM track_name_correction_history
             ORDER BY stream_type,use_count DESC,last_used DESC,old_value COLLATE NOCASE,new_value COLLATE NOCASE
        """).fetchall()
    return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]


@app.get("/api/v48/settings/track-name-suggestions")
def list_learned_suggestions() -> dict:
    return {"suggestions": learned_suggestions()}


@app.post("/api/v48/track-name-suggestions")
def enabled_track_name_suggestions(request: TrackNameSuggestionRequest) -> dict:
    unique = {(item.stream_type, item.value.strip()) for item in request.values if item.stream_type in {"audio", "subtitle"}}
    suggestions = []
    with connection() as db:
        for stream_type, old_value in unique:
            rows = db.execute("""
                SELECT new_value,use_count
                  FROM track_name_correction_history
                 WHERE stream_type=? AND old_value=? AND enabled=1
                 ORDER BY use_count DESC,last_used DESC,new_value COLLATE NOCASE
            """, (stream_type, old_value)).fetchall()
            if not rows:
                continue
            total = sum(row["use_count"] for row in rows)
            best = rows[0]
            if best["use_count"] >= 2 and best["use_count"] / total >= 0.60:
                suggestions.append({"stream_type": stream_type, "old_value": old_value, "new_value": best["new_value"], "use_count": best["use_count"]})
    return {"suggestions": suggestions}


@app.put("/api/v48/settings/track-name-suggestions")
def maintain_learned_suggestion(request: LearnedSuggestionAction) -> dict:
    with connection() as db:
        row = db.execute("""
            SELECT use_count,enabled FROM track_name_correction_history
             WHERE stream_type=? AND old_value=? AND new_value=?
        """, (request.stream_type, request.old_value, request.new_value)).fetchone()
        if not row:
            raise HTTPException(404, "Learned suggestion was not found")
        if request.action == "pause":
            db.execute("UPDATE track_name_correction_history SET enabled=0 WHERE stream_type=? AND old_value=? AND new_value=?", (request.stream_type, request.old_value, request.new_value))
        elif request.action == "resume":
            db.execute("UPDATE track_name_correction_history SET enabled=1 WHERE stream_type=? AND old_value=? AND new_value=?", (request.stream_type, request.old_value, request.new_value))
        elif request.action == "reset":
            db.execute("UPDATE track_name_correction_history SET use_count=1,enabled=1,last_used=CURRENT_TIMESTAMP WHERE stream_type=? AND old_value=? AND new_value=?", (request.stream_type, request.old_value, request.new_value))
        else:
            replacement = (request.replacement or "").strip()
            if not replacement:
                raise HTTPException(400, "Replacement track name cannot be empty")
            if replacement != request.new_value:
                db.execute("""
                    INSERT INTO track_name_correction_history(stream_type,old_value,new_value,use_count,last_used,enabled)
                    VALUES(?,?,?,?,CURRENT_TIMESTAMP,?)
                    ON CONFLICT(stream_type,old_value,new_value) DO UPDATE SET
                      use_count=use_count+excluded.use_count,last_used=CURRENT_TIMESTAMP,enabled=excluded.enabled
                """, (request.stream_type, request.old_value, replacement, row["use_count"], row["enabled"]))
                db.execute("DELETE FROM track_name_correction_history WHERE stream_type=? AND old_value=? AND new_value=?", (request.stream_type, request.old_value, request.new_value))
    logger.info("change=learned_suggestion action=%s stream=%s from=%s to=%s", request.action, request.stream_type, request.old_value.replace("\n", "\\n"), (request.replacement or request.new_value).replace("\n", "\\n"))
    return {"suggestions": learned_suggestions()}


@app.delete("/api/v48/settings/track-name-suggestions")
def delete_learned_suggestion(request: LearnedSuggestionDelete) -> dict:
    with connection() as db:
        cursor = db.execute("DELETE FROM track_name_correction_history WHERE stream_type=? AND old_value=? AND new_value=?", (request.stream_type, request.old_value, request.new_value))
    logger.info("change=learned_suggestion action=delete stream=%s from=%s to=%s", request.stream_type, request.old_value.replace("\n", "\\n"), request.new_value.replace("\n", "\\n"))
    return {"removed": bool(cursor.rowcount), "suggestions": learned_suggestions()}


@app.delete("/api/v48/settings/track-name-suggestions/all")
def clear_learned_suggestions() -> dict:
    with connection() as db:
        removed = db.execute("DELETE FROM track_name_correction_history").rowcount
    logger.info("change=learned_suggestion action=delete_all removed=%d", removed)
    return {"removed": removed, "suggestions": []}
