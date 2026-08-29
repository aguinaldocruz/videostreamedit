from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.v14 import STATIC_DIR, app, asset


@app.middleware("http")
async def v15_path_indicator(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text()
        html = html.replace('<title>VideoStreamEdit</title><link rel="stylesheet" href="/app.css">', '<title>VideoStreamEdit · Stream Metadata Editor</title><meta name="theme-color" content="#14191f"><link rel="icon" href="/brand/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="32x32" href="/brand/favicon-32.png"><link rel="icon" type="image/png" sizes="16x16" href="/brand/favicon-16.png"><link rel="apple-touch-icon" sizes="180x180" href="/brand/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><link rel="stylesheet" href="/assets/v15.css">')
        html = html.replace('<header><h1>VideoStreamEdit</h1><nav>', '<header><a class="brand" href="/" aria-label="VideoStreamEdit home"><img src="/brand/header-icon.png" width="48" height="48" alt=""><span><strong>VideoStreamEdit</strong><small>Stream metadata editor</small></span></a><nav>')
        html = html.replace('<script src="/app.js"></script>', '<script src="/assets/v15.js"></script>')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v15.css":
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css", "v14-brand.css", "v15-path.css")
        css = "\n".join((STATIC_DIR / name).read_text() for name in names).replace("@import url('/base.css');", "").replace("@import url('/previous.css');", "")
        return asset(css, "text/css")
    if request.method == "GET" and request.url.path == "/assets/v15.js":
        javascript = (STATIC_DIR / "v5.js").read_text().replace("'/api/movies'", "'/api/v11/movies'").replace("'/api/tv'", "'/api/v11/tv'")
        for name in ("v8-addon.js", "v9-session.js", "v10-progress.js", "v11-plex.js", "v12-context.js", "v15-path.js"):
            javascript += "\n" + (STATIC_DIR / name).read_text()
        javascript = javascript.replace("/api/media/details", "/api/v13/media/details")
        return asset(javascript, "text/javascript")
    return await call_next(request)
