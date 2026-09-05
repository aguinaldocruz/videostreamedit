from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from app.v11 import connection
from app.v43 import app


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
