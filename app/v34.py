from __future__ import annotations

import logging

from pydantic import BaseModel

from app.v2 import connection
from app.v8 import ValueField
from app.v32 import app


logger = logging.getLogger("videostreamedit")


class SavedValueEditRequest(BaseModel):
    field: ValueField
    value: str
    new_value: str


class SavedValueRemoveRequest(BaseModel):
    field: ValueField
    value: str


@app.put("/api/v34/saved-values")
def edit_saved_value(request: SavedValueEditRequest) -> dict:
    old_value = request.value.strip()
    new_value = request.new_value.strip()
    if not old_value or not new_value:
        return {"updated": False}
    with connection() as db:
        row = db.execute(
            "SELECT use_count FROM reusable_stream_values WHERE field=? AND value=? AND saved=1",
            (request.field, old_value),
        ).fetchone()
        if not row:
            return {"updated": False}
        db.execute(
            """INSERT INTO reusable_stream_values(field,value,use_count,saved,prompted)
               VALUES(?,?,?,1,1)
               ON CONFLICT(field,value) DO UPDATE SET
                 use_count=MAX(use_count,excluded.use_count),saved=1,prompted=1""",
            (request.field, new_value, row["use_count"]),
        )
        if new_value != old_value:
            db.execute(
                "DELETE FROM reusable_stream_values WHERE field=? AND value=?",
                (request.field, old_value),
            )
    logger.info("change=saved_stream_value_renamed field=%s from=%s to=%s", request.field, old_value.replace("\n", "\\n"), new_value.replace("\n", "\\n"))
    return {"updated": True, "value": new_value}


@app.delete("/api/v34/saved-values")
def remove_saved_value(request: SavedValueRemoveRequest) -> dict:
    value = request.value.strip()
    with connection() as db:
        cursor = db.execute(
            "DELETE FROM reusable_stream_values WHERE field=? AND value=? AND saved=1",
            (request.field, value),
        )
    logger.info("change=saved_stream_value_removed field=%s value=%s", request.field, value.replace("\n", "\\n"))
    return {"removed": cursor.rowcount > 0}
