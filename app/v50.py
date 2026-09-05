from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from fastapi import HTTPException, Query
from fastapi.responses import Response

from app.v2 import probe
from app.v5 import checked_external
from app.v28 import authorized_import_file
from app.v49 import app


logger = logging.getLogger("uvicorn.error")


def media_and_count(path: str, stream_type: str) -> tuple[Path, dict, int]:
    media = authorized_import_file(path)
    details = probe(media)
    count = sum(1 for stream in details.get("streams", []) if stream.get("codec_type") == stream_type)
    return media, details, count


def validate_index(index: int, count: int, stream_type: str) -> None:
    if index < 0 or index >= count:
        raise HTTPException(404, f"{stream_type.title()} stream was not found")


def run(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=True)
    except FileNotFoundError as exc:
        raise HTTPException(503, f"{command[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Stream preview took too long") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()[-1200:]
        raise HTTPException(422, detail or "Could not create the stream preview") from exc


@app.get("/api/v50/stream-preview/audio")
def audio_preview(path: str, type_index: int = Query(ge=0), segment: int = Query(default=0, ge=0)) -> Response:
    media, details, count = media_and_count(path, "audio")
    validate_index(type_index, count, "audio")
    try:
        duration = float((details.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    start = segment * 25.0
    if duration and start >= duration:
        raise HTTPException(416, "There are no more audio segments")
    result = run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(media), "-map", f"0:a:{type_index}", "-vn", "-t", "25", "-ac", "2", "-ar", "44100", "-b:a", "128k", "-f", "mp3", "pipe:1"])
    if not result.stdout:
        raise HTTPException(422, "The selected audio stream produced no preview")
    logger.info("operation=audio_preview file=%s stream=audio:%d segment=%d", str(media).replace("\n", "\\n"), type_index, segment)
    return Response(result.stdout, media_type="audio/mpeg", headers={"Cache-Control": "no-store, max-age=0", "X-Media-Duration": str(duration)})


@app.get("/api/v50/stream-preview/subtitle")
def subtitle_preview(path: str, type_index: int = Query(default=0, ge=0), external_path: str | None = None, page: int = Query(default=0, ge=0)) -> dict:
    media, _, count = media_and_count(path, "subtitle")
    if external_path:
        subtitle = checked_external(media, external_path)
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(subtitle), "-map", "0:0", "-f", "srt", "pipe:1"]
        description = subtitle.name
    else:
        validate_index(type_index, count, "subtitle")
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media), "-map", f"0:s:{type_index}", "-f", "srt", "pipe:1"]
        description = f"Subtitle {type_index + 1}"
    try:
        result = run(command, timeout=45)
    except HTTPException as exc:
        if exc.status_code == 422 and not external_path:
            return {"description": description, "kind": "graphical", "page": page, "has_previous": page > 0, "has_next": True}
        raise
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise HTTPException(422, "No readable subtitle text was found")
    page_size = 12000
    start = page * page_size
    if start >= len(text) and page:
        raise HTTPException(416, "There are no more subtitle pages")
    logger.info("operation=subtitle_preview file=%s stream=%s page=%d", str(media).replace("\n", "\\n"), description.replace("\n", "\\n"), page)
    return {"description": description, "kind": "text", "text": text[start:start + page_size], "page": page, "has_previous": page > 0, "has_next": start + page_size < len(text)}


@app.get("/api/v50/stream-preview/subtitle-image")
def subtitle_image(path: str, type_index: int = Query(ge=0), page: int = Query(default=0, ge=0)) -> Response:
    media, _, count = media_and_count(path, "subtitle")
    validate_index(type_index, count, "subtitle")
    packet_result = run(["ffprobe", "-v", "error", "-select_streams", f"s:{type_index}", "-show_entries", "packet=pts_time", "-of", "json", str(media)], timeout=45)
    try:
        timestamps = [float(item["pts_time"]) for item in json.loads(packet_result.stdout).get("packets", []) if item.get("pts_time") is not None]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(422, "Could not locate graphical subtitle events") from exc
    if page >= len(timestamps):
        raise HTTPException(416, "There are no more subtitle images")
    timestamp = timestamps[page]
    result = run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, timestamp):.3f}", "-i", str(media), "-filter_complex", f"[0:v:0][0:s:{type_index}]overlay", "-frames:v", "1", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"])
    if not result.stdout:
        raise HTTPException(422, "This graphical subtitle codec cannot be rendered by the installed FFmpeg")
    logger.info("operation=graphical_subtitle_preview file=%s stream=subtitle:%d event=%d", str(media).replace("\n", "\\n"), type_index, page)
    return Response(result.stdout, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0", "X-Subtitle-Pages": str(len(timestamps))})
