from __future__ import annotations
from fastapi import Query
from fastapi.responses import Response
from app.v50 import audio_preview
from app.v68 import app
@app.get("/api/v69/stream-preview/audio")
def direct_audio_preview(path: str, type_index: int = Query(ge=0), segment: int = Query(default=12, ge=0)) -> Response:
    response = audio_preview(path, type_index, segment)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["X-Preview-Cache"] = "removed"
    return response
