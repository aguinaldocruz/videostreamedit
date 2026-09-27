from __future__ import annotations

from fastapi import Query
from fastapi.responses import JSONResponse, Response

from app.v28 import authorized_import_file
from app.v50 import audio_preview as generate_audio_preview
from app.v50 import subtitle_preview as generate_subtitle_preview
from app.v54 import app


@app.get("/api/v55/stream-preview/audio")
def cached_audio_preview(path: str, type_index: int = Query(ge=0), segment: int = Query(default=0, ge=0)) -> Response:
    # Compatibility URL: direct extraction only, never persisted.
    response = generate_audio_preview(path, type_index, segment)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["X-Preview-Cache"] = "removed"
    return response


@app.get("/api/v55/stream-preview/subtitle")
def cached_subtitle_preview(path: str, type_index: int = Query(default=0, ge=0), external_path: str | None = None, page: int = Query(default=0, ge=0)) -> dict:
    media = authorized_import_file(path)
    # Subtitle previews intentionally bypass the preview cache. Reading the
    # current media on every request prevents stale text after edits/remuxes;
    # Audio preview compatibility requests are generated directly and discarded.
    result = generate_subtitle_preview(str(media), type_index, external_path, page)
    return JSONResponse(result, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})
