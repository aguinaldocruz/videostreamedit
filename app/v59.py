from __future__ import annotations

import logging
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

import app.v43 as optimized_editor
from app.v2 import make_language
from app.v7 import ReorderEditRequest
from app.v11 import connection
from app.v57 import app


logger = logging.getLogger("uvicorn.error")


class SuggestionValue(BaseModel):
    stream_type: Literal["audio", "subtitle"]
    language: str = ""
    value: str = ""


class SuggestionRequest(BaseModel):
    values: list[SuggestionValue] = []


class SuggestionAction(BaseModel):
    stream_type: Literal["audio", "subtitle"]
    track_language: str = ""
    old_value: str
    new_value: str
    action: Literal["pause", "resume", "reset", "rename"]
    replacement: str | None = None


class SuggestionDelete(BaseModel):
    stream_type: Literal["audio", "subtitle"]
    track_language: str = ""
    old_value: str
    new_value: str


@app.on_event("startup")
def migrate_language_aware_suggestions() -> None:
    with connection() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(track_name_correction_history)")}
        if "track_language" not in columns:
            db.execute("DROP TABLE IF EXISTS track_name_correction_history")
            db.execute("DROP INDEX IF EXISTS track_name_correction_lookup")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS track_name_correction_history (
                stream_type TEXT NOT NULL CHECK(stream_type IN ('audio','subtitle')),
                track_language TEXT NOT NULL DEFAULT '', old_value TEXT NOT NULL, new_value TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 1, last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(stream_type,track_language,old_value,new_value)
            );
            CREATE INDEX IF NOT EXISTS track_name_correction_lookup
                ON track_name_correction_history(stream_type,track_language,old_value,use_count DESC);
        """)
    if "track_language" not in columns:
        logger.info("change=track_name_suggestions_migrated scope=language_aware previous_history=cleared")


def normalized(value: str) -> str:
    primary = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return {"por": "pt", "eng": "en"}.get(primary, primary)


def mapping_allowed(db, stream_type: str, language: str, old_value: str, new_value: str, exclude: tuple[str, str] | None = None) -> tuple[bool, str]:
    """Keep learned corrections as one-way, single-target mappings."""
    rows = db.execute(
        "SELECT old_value,new_value FROM track_name_correction_history WHERE stream_type=? AND track_language=?",
        (stream_type, language),
    ).fetchall()
    edges = [(row["old_value"], row["new_value"]) for row in rows if exclude is None or (row["old_value"], row["new_value"]) != exclude]
    if any(source == old_value and target == new_value for source, target in edges):
        return True, "same_mapping"
    if any(source == old_value for source, _ in edges):
        return False, "source_already_has_target"
    if any(target == old_value for _, target in edges):
        return False, "source_is_existing_target"
    graph: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
    pending, visited = [new_value], set()
    while pending:
        value = pending.pop()
        if value == old_value:
            return False, "circular_mapping"
        if value in visited:
            continue
        visited.add(value)
        pending.extend(graph.get(value, ()))
    return True, "new_mapping"


def existing_properties(request: ReorderEditRequest) -> dict[tuple[str, int], tuple[str, str]]:
    source = optimized_editor.media_editor.authorized_file(request.path)
    counters = {"audio": 0, "subtitle": 0}
    result = {}
    for stream in optimized_editor.media_editor.probe(source).get("streams", []):
        stream_type = stream.get("codec_type")
        if stream_type not in counters:
            continue
        index = counters[stream_type]; counters[stream_type] += 1
        tags = stream.get("tags") or {}
        result[(stream_type, index)] = (str(tags.get("title") or "").strip(), normalized(str(tags.get("language") or "")))
    return result


def record_language_aware_corrections(request: ReorderEditRequest, before: dict) -> None:
    removed = set(request.remove)
    changes = []
    for track in request.tracks:
        if track.title is None or f"embedded:{track.codec_type}:{track.type_index}" in removed:
            continue
        old_title, old_language = before.get((track.codec_type, track.type_index), ("", ""))
        language = normalized(make_language(track.language, track.region)) if track.language is not None or track.region is not None else old_language
        new_title = track.title.strip()
        if old_title != new_title:
            changes.append((track.codec_type, language, old_title, new_title))
    for subtitle in request.external_subtitles:
        key = f"external:{subtitle.path}"
        if subtitle.embed and key not in removed and subtitle.title.strip():
            changes.append(("subtitle", normalized(make_language(subtitle.language, subtitle.region)), "", subtitle.title.strip()))
    with connection() as db:
        for stream_type, language, old_value, new_value in changes:
            allowed, reason = mapping_allowed(db, stream_type, language, old_value, new_value)
            if not allowed:
                logger.info("change=track_name_correction_skipped reason=%s stream=%s language=%s from=%s to=%s", reason, stream_type, language, old_value.replace("\n", "\\n"), new_value.replace("\n", "\\n"))
                continue
            db.execute("""
                INSERT INTO track_name_correction_history(stream_type,track_language,old_value,new_value,use_count)
                VALUES(?,?,?,?,1) ON CONFLICT(stream_type,track_language,old_value,new_value) DO UPDATE SET
                use_count=use_count+1,last_used=CURRENT_TIMESTAMP
            """, (stream_type, language, old_value, new_value))
            logger.info("change=track_name_correction_learned stream=%s language=%s from=%s to=%s", stream_type, language, old_value.replace("\n", "\\n"), new_value.replace("\n", "\\n"))


def language_aware_old_properties(typed: dict[str, list[dict]]) -> dict[tuple[str, int], tuple[str, str]]:
    return {(stream_type, index): (str((stream.get("tags") or {}).get("title") or "").strip(), normalized(str((stream.get("tags") or {}).get("language") or ""))) for stream_type, streams in typed.items() for index, stream in enumerate(streams)}


optimized_editor.old_track_names = language_aware_old_properties
optimized_editor.record_track_name_corrections = record_language_aware_corrections


def suggestions() -> list[dict]:
    with connection() as db:
        rows = db.execute("SELECT stream_type,track_language,old_value,new_value,use_count,last_used,enabled FROM track_name_correction_history ORDER BY stream_type,track_language,use_count DESC,last_used DESC,old_value COLLATE NOCASE,new_value COLLATE NOCASE").fetchall()
    return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]


@app.get("/api/v59/settings/track-name-suggestions")
def list_suggestions() -> dict:
    return {"suggestions": suggestions()}


@app.post("/api/v59/track-name-suggestions")
def applicable_suggestions(request: SuggestionRequest) -> dict:
    unique = {(item.stream_type, normalized(item.language), item.value.strip()) for item in request.values}
    found = []
    with connection() as db:
        for stream_type, language, old_value in unique:
            rows = db.execute("SELECT new_value,use_count FROM track_name_correction_history WHERE stream_type=? AND track_language=? AND old_value=? AND enabled=1 ORDER BY use_count DESC,last_used DESC,new_value COLLATE NOCASE", (stream_type, language, old_value)).fetchall()
            if not rows:
                continue
            total = sum(row["use_count"] for row in rows); best = rows[0]
            allowed, _ = mapping_allowed(db, stream_type, language, old_value, best["new_value"], (old_value, best["new_value"]))
            if allowed and best["use_count"] >= 2 and best["use_count"] / total >= .60:
                found.append({"stream_type": stream_type, "track_language": language, "old_value": old_value, "new_value": best["new_value"], "use_count": best["use_count"]})
    return {"suggestions": found}


@app.put("/api/v59/settings/track-name-suggestions")
def maintain_suggestion(request: SuggestionAction) -> dict:
    key = (request.stream_type, normalized(request.track_language), request.old_value, request.new_value)
    with connection() as db:
        row = db.execute("SELECT use_count,enabled FROM track_name_correction_history WHERE stream_type=? AND track_language=? AND old_value=? AND new_value=?", key).fetchone()
        if not row:
            raise HTTPException(404, "Learned suggestion was not found")
        if request.action in {"pause", "resume"}:
            if request.action == "resume":
                allowed, reason = mapping_allowed(db, *key[:2], key[2], key[3], (key[2], key[3]))
                if not allowed:
                    raise HTTPException(409, f"Cannot resume this learned mapping: {reason.replace('_', ' ')}")
            db.execute("UPDATE track_name_correction_history SET enabled=? WHERE stream_type=? AND track_language=? AND old_value=? AND new_value=?", (int(request.action == "resume"), *key))
        elif request.action == "reset":
            allowed, reason = mapping_allowed(db, *key[:2], key[2], key[3], (key[2], key[3]))
            if not allowed:
                raise HTTPException(409, f"Cannot reset this learned mapping: {reason.replace('_', ' ')}")
            db.execute("UPDATE track_name_correction_history SET use_count=1,enabled=1,last_used=CURRENT_TIMESTAMP WHERE stream_type=? AND track_language=? AND old_value=? AND new_value=?", key)
        else:
            replacement = (request.replacement or "").strip()
            if not replacement:
                raise HTTPException(400, "Replacement track name cannot be empty")
            if replacement != request.new_value:
                allowed, reason = mapping_allowed(db, *key[:2], key[2], replacement, (key[2], key[3]))
                if not allowed:
                    raise HTTPException(409, f"Cannot save this learned mapping: {reason.replace('_', ' ')}")
                db.execute("INSERT INTO track_name_correction_history(stream_type,track_language,old_value,new_value,use_count,enabled) VALUES(?,?,?,?,?,?) ON CONFLICT(stream_type,track_language,old_value,new_value) DO UPDATE SET use_count=use_count+excluded.use_count,last_used=CURRENT_TIMESTAMP,enabled=excluded.enabled", (*key[:3], replacement, row["use_count"], row["enabled"]))
                db.execute("DELETE FROM track_name_correction_history WHERE stream_type=? AND track_language=? AND old_value=? AND new_value=?", key)
    logger.info("change=learned_suggestion action=%s stream=%s language=%s", request.action, request.stream_type, normalized(request.track_language))
    return {"suggestions": suggestions()}


@app.delete("/api/v59/settings/track-name-suggestions")
def delete_suggestion(request: SuggestionDelete) -> dict:
    key = (request.stream_type, normalized(request.track_language), request.old_value, request.new_value)
    with connection() as db:
        removed = db.execute("DELETE FROM track_name_correction_history WHERE stream_type=? AND track_language=? AND old_value=? AND new_value=?", key).rowcount
    return {"removed": bool(removed), "suggestions": suggestions()}


@app.delete("/api/v59/settings/track-name-suggestions/all")
def clear_suggestions() -> dict:
    with connection() as db:
        removed = db.execute("DELETE FROM track_name_correction_history").rowcount
    logger.info("change=learned_suggestion action=delete_all removed=%d", removed)
    return {"removed": removed, "suggestions": []}
