"""Brand resources; asset bundles are owned by web_assets."""
import json
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from app.v13 import STATIC_DIR, app

BRAND_DIR = STATIC_DIR / "brand"
BRAND_FILES = {"favicon.ico", "favicon-16.png", "favicon-32.png", "apple-touch-icon.png", "header-icon.png", "icon-192.png", "icon-512.png", "videostreamedit-master.png"}


@app.get("/brand/{filename}", include_in_schema=False)
def brand_resource(filename: str):
    if filename not in BRAND_FILES:
        raise HTTPException(404, "Brand resource not found")
    return FileResponse(BRAND_DIR / filename, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest():
    return Response(json.dumps({"name":"VideoStreamEdit","short_name":"VSE","description":"Edit video container audio and subtitle stream metadata","start_url":"/","display":"standalone","background_color":"#101419","theme_color":"#14191f","icons":[{"src":"/brand/icon-192.png","sizes":"192x192","type":"image/png"},{"src":"/brand/icon-512.png","sizes":"512x512","type":"image/png"}]}), media_type="application/manifest+json", headers={"Cache-Control":"public, max-age=86400"})
