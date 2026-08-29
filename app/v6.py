from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from app.v2 import RootCreate, add_root, remove_root, roots
from app.v5 import SingleEditRequest, app, edit_single

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("uvicorn.error")


def safe_log(value: object) -> str:
    return str(value).replace("\n", "\\n").replace("\r", "\\r")


def asset_response(content: str, media_type: str) -> Response:
    return Response(content, media_type=media_type, headers={"Cache-Control": "no-store, max-age=0"})


@app.middleware("http")
async def stable_v6_assets(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text()
        html = html.replace('href="/app.css"', 'href="/assets/v6.css"').replace('src="/app.js"', 'src="/assets/v6.js"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v6.css":
        css = "\n".join((STATIC_DIR / name).read_text() for name in ("v3.css", "v4.css", "v5.css"))
        css = css.replace("@import url('/base.css');", "").replace("@import url('/previous.css');", "")
        return asset_response(css, "text/css")
    if request.method == "GET" and request.url.path == "/assets/v6.js":
        javascript = (STATIC_DIR / "v5.js").read_text()
        javascript = javascript.replace("await api('/api/roots',{method:'POST'", "await api('/api/v6/roots',{method:'POST'").replace("`/api/roots/${b.dataset.rootId}`", "`/api/v6/roots/${b.dataset.rootId}`").replace("'/api/media/edit-single'", "'/api/v6/media/edit'")
        return asset_response(javascript, "text/javascript")
    return await call_next(request)


@app.post("/api/v6/roots", status_code=201)
def add_root_logged(item: RootCreate) -> dict:
    result = add_root(item)
    logger.info("change=library_root_added kind=%s name=%s path=%s", safe_log(result["kind"]), safe_log(result["name"]), safe_log(result["path"]))
    return result


@app.delete("/api/v6/roots/{root_id}")
def remove_root_logged(root_id: int) -> dict[str, bool]:
    existing = next((dict(row) for row in roots() if row["id"] == root_id), None)
    result = remove_root(root_id)
    if existing:
        logger.info("change=library_root_removed kind=%s name=%s path=%s", safe_log(existing["kind"]), safe_log(existing["name"]), safe_log(existing["path"]))
    return result


@app.post("/api/v6/media/edit")
def edit_single_logged(request: SingleEditRequest) -> dict:
    result = edit_single(request)
    media = safe_log(result["edited"])
    for track in request.tracks:
        fields = []
        if track.language is not None or track.region is not None:
            fields.append(f"language={safe_log(track.language or '')} region={safe_log(track.region or '')}")
        if track.title is not None:
            fields.append(f"title={safe_log(track.title)}")
        if fields:
            logger.info("change=track_metadata file=%s track=%s:%d %s", media, track.codec_type, track.type_index, " ".join(fields))
    logger.info("change=forced_track file=%s type=audio selected=%s", media, request.forced_audio if request.forced_audio is not None else "none")
    logger.info("change=forced_track file=%s type=subtitle selected=%s", media, request.forced_subtitle if request.forced_subtitle is not None else "none_or_external")
    for subtitle in request.external_subtitles:
        if subtitle.embed:
            logger.info("change=external_subtitle_embedded file=%s subtitle=%s language=%s region=%s title=%s forced=%s", media, safe_log(subtitle.path), safe_log(subtitle.language), safe_log(subtitle.region), safe_log(subtitle.title), str(subtitle.forced).lower())
    for warning in result["warnings"]:
        logger.warning("change=external_subtitle_cleanup_warning file=%s message=%s", media, safe_log(warning))
    logger.info("change=media_file_replaced file=%s", media)
    return result
