from __future__ import annotations

from fastapi import Query
from fastapi.responses import JSONResponse, Response

from app.v28 import authorized_import_file
from app.v50 import audio_preview as generate_audio_preview
from app.v50 import subtitle_preview as generate_subtitle_preview
from app.v54 import app, cache_folder


@app.get("/api/v55/stream-preview/audio")
def cached_audio_preview(path: str, type_index: int = Query(ge=0), segment: int = Query(default=0, ge=0)) -> Response:
    media = authorized_import_file(path)
    cached = cache_folder(str(media)) / f"audio-{type_index}-{segment}.mp3"
    if cached.is_file():
        duration_file = cached.parent / "duration.txt"
        duration = duration_file.read_text(encoding="ascii") if duration_file.is_file() else "0"
        return Response(cached.read_bytes(), media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600", "X-Preview-Cache": "hit", "X-Media-Duration": duration})
    response = generate_audio_preview(str(media), type_index, segment)
    # On-demand generation also warms this one segment without claiming the full media cache is complete.
    folder = cache_folder(str(media)); folder.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(bytes(response.body))
    response.headers["X-Preview-Cache"] = "miss"
    return response


@app.get("/api/v55/stream-preview/subtitle")
def cached_subtitle_preview(path: str, type_index: int = Query(default=0, ge=0), external_path: str | None = None, page: int = Query(default=0, ge=0)) -> dict:
    media = authorized_import_file(path)
    # Subtitle previews intentionally bypass the preview cache. Reading the
    # current media on every request prevents stale text after edits/remuxes;
    # audio previews remain cached independently by cached_audio_preview().
    result = generate_subtitle_preview(str(media), type_index, external_path, page)
    return JSONResponse(result, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})
