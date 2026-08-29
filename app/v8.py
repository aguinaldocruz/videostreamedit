from __future__ import annotations

import logging
from typing import Literal

from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.v2 import connection
from app.v7 import STATIC_DIR, app, asset

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


@app.middleware("http")
async def v8_assets(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text().replace('href="/app.css"', 'href="/assets/v8.css"').replace('src="/app.js"', 'src="/assets/v8.js"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v8.css":
        css = "\n".join((STATIC_DIR / name).read_text() for name in ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css"))
        css = css.replace("@import url('/base.css');", "").replace("@import url('/previous.css');", "")
        return asset(css, "text/css")
    if request.method == "GET" and request.url.path == "/assets/v8.js":
        javascript = (STATIC_DIR / "v5.js").read_text().replace("'/api/tv'", "'/api/v7/tv'")
        javascript += "\n" + (STATIC_DIR / "v8-addon.js").read_text()
        javascript += "\n" + (STATIC_DIR / "v9-session.js").read_text()
        javascript += "\n" + (STATIC_DIR / "v10-progress.js").read_text()
        return asset(javascript, "text/javascript")
    return await call_next(request)


@app.get("/api/v8/saved-values")
def saved_values() -> dict[str, list[str]]:
    result = {"language": [], "region": [], "title_audio": [], "title_subtitle": []}
    with connection() as db:
        rows = db.execute("SELECT field, value FROM reusable_stream_values WHERE saved = 1 ORDER BY value COLLATE NOCASE").fetchall()
    for row in rows:
        result[row["field"]].append(row["value"])
    return result


@app.post("/api/v8/value-uses")
def record_value_uses(request: ValueUsesRequest) -> dict:
    prompts = []
    with connection() as db:
        for item in request.values:
            value = item.value.strip()
            if not value:
                continue
            db.execute(
                """INSERT INTO reusable_stream_values(field, value, use_count)
                   VALUES (?, ?, 1)
                   ON CONFLICT(field, value) DO UPDATE SET use_count = use_count + 1""",
                (item.field, value),
            )
        rows = db.execute(
            "SELECT field, value FROM reusable_stream_values WHERE use_count >= 1 AND saved = 0 AND prompted = 0 ORDER BY field, value COLLATE NOCASE"
        ).fetchall()
        prompts = [dict(row) for row in rows]
    return {"prompts": prompts}


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
