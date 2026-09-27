from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.v13 import media_details_with_ietf
from app.v14 import STATIC_DIR, app


class BulkInspectRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=250)


def inspect_episode(path: str) -> dict:
    details = media_details_with_ietf(path)
    audio = [stream for stream in details["streams"] if stream["codec_type"] == "audio"]
    subtitles = [stream for stream in details["streams"] if stream["codec_type"] == "subtitle"]
    eligible = len(audio) == 1 and not subtitles and not details["external_subtitles"]
    return {"path": path, "eligible": eligible, "audio_count": len(audio), "subtitle_count": len(subtitles) + len(details["external_subtitles"]), "audio": audio[0] if eligible else None}


@app.post("/api/v16/tv/bulk-audio/inspect")
def inspect_bulk_audio(request: BulkInspectRequest) -> dict:
    with ThreadPoolExecutor(max_workers=min(4, len(request.paths))) as executor:
        items = list(executor.map(inspect_episode, request.paths))
    invalid = [item for item in items if not item["eligible"]]
    if invalid:
        return {"eligible": False, "count": len(items), "invalid": [{"path": item["path"], "audio_count": item["audio_count"], "subtitle_count": item["subtitle_count"]} for item in invalid]}
    fields = ("language", "region", "title", "default", "forced")
    common = {}
    for field in fields:
        values = [item["audio"][field] for item in items]
        if all(value == values[0] for value in values):
            common[field] = values[0]
    return {"eligible": True, "count": len(items), "common": common, "items": [{"path": item["path"], "audio": item["audio"]} for item in items]}
