from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.v2 import probe
from app.v5 import external_subtitles, split_tag
from app.v11 import STATIC_DIR, app, asset, plex_authorized_file


def matroska_tracks(path: Path) -> dict[str, list[dict]]:
    found = {"audio": [], "subtitle": []}
    if path.suffix.lower() not in {".mkv", ".mka", ".mks", ".mk3d"}:
        return found
    try:
        result = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True, timeout=90, check=True)
        for track in json.loads(result.stdout).get("tracks", []):
            track_type = "subtitle" if track.get("type") == "subtitles" else track.get("type")
            if track_type in found:
                found[track_type].append(track.get("properties") or {})
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return found


@app.middleware("http")
async def v13_assets(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text().replace('href="/app.css"', 'href="/assets/v13.css"').replace('src="/app.js"', 'src="/assets/v13.js"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v13.css":
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css")
        css = "\n".join((STATIC_DIR / name).read_text() for name in names).replace("@import url('/base.css');", "").replace("@import url('/previous.css');", "")
        return asset(css, "text/css")
    if request.method == "GET" and request.url.path == "/assets/v13.js":
        javascript = (STATIC_DIR / "v5.js").read_text().replace("'/api/movies'", "'/api/v11/movies'").replace("'/api/tv'", "'/api/v11/tv'")
        for name in ("v8-addon.js", "v9-session.js", "v10-progress.js", "v11-plex.js", "v12-context.js"):
            javascript += "\n" + (STATIC_DIR / name).read_text()
        javascript = javascript.replace("/api/media/details", "/api/v13/media/details")
        return asset(javascript, "text/javascript")
    return await call_next(request)


@app.get("/api/v13/media/details")
def media_details_with_ietf(path: str) -> dict:
    media = plex_authorized_file(path)
    mkv = matroska_tracks(media)
    counters = {"audio": 0, "subtitle": 0}
    streams = []
    for stream in probe(media).get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type not in counters:
            continue
        type_index = counters[codec_type]
        counters[codec_type] += 1
        tags = stream.get("tags") or {}
        language, legacy_region = split_tag(tags.get("language") or "")
        properties = mkv[codec_type][type_index] if type_index < len(mkv[codec_type]) else {}
        ietf_language, ietf_region = split_tag(properties.get("language_ietf") or "")
        if ietf_language:
            language = ietf_language
        streams.append({
            "codec_type": codec_type, "type_index": type_index,
            "codec": stream.get("codec_name") or "unknown",
            "language": language, "region": ietf_region or legacy_region,
            "title": tags.get("title") or properties.get("track_name") or "",
            "default": bool((stream.get("disposition") or {}).get("default")),
            "forced": bool((stream.get("disposition") or {}).get("forced")),
            "external": False,
        })
    return {"path": str(media), "streams": streams, "external_subtitles": external_subtitles(media)}
