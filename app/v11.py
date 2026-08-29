from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import app.v5 as v5_module
import app.v7 as v7_module
from app.v2 import connection, resolve_existing
from app.v8 import STATIC_DIR, app, asset
from app.plex_secret import decrypt_token, encrypt_token

logger = logging.getLogger("uvicorn.error")


class PlexConfig(BaseModel):
    url: str
    token: str = ""


class LibrarySelection(BaseModel):
    keys: list[str]


@app.on_event("startup")
def initialize_plex() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS plex_config (
                id INTEGER PRIMARY KEY CHECK(id = 1), url TEXT NOT NULL, token TEXT NOT NULL,
                server_name TEXT NOT NULL DEFAULT '', last_sync TEXT
            );
            CREATE TABLE IF NOT EXISTS plex_libraries (
                library_key TEXT PRIMARY KEY, title TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('movie','show')), selected INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS plex_media (
                path TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('movie','episode')),
                rating_key TEXT NOT NULL, library_key TEXT NOT NULL, library_name TEXT NOT NULL,
                title TEXT NOT NULL, show_title TEXT, season_number INTEGER, episode_number INTEGER,
                size INTEGER NOT NULL DEFAULT 0, modified INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS plex_media_kind ON plex_media(kind);
            CREATE INDEX IF NOT EXISTS plex_media_show ON plex_media(show_title, season_number, episode_number);
        """)


def config_row():
    with connection() as db:
        return db.execute("SELECT * FROM plex_config WHERE id = 1").fetchone()


def plex_request(path: str, *, url: str | None = None, token: str | None = None, start: int | None = None, size: int = 500) -> dict:
    saved = config_row()
    base = (url or (saved["url"] if saved else "")).strip().rstrip("/")
    secret = token if token is not None and token != "" else (decrypt_token(saved["token"]) if saved else "")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(400, "Plex URL must begin with http:// or https://")
    headers = {"Accept": "application/json", "X-Plex-Token": secret, "X-Plex-Client-Identifier": "videostreamedit", "X-Plex-Product": "VideoStreamEdit", "X-Plex-Version": "0.11"}
    if start is not None:
        headers.update({"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(size)})
    request = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Could not communicate with Plex: {getattr(exc, 'reason', str(exc))}") from exc


def section_list(url: str | None = None, token: str | None = None) -> tuple[str, list[dict]]:
    root = plex_request("/", url=url, token=token).get("MediaContainer", {})
    data = plex_request("/library/sections", url=url, token=token).get("MediaContainer", {})
    libraries = [{"key": str(item["key"]), "title": item.get("title") or f'Library {item["key"]}', "kind": item["type"]} for item in data.get("Directory", []) if item.get("type") in {"movie", "show"}]
    return root.get("friendlyName") or "Plex Media Server", libraries


@app.middleware("http")
async def v11_assets(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text().replace('href="/app.css"', 'href="/assets/v11.css"').replace('src="/app.js"', 'src="/assets/v11.js"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v11.css":
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css")
        css = "\n".join((STATIC_DIR / name).read_text() for name in names).replace("@import url('/base.css');", "").replace("@import url('/previous.css');", "")
        return asset(css, "text/css")
    if request.method == "GET" and request.url.path == "/assets/v11.js":
        javascript = (STATIC_DIR / "v5.js").read_text().replace("'/api/movies'", "'/api/v11/movies'").replace("'/api/tv'", "'/api/v11/tv'")
        for name in ("v8-addon.js", "v9-session.js", "v10-progress.js", "v11-plex.js", "v12-context.js"):
            javascript += "\n" + (STATIC_DIR / name).read_text()
        return asset(javascript, "text/javascript")
    return await call_next(request)


@app.get("/api/v11/plex/config")
def get_plex_config() -> dict:
    row = config_row()
    with connection() as db:
        libraries = [dict(item) for item in db.execute("SELECT library_key AS key, title, kind, selected FROM plex_libraries ORDER BY kind, title COLLATE NOCASE")]
        count = db.execute("SELECT COUNT(*) FROM plex_media").fetchone()[0]
    return {"url": row["url"] if row else "", "has_token": bool(row and row["token"]), "server_name": row["server_name"] if row else "", "last_sync": row["last_sync"] if row else None, "libraries": libraries, "media_count": count}


@app.post("/api/v11/plex/config")
def save_plex_config(item: PlexConfig) -> dict:
    current = config_row(); token = item.token.strip() or (decrypt_token(current["token"]) if current else "")
    name, libraries = section_list(item.url, token)
    with connection() as db:
        db.execute("INSERT INTO plex_config(id,url,token,server_name) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url,token=excluded.token,server_name=excluded.server_name", (item.url.strip().rstrip("/"), encrypt_token(token), name))
        old = {row["library_key"]: row["selected"] for row in db.execute("SELECT library_key,selected FROM plex_libraries")}
        db.execute("DELETE FROM plex_libraries")
        db.executemany("INSERT INTO plex_libraries(library_key,title,kind,selected) VALUES(?,?,?,?)", [(x["key"], x["title"], x["kind"], old.get(x["key"], 0)) for x in libraries])
    logger.info("change=plex_configuration server=%s url=%s libraries=%d", name.replace("\n", "\\n"), item.url.replace("\n", "\\n"), len(libraries))
    return get_plex_config()


@app.put("/api/v11/plex/libraries")
def select_libraries(item: LibrarySelection) -> dict:
    with connection() as db:
        valid = {row[0] for row in db.execute("SELECT library_key FROM plex_libraries")}
        chosen = set(item.keys)
        if not chosen <= valid: raise HTTPException(400, "Unknown Plex library selected")
        db.execute("UPDATE plex_libraries SET selected = 0")
        db.executemany("UPDATE plex_libraries SET selected = 1 WHERE library_key = ?", [(key,) for key in chosen])
    logger.info("change=plex_libraries_selected keys=%s", ",".join(sorted(chosen)))
    return get_plex_config()


def paged_metadata(key: str, kind: str, metadata_type: int | None = None) -> list[dict]:
    start=0; found=[]
    while True:
        requested_type = metadata_type if metadata_type is not None else (4 if kind == "show" else None)
        query = f"/library/sections/{urllib.parse.quote(key)}/all" + (f"?type={requested_type}" if requested_type else "")
        container = plex_request(query, start=start).get("MediaContainer", {}); page = container.get("Metadata", []); found.extend(page)
        total = int(container.get("totalSize", container.get("size", len(found))))
        if not page or len(found) >= total: return found
        start += len(page)


@app.post("/api/v11/plex/sync")
def sync_plex() -> dict:
    with connection() as db:
        selected = [dict(row) for row in db.execute("SELECT library_key,title,kind FROM plex_libraries WHERE selected=1")]
    if not selected: raise HTTPException(400, "Select at least one Plex library")
    records=[]
    for library in selected:
        show_titles = {str(show.get("ratingKey")): show.get("originalTitle") or show.get("title") for show in paged_metadata(library["library_key"], library["kind"], 2)} if library["kind"] == "show" else {}
        for item in paged_metadata(library["library_key"], library["kind"]):
            media_kind = "movie" if library["kind"] == "movie" else "episode"
            for media in item.get("Media", []):
                for part in media.get("Part", []):
                    path=part.get("file");
                    if not path: continue
                    records.append((path,media_kind,str(item.get("ratingKey", "")),library["library_key"],library["title"],item.get("title") or Path(path).stem,show_titles.get(str(item.get("grandparentRatingKey"))) or item.get("grandparentTitle"),item.get("parentIndex"),item.get("index"),int(part.get("size") or 0),int(item.get("updatedAt") or 0)))
    with connection() as db:
        db.execute("DELETE FROM plex_media")
        db.executemany("INSERT OR REPLACE INTO plex_media(path,kind,rating_key,library_key,library_name,title,show_title,season_number,episode_number,size,modified) VALUES(?,?,?,?,?,?,?,?,?,?,?)",records)
        db.execute("UPDATE plex_config SET last_sync = datetime('now') WHERE id=1")
    logger.info("change=plex_catalog_synced libraries=%d media=%d", len(selected), len(records))
    return {"libraries":len(selected),"media":len(records),"configuration":get_plex_config()}


@app.get("/api/v11/movies")
def plex_movies() -> list[dict]:
    with connection() as db: rows=db.execute("SELECT * FROM plex_media WHERE kind='movie' ORDER BY title COLLATE NOCASE,path").fetchall()
    return [{"path":r["path"],"name":r["title"]+Path(r["path"]).suffix,"relative_path":r["title"]+Path(r["path"]).suffix,"parts":[r["title"]],"root_name":r["library_name"],"size":r["size"],"modified":r["modified"]} for r in rows]


@app.get("/api/v11/tv")
def plex_tv() -> list[dict]:
    with connection() as db: rows=db.execute("SELECT * FROM plex_media WHERE kind='episode' ORDER BY show_title COLLATE NOCASE,season_number,episode_number,title COLLATE NOCASE").fetchall()
    shows={}
    for r in rows:
        show_name=r["show_title"] or "Unknown show"; show=shows.setdefault((r["library_key"],show_name),{"id":r["library_key"]+":"+show_name,"name":show_name,"root_name":r["library_name"],"episode_count":0,"seasons":{}}); season_no=r["season_number"] or 0; season=show["seasons"].setdefault(season_no,{"name":f"Season {season_no}","episodes":[]}); code=f"S{season_no:02d}E{(r['episode_number'] or 0):02d}"; fake=code+" "+r["title"]+Path(r["path"]).suffix; season["episodes"].append({"path":r["path"],"name":fake,"relative_path":fake,"size":r["size"],"modified":r["modified"]});show["episode_count"]+=1
    return [{**show,"seasons":list(show["seasons"].values())} for show in shows.values()]


def plex_authorized_file(value: str) -> Path:
    path=resolve_existing(value)
    if not path.is_file(): raise HTTPException(400,"Media path is not a file")
    with connection() as db:
        allowed=db.execute("SELECT 1 FROM plex_media WHERE path=? OR path LIKE ? LIMIT 1",(str(path),str(path.parent)+"/%")).fetchone()
    if not allowed: raise HTTPException(403,"File is outside the synchronized Plex catalog")
    return path


v5_module.authorized_file = plex_authorized_file
v7_module.authorized_file = plex_authorized_file
