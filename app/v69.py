from __future__ import annotations

import math
import os
import subprocess
import threading
import time

from fastapi import Query
from fastapi.responses import FileResponse, Response, StreamingResponse

import app.v54 as preview_jobs
import app.v63 as preview_cache
from app.v2 import probe
from app.v28 import authorized_import_file
from app.v63 import (
    PREVIEW_ENCODER_VERSION,
    PREVIEW_SAMPLE_SECONDS,
    PREVIEW_START_SECONDS,
    audio_command,
    cache_entry_valid,
    enforce_lru,
    preview_media_signature,
    register_file,
)
from app.v68 import app


@app.get("/api/v69/stream-preview/audio")
def five_minute_audio_preview(
    path: str,
    type_index: int = Query(ge=0),
    segment: int = Query(default=PREVIEW_START_SECONDS // PREVIEW_SAMPLE_SECONDS, ge=0),
) -> Response:
    media = authorized_import_file(path)
    folder = preview_jobs.cache_folder(str(media))
    duration_file = folder / "duration.txt"
    try:
        duration = float(duration_file.read_text(encoding="ascii")) if duration_file.is_file() else float((probe(media).get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    media_signature = preview_media_signature(media)
    effective = segment
    if duration and segment * PREVIEW_SAMPLE_SECONDS >= duration:
        effective = max(0, math.ceil(duration / PREVIEW_SAMPLE_SECONDS) - 1)
    cached = folder / f"audio-{type_index}-{effective}.mp3"
    headers = {"Cache-Control": "private,max-age=3600", "X-Preview-Segment": str(effective), "X-Media-Duration": str(duration), "X-Preview-Encoder": PREVIEW_ENCODER_VERSION}
    if cached.is_file() and cache_entry_valid(str(media), cached.name, media_signature):
        with preview_cache.connection() as db:
            db.execute("UPDATE preview_cache_files SET last_access=? WHERE path=? AND filename=?", (time.time(), str(media), cached.name))
        preview_cache.record_preview_metric("hits")
        headers["X-Preview-Cache"] = "hit"
        return FileResponse(cached, media_type="audio/mpeg", headers=headers)

    folder.mkdir(parents=True, exist_ok=True)
    temporary = cached.with_name(f".{cached.name}.{threading.get_ident()}.tmp")
    command = audio_command(media, type_index, effective)

    def stream_audio():
        with preview_cache.preview_claim(str(media), type_index, effective):
            # Another request may have completed this segment while waiting.
            if cached.is_file() and cache_entry_valid(str(media), cached.name, media_signature):
                headers["X-Preview-Cache"] = "hit"
                yield cached.read_bytes()
                return
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            started = time.monotonic()
            try:
                with temporary.open("wb") as output:
                    while chunk := process.stdout.read(64 * 1024):
                        output.write(chunk)
                        yield chunk
                if process.wait(timeout=10) == 0 and temporary.stat().st_size > 0:
                    os.replace(temporary, cached)
                    register_file(str(media), cached, False, media_signature, duration)
                    elapsed = time.monotonic() - started
                    preview_cache.record_preview_metric("extractions")
                    preview_cache.record_preview_metric("extraction_seconds", elapsed)
                    preview_cache.record_preview_metric("last_extraction_seconds", elapsed)
                    enforce_lru()
                    preview_cache.logger.info("operation=audio_preview_streamed file=%s stream=audio:%d segment=%d", str(media).replace("\n", "\\n"), type_index, effective)
            except Exception:
                preview_cache.record_preview_metric("failures")
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                temporary.unlink(missing_ok=True)


    preview_cache.record_preview_metric("misses")
    headers["X-Preview-Cache"] = "miss"
    return StreamingResponse(stream_audio(), media_type="audio/mpeg", headers=headers)
