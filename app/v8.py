from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.v2 import connection
from app.v7 import STATIC_DIR, app

logger = logging.getLogger("uvicorn.error")


ValueField = Literal["language", "region", "title_audio", "title_subtitle"]


class ValueUse(BaseModel):
    field: ValueField
    value: str


class ValueUsesRequest(BaseModel):
    values: list[ValueUse] = []


class SavedValueDecision(BaseModel):
    field: ValueField
    value: str
    save: bool


@app.on_event("startup")
def initialize_reusable_values() -> None:
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reusable_stream_values (
                field TEXT NOT NULL CHECK(field IN ('language', 'region', 'title_audio', 'title_subtitle')),
                value TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0,
                saved INTEGER NOT NULL DEFAULT 0,
                prompted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(field, value)
            )
        """)




@app.get("/api/v8/saved-values")
def saved_values() -> dict:
    result = {"language": [], "region": [], "title_audio": [], "title_subtitle": [], "language_region_usage": {}, "language_region_order": []}
    with connection() as db:
        rows = db.execute(
            """SELECT field, value, use_count
               FROM reusable_stream_values
               WHERE saved = 1
               ORDER BY field, use_count DESC, value COLLATE NOCASE"""
        ).fetchall()
        usage_table = db.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='language_region_selection_usage'"
        ).fetchone()
        usage_rows = db.execute(
            "SELECT value, use_count FROM language_region_selection_usage"
        ).fetchall() if usage_table else []
        order_row = db.execute("SELECT value FROM application_settings WHERE key='language_region_order'").fetchone()
    if order_row:
        try:
            result["language_region_order"] = [str(v) for v in json.loads(order_row["value"] or "[]") if str(v).strip()]
        except Exception:
            result["language_region_order"] = []
    # Merge explicitly saved values with learned track-name corrections and
    # rank the combined list by actual use count. Previously learned values
    # were always prepended, which made the visible order unrelated to usage.
    value_counts = {field: {} for field in result if field != "language_region_usage"}
    for row in rows:
        field, value = str(row["field"]), str(row["value"] or "").strip()
        if field in value_counts and value:
            value_counts[field][value] = value_counts[field].get(value, 0) + int(row["use_count"] or 0)
    # Learned corrections are reusable track-name choices, ranked by use.
    learned = []
    try:
        with connection() as learned_db:
            learned = learned_db.execute(
                "SELECT stream_type,new_value,use_count FROM track_name_correction_history WHERE enabled=1 AND TRIM(new_value) <> '' ORDER BY use_count DESC,last_used DESC"
            ).fetchall()
    except Exception as exc:
        logger.warning("saved_values event=learned_track_names_unavailable error=%s", str(exc).replace("\n", " ")[-300:])
    for row in learned:
        field = "title_audio" if str(row["stream_type"]) == "audio" else "title_subtitle"
        value = str(row["new_value"] or "").strip()
        if value:
            value_counts[field][value] = value_counts[field].get(value, 0) + int(row["use_count"] or 0)
    for field in ("language", "region", "title_audio", "title_subtitle"):
        result[field] = [value for value, _count in sorted(value_counts[field].items(), key=lambda item: (-item[1], item[0].casefold()))]
    result["language_region_usage"] = {row["value"]: row["use_count"] for row in usage_rows}
    return result


class LanguageRegionOrder(BaseModel):
    values: list[str] = []


@app.put("/api/v8/saved-values/language-region-order")
def save_language_region_order(request: LanguageRegionOrder) -> dict:
    values=[]
    for value in request.values:
        value=str(value).strip()
        if value and value not in values: values.append(value)
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO application_settings(key,value) VALUES('language_region_order',?)", (json.dumps(values, ensure_ascii=False),))
    return {"language_region_order": values}


@app.get("/api/v8/language-region-values")
def language_region_values() -> dict:
    with connection() as db:
        # The editor remains usable during a rebuild or before the index tables exist.
        try:
            rows=db.execute("SELECT COALESCE(language,'') AS language, COALESCE(region,'') AS region, COUNT(*) AS indexed_count FROM media_stream_index WHERE TRIM(COALESCE(language,''))<>'' OR TRIM(COALESCE(region,''))<>'' GROUP BY language,region ORDER BY language,region").fetchall()
        except Exception as exc:
            logger.warning("language_region_values event=index_unavailable error=%s", str(exc).replace(chr(10), " ")[-300:])
            rows=[]
        try:
            custom=db.execute("SELECT value FROM application_settings WHERE key='language_region_custom'").fetchone()
            ordered=db.execute("SELECT value FROM application_settings WHERE key='language_region_order'").fetchone()
        except Exception as exc:
            logger.warning("language_region_values event=settings_unavailable error=%s", str(exc).replace(chr(10), " ")[-300:])
            custom=ordered=None
        try:
            usage_rows=db.execute("SELECT value, use_count FROM language_region_selection_usage").fetchall()
        except Exception:
            usage_rows=[]
    usage={str(row["value"] if hasattr(row, "keys") else row[0]): int(row["use_count"] if hasattr(row, "keys") else row[1] or 0) for row in usage_rows}
    indexed_usage={}
    used=[]
    for row in rows:
        language = row["language"] if hasattr(row, "keys") else row[0]
        region = row["region"] if hasattr(row, "keys") else row[1]
        value=f"{str(language or '').strip().lower()}|{str(region or '').strip().upper()}"
        indexed_usage[value] = int(row["indexed_count"] if hasattr(row, "keys") else row[2] or 0)
        if value not in used: used.append(value)
    try: custom_values=[str(v) for v in json.loads(custom['value'] if custom else '[]') if str(v).strip()]
    except Exception: custom_values=[]
    try: order_values=[str(v) for v in json.loads(ordered['value'] if ordered else '[]') if str(v).strip()]
    except Exception: order_values=[]
    values=[]
    for value in order_values+used+custom_values:
        if value not in values: values.append(value)
    return {"values":values,"used":used,"usage":usage,"indexed_usage":indexed_usage}


@app.put("/api/v8/language-region-values")
def save_language_region_values(request: LanguageRegionOrder) -> dict:
    values=[]
    for value in request.values:
        value=str(value).strip().lower()
        if '|' not in value: continue
        language,region=value.split('|',1)
        value=f"{language}|{region.upper()}"
        if value and value not in values: values.append(value)
    current=language_region_values()
    missing=[value for value in current['used'] if value not in values]
    if missing: raise HTTPException(409, "Used language-region values cannot be removed: " + ', '.join(missing))
    custom=[value for value in values if value not in current['used']]
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO application_settings(key,value) VALUES('language_region_order',?)", (json.dumps(values,ensure_ascii=False),))
        db.execute("INSERT OR REPLACE INTO application_settings(key,value) VALUES('language_region_custom',?)", (json.dumps(custom,ensure_ascii=False),))
    return {"values":values,"used":current['used']}


@app.post("/api/v8/value-uses")
def record_value_uses(request: ValueUsesRequest) -> dict:
    # One editing operation is one use, even when it changes several streams.
    used = {(item.field, item.value.strip()) for item in request.values if item.value.strip()}
    available = saved_values()
    with connection() as db:
        for field, value in sorted(used):
            db.execute(
                """INSERT INTO reusable_stream_values(field, value, use_count)
                   VALUES (?, ?, 1)
                   ON CONFLICT(field, value) DO UPDATE SET use_count = reusable_stream_values.use_count + 1""",
                (field, value),
            )
        rows = db.execute(
            "SELECT field, value FROM reusable_stream_values WHERE use_count >= 2 AND saved = 0 AND prompted = 0 ORDER BY field, value COLLATE NOCASE"
        ).fetchall()
        prompts = [dict(row) for row in rows
                   if (row['field'], row['value']) in used
                   and row['field'] in ('title_audio', 'title_subtitle')
                   and row['value'] not in available.get(row['field'], [])]
    return {"prompts": prompts}


@app.get('/api/v8/declined-track-names')
def declined_track_names() -> dict:
    with connection() as db:
        rows = db.execute("SELECT field,value,use_count FROM reusable_stream_values WHERE saved=0 AND prompted=1 AND field IN ('title_audio','title_subtitle') ORDER BY field,value COLLATE NOCASE").fetchall()
    return {'values': [dict(row) for row in rows]}


@app.delete('/api/v8/declined-track-names')
def clear_declined_track_names() -> dict:
    # Start fresh: two new editing uses are needed before another question.
    with connection() as db:
        removed = db.execute("DELETE FROM reusable_stream_values WHERE saved=0 AND prompted=1 AND field IN ('title_audio','title_subtitle')").rowcount
    return {'removed': removed}


@app.post("/api/v8/saved-values")
def decide_saved_value(request: SavedValueDecision) -> dict:
    value = request.value.strip()
    if value:
        with connection() as db:
            db.execute(
                """INSERT INTO reusable_stream_values(field, value, use_count, saved, prompted)
                   VALUES (?, ?, 0, ?, 1)
                   ON CONFLICT(field, value) DO UPDATE SET saved = excluded.saved, prompted = 1""",
                (request.field, value, int(request.save)),
            )
        logger.info("change=saved_stream_value field=%s value=%s saved=%s", request.field, value.replace("\n", "\\n"), str(request.save).lower())
    return {"saved": bool(value and request.save)}
