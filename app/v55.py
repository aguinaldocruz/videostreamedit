from __future__ import annotations

import json
import os
from fastapi import Query
from fastapi.responses import Response

from app.v28 import authorized_import_file
from app.v50 import audio_preview as generate_audio_preview
from app.v50 import subtitle_preview as generate_subtitle_preview
from app.v54 import app, cache_folder
from app.v11 import connection


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
    if not external_path:
        page_cache = cache_folder(str(media)) / f"subtitle-{type_index}-page-{page}.json"
        if page_cache.is_file():
            with connection() as db:
                db.execute("UPDATE preview_cache_files SET last_access=strftime('%s','now') WHERE path=? AND filename=?", (str(media), page_cache.name))
            result = json.loads(page_cache.read_text(encoding="utf-8"))
            result["cache"] = "hit"
            return result
        cached = cache_folder(str(media)) / f"subtitle-{type_index}.srt"
        if cached.is_file():
            text = cached.read_text(encoding="utf-8", errors="replace").strip()
            page_size = 12000; start = page * page_size
            if start < len(text):
                return {"description": f"Subtitle {type_index + 1}", "kind": "text", "text": text[start:start + page_size], "page": page, "has_previous": page > 0, "has_next": True, "cache": "hit"}
    result = generate_subtitle_preview(str(media), type_index, external_path, page)
    if not external_path and result.get("kind") == "text":
        folder = cache_folder(str(media)); folder.mkdir(parents=True, exist_ok=True)
        page_cache = folder / f"subtitle-{type_index}-page-{page}.json"
        temporary = page_cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, page_cache)
        import app.v63 as preview_cache
        preview_cache.register_file(str(media), page_cache, False)
        preview_cache.enforce_lru()
    result["cache"] = "miss"
    return result
