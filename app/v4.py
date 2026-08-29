from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse

from app.v3 import app

STATIC_DIR = Path(__file__).parent / "static"


@app.middleware("http")
async def v4_assets(request: Request, call_next):
    if request.method == "GET":
        assets = {
            "/": ("v4.html", "text/html"),
            "/app.css": ("v4.css", "text/css"),
            "/app.js": ("v4.js", "text/javascript"),
            "/base.css": ("v3.css", "text/css"),
        }
        if request.url.path in assets:
            filename, media_type = assets[request.url.path]
            return FileResponse(STATIC_DIR / filename, media_type=media_type)
    return await call_next(request)
