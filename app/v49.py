from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fastapi import HTTPException, Query
from fastapi.responses import Response

from app.v2 import probe
from app.v5 import checked_external
from app.v28 import authorized_import_file
from app.v48 import app


logger = logging.getLogger("uvicorn.error")


def checked_stream(media: Path, stream_type: str, type_index: int) -> None:
    count = sum(1 for stream in probe(media).get("streams", []) if stream.get("codec_type") == stream_type)
    if type_index < 0 or type_index >= count:
        raise HTTPException(404, f"{stream_type.title()} stream was not found")


def run_preview(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=True)
    except FileNotFoundError as exc:
        raise HTTPException(503, "ffmpeg is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Stream preview took too long") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()[-1200:]
        raise HTTPException(422, detail or "Could not create the stream preview") from exc


@app.get("/api/v49/stream-preview/audio")
def audio_stream_preview(path: str, type_index: int = Query(ge=0)) -> Response:
    media = authorized_import_file(path)
    checked_stream(media, "audio", type_index)
    details = probe(media)
    try:
        duration = float((details.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    start = min(60.0, max(0.0, duration * 0.15)) if duration else 30.0
    result = run_preview([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(media), "-map", f"0:a:{type_index}",
        "-vn", "-t", "25", "-ac", "2", "-ar", "44100", "-b:a", "128k",
        "-f", "mp3", "pipe:1",
    ])
    if not result.stdout:
        raise HTTPException(422, "The selected audio stream produced no preview")
    logger.info("operation=audio_preview file=%s stream=audio:%d", str(media).replace("\n", "\\n"), type_index)
    return Response(result.stdout, media_type="audio/mpeg", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/v49/stream-preview/subtitle")
def subtitle_stream_preview(path: str, type_index: int = Query(default=0, ge=0), external_path: str | None = None) -> dict:
    media = authorized_import_file(path)
    if external_path:
        subtitle = checked_external(media, external_path)
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(subtitle), "-map", "0:0", "-f", "srt", "pipe:1"]
        description = subtitle.name
    else:
        checked_stream(media, "subtitle", type_index)
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media), "-map", f"0:s:{type_index}", "-f", "srt", "pipe:1"]
        description = f"Subtitle {type_index + 1}"
    try:
        result = run_preview(command, timeout=45)
    except HTTPException as exc:
        if exc.status_code == 422:
            raise HTTPException(422, "This subtitle is graphical or cannot be converted to a text preview") from exc
        raise
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise HTTPException(422, "No readable subtitle text was found")
    clipped = len(text) > 16000
    logger.info("operation=subtitle_preview file=%s stream=%s", str(media).replace("\n", "\\n"), description.replace("\n", "\\n"))
    return {"description": description, "text": text[:16000], "clipped": clipped}
