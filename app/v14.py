from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.v13 import STATIC_DIR, app, asset

BRAND_DIR = STATIC_DIR / "brand"
BRAND_FILES = {"favicon.ico", "favicon-16.png", "favicon-32.png", "apple-touch-icon.png", "header-icon.png", "icon-192.png", "icon-512.png", "videostreamedit-master.png"}


@app.middleware("http")
async def v14_branding(request: Request, call_next):
    path = request.url.path
    if request.method == "GET" and path == "/":
        html = (STATIC_DIR / "v5.html").read_text()
        html = html.replace('<title>VideoStreamEdit</title><link rel="stylesheet" href="/app.css">', '<title>VideoStreamEdit · Stream Metadata Editor</title><meta name="theme-color" content="#14191f"><link rel="icon" href="/brand/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="32x32" href="/brand/favicon-32.png"><link rel="icon" type="image/png" sizes="16x16" href="/brand/favicon-16.png"><link rel="apple-touch-icon" sizes="180x180" href="/brand/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><link rel="stylesheet" href="/assets/v14.css">')
        html = html.replace('<header><h1>VideoStreamEdit</h1><nav>', '<header><a class="brand" href="/" aria-label="VideoStreamEdit home"><img src="/brand/header-icon.png" width="48" height="48" alt=""><span><strong>VideoStreamEdit</strong><small>Stream metadata editor</small></span></a><nav>')
        html = html.replace('<script src="/app.js"></script>', '<script src="/assets/v13.js"></script>')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and path == "/assets/v14.css":
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css", "v14-brand.css")
        css = "\n".join((STATIC_DIR / name).read_text() for name in names).replace("@import url('/base.css');", "").replace("@import url('/previous.css');", "")
        return asset(css, "text/css")
    if request.method == "GET" and path.startswith("/brand/") and path.removeprefix("/brand/") in BRAND_FILES:
        return FileResponse(BRAND_DIR / path.removeprefix("/brand/"), headers={"Cache-Control": "public, max-age=86400"})
    if request.method == "GET" and path == "/manifest.webmanifest":
        manifest = {"name":"VideoStreamEdit","short_name":"VSE","description":"Edit video container audio and subtitle stream metadata","start_url":"/","display":"standalone","background_color":"#101419","theme_color":"#14191f","icons":[{"src":"/brand/icon-192.png","sizes":"192x192","type":"image/png"},{"src":"/brand/icon-512.png","sizes":"512x512","type":"image/png"}]}
        return Response(json.dumps(manifest), media_type="application/manifest+json", headers={"Cache-Control":"public, max-age=86400"})
    return await call_next(request)
