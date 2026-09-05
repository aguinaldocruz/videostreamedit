from __future__ import annotations

import math
import os
import subprocess
import threading
import time

from fastapi import Query
from fastapi.responses import FileResponse, Response, StreamingResponse

import app.v63 as preview_cache
import app.v54 as preview_jobs
from app.v63 import enforce_lru, register_file
from app.v2 import probe
from app.v28 import authorized_import_file
from app.v68 import app


@app.get("/api/v69/stream-preview/audio")
def five_minute_audio_preview(
    path: str,
    type_index: int = Query(ge=0),
    segment: int = Query(default=12, ge=0),
) -> Response:
    media = authorized_import_file(path)
    folder = preview_jobs.cache_folder(str(media))
    duration_file = folder / "duration.txt"
    try:
        duration = float(duration_file.read_text(encoding="ascii")) if duration_file.is_file() else float((probe(media).get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    effective = segment
    if duration and segment * 25 >= duration:
        effective = max(0, math.ceil(duration / 25) - 1)
    cached = folder / f"audio-{type_index}-{effective}.mp3"
    headers = {"Cache-Control": "private,max-age=3600", "X-Preview-Segment": str(effective), "X-Media-Duration": str(duration)}
    if cached.is_file():
        with preview_cache.connection() as db:
            db.execute("UPDATE preview_cache_files SET last_access=? WHERE path=? AND filename=?", (time.time(), str(media), cached.name))
        headers["X-Preview-Cache"] = "hit"
        return FileResponse(cached, media_type="audio/mpeg", headers=headers)

    folder.mkdir(parents=True, exist_ok=True)
    temporary = cached.with_name(f".{cached.name}.{threading.get_ident()}.tmp")
    command = [
        "nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", str(effective * 25), "-i", str(media), "-map", f"0:a:{type_index}",
        "-vn", "-t", "25", "-ac", "1", "-ar", "32000", "-b:a", "64k", "-f", "mp3", "pipe:1",
    ]

    def stream_audio():
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with temporary.open("wb") as output:
                while chunk := process.stdout.read(64 * 1024):
                    output.write(chunk)
                    yield chunk
            if process.wait(timeout=10) == 0 and temporary.stat().st_size > 0:
                os.replace(temporary, cached)
                register_file(str(media), cached, False)
                enforce_lru()
                preview_cache.logger.info("operation=audio_preview_streamed file=%s stream=audio:%d segment=%d", str(media).replace("\n", "\\n"), type_index, effective)
        finally:
            if process.poll() is None:
                process.terminate()
            temporary.unlink(missing_ok=True)

    headers["X-Preview-Cache"] = "miss"
    return StreamingResponse(stream_audio(), media_type="audio/mpeg", headers=headers)
