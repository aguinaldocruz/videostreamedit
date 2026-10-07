from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from app.v11 import connection
from app.v43 import app
from app import subtitle_autofix as _subtitle_autofix  # Register durable rule settings and sample preview.

logger = logging.getLogger("uvicorn.error")


SettingKey = Literal["ask_save_templates", "offer_track_name_corrections"]


class PromptSettingUpdate(BaseModel):
    key: SettingKey
    enabled: bool


DEFAULT_PROMPT_SETTINGS = {
    "ask_save_templates": True,
    "offer_track_name_corrections": True,
}


@app.on_event("startup")
def initialize_prompt_settings() -> None:
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS application_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        db.executemany(
            "INSERT OR IGNORE INTO application_settings(key,value) VALUES(?,?)",
            [(key, "1" if enabled else "0") for key, enabled in DEFAULT_PROMPT_SETTINGS.items()],
        )


def prompt_settings() -> dict[str, bool]:
    result = dict(DEFAULT_PROMPT_SETTINGS)
    with connection() as db:
        rows = db.execute(
            "SELECT key,value FROM application_settings WHERE key IN (?,?)",
            tuple(DEFAULT_PROMPT_SETTINGS),
        ).fetchall()
    for row in rows:
        result[row["key"]] = row["value"] == "1"
    return result


@app.get("/api/v44/settings/prompts")
def get_prompt_settings() -> dict[str, bool]:
    return prompt_settings()


@app.put("/api/v44/settings/prompts")
def update_prompt_setting(request: PromptSettingUpdate) -> dict[str, bool]:
    with connection() as db:
        db.execute(
            "INSERT OR REPLACE INTO application_settings(key,value) VALUES(?,?)",
            (request.key, "1" if request.enabled else "0"),
        )
    logger.info("change=prompt_setting key=%s enabled=%s", request.key, str(request.enabled).lower())
    return prompt_settings()


class SubtitleColorSetting(BaseModel):
    color: str


@app.get('/api/settings/subtitle-color')
def get_subtitle_color() -> dict:
    with connection() as db:
        row = db.execute("SELECT value FROM application_settings WHERE key='subtitle_color'").fetchone()
    return {'color': row['value'] if row else '#FFFF00'}


@app.put('/api/settings/subtitle-color')
def save_subtitle_color(request: SubtitleColorSetting) -> dict:
    from fastapi import HTTPException
    from app.subtitle_color import COLOR
    if not COLOR.fullmatch(request.color):
        raise HTTPException(422, 'Use a six-digit hexadecimal color: #RRGGBB')
    with connection() as db:
        db.execute("INSERT INTO application_settings(key,value) VALUES('subtitle_color',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (request.color.upper(),))
    return {'color': request.color.upper()}
