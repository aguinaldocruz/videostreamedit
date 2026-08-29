from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.v13 import media_details_with_ietf
from app.v15 import STATIC_DIR, app, asset


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


@app.middleware("http")
async def v16_bulk_assets(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text()
        html = html.replace('<title>VideoStreamEdit</title><link rel="stylesheet" href="/app.css">', '<title>VideoStreamEdit · Stream Metadata Editor</title><meta name="theme-color" content="#14191f"><link rel="icon" href="/brand/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="32x32" href="/brand/favicon-32.png"><link rel="icon" type="image/png" sizes="16x16" href="/brand/favicon-16.png"><link rel="apple-touch-icon" sizes="180x180" href="/brand/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><link rel="stylesheet" href="/assets/v16.css">')
        html = html.replace('<header><h1>VideoStreamEdit</h1><nav>', '<header><a class="brand" href="/" aria-label="VideoStreamEdit home"><img src="/brand/header-icon.png" width="48" height="48" alt=""><span><strong>VideoStreamEdit</strong><small>Stream metadata editor</small></span></a><nav>')
        html = html.replace('<script src="/app.js"></script>', '<script src="/assets/v16.js"></script>')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v16.css":
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css", "v14-brand.css", "v15-path.css", "v16-bulk.css", "v18-value-popup.css")
        css = "\n".join((STATIC_DIR / name).read_text() for name in names).replace("@import url('/base.css');", "").replace("@import url('/previous.css');", "")
        return asset(css, "text/css")
    if request.method == "GET" and request.url.path == "/assets/v16.js":
        javascript = (STATIC_DIR / "v5.js").read_text().replace("'/api/movies'", "'/api/v11/movies'").replace("'/api/tv'", "'/api/v11/tv'")
        for name in ("v8-addon.js", "v9-session.js", "v10-progress.js", "v11-plex.js", "v12-context.js", "v15-path.js", "v16-bulk.js", "v18-value-popup.js"):
            javascript += "\n" + (STATIC_DIR / name).read_text()
        javascript = javascript.replace("/api/media/details", "/api/v13/media/details")
        return asset(javascript, "text/javascript")
    return await call_next(request)
