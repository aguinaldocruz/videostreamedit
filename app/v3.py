from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.v2 import app

STATIC_DIR = Path(__file__).parent / "static"


@app.middleware("http")
async def single_file_and_v3_assets(request: Request, call_next):
    if request.method == "GET":
        assets = {
            "/": ("v3.html", "text/html"),
            "/app.css": ("v3.css", "text/css"),
            "/app.js": ("v3.js", "text/javascript"),
        }
        if request.url.path in assets:
            filename, media_type = assets[request.url.path]
            return FileResponse(STATIC_DIR / filename, media_type=media_type)
    if request.method == "POST" and request.url.path in {"/api/media/batch-probe", "/api/media/edit"}:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON request"}, status_code=400)
        if len(payload.get("paths") or []) != 1:
            return JSONResponse({"detail": "Exactly one media file must be selected"}, status_code=422)
    return await call_next(request)
