from __future__ import annotations

import json
import logging

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.v11 import connection, paged_metadata, plex_movies, plex_tv, sync_plex
from app.v16 import STATIC_DIR, app, asset


logger = logging.getLogger("videostreamedit")


@app.on_event("startup")
def initialize_plex_title_aliases() -> None:
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS plex_title_aliases (path TEXT PRIMARY KEY, alternatives TEXT NOT NULL DEFAULT '[]')")


def title_values(item: dict) -> list[str]:
    values = []
    for key in ("title", "originalTitle", "titleSort"):
        value = str(item.get(key) or "").strip()
        if value and value.casefold() not in {existing.casefold() for existing in values}:
            values.append(value)
    return values


@app.post("/api/v19/plex/sync")
def sync_plex_with_title_aliases() -> dict:
    result = sync_plex()
    with connection() as db:
        selected = [dict(row) for row in db.execute("SELECT library_key,kind FROM plex_libraries WHERE selected=1")]
    aliases: list[tuple[str, str]] = []
    for library in selected:
        show_titles = {}
        if library["kind"] == "show":
            for show in paged_metadata(library["library_key"], library["kind"], 2):
                displayed = show.get("originalTitle") or show.get("title") or ""
                show_titles[str(show.get("ratingKey"))] = [value for value in title_values(show) if value.casefold() != str(displayed).casefold()]
        for item in paged_metadata(library["library_key"], library["kind"]):
            displayed = item.get("title") or ""
            alternatives = ([value for value in title_values(item) if value.casefold() != str(displayed).casefold()]
                            if library["kind"] == "movie"
                            else show_titles.get(str(item.get("grandparentRatingKey")), []))
            encoded = json.dumps(alternatives, ensure_ascii=False)
            for media in item.get("Media", []):
                for part in media.get("Part", []):
                    if part.get("file"):
                        aliases.append((part["file"], encoded))
    with connection() as db:
        db.execute("DELETE FROM plex_title_aliases")
        db.executemany("INSERT OR REPLACE INTO plex_title_aliases(path,alternatives) VALUES(?,?)", aliases)
    logger.info("change=plex_title_aliases_synced media=%d", len(aliases))
    return result


def aliases_by_path() -> dict[str, list[str]]:
    with connection() as db:
        rows = db.execute("SELECT path,alternatives FROM plex_title_aliases").fetchall()
    return {row["path"]: json.loads(row["alternatives"]) for row in rows}


@app.get("/api/v19/movies")
def plex_movies_with_alternatives() -> list[dict]:
    aliases = aliases_by_path()
    return [{**movie, "alternative_titles": aliases.get(movie["path"], [])} for movie in plex_movies()]


@app.get("/api/v19/tv")
def plex_tv_with_alternatives() -> list[dict]:
    aliases = aliases_by_path()
    shows = plex_tv()
    for show in shows:
        paths = [episode["path"] for season in show["seasons"] for episode in season["episodes"]]
        show["alternative_titles"] = next((aliases[path] for path in paths if aliases.get(path)), [])
    return shows


@app.middleware("http")
async def v19_title_assets(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text()
        html = html.replace('<title>VideoStreamEdit</title><link rel="stylesheet" href="/app.css">', '<title>VideoStreamEdit · Stream Metadata Editor</title><meta name="theme-color" content="#14191f"><link rel="icon" href="/brand/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="32x32" href="/brand/favicon-32.png"><link rel="icon" type="image/png" sizes="16x16" href="/brand/favicon-16.png"><link rel="apple-touch-icon" sizes="180x180" href="/brand/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><link rel="stylesheet" href="/assets/v19.css">')
        html = html.replace('<header><h1>VideoStreamEdit</h1><nav>', '<header><a class="brand" href="/" aria-label="VideoStreamEdit home"><img src="/brand/header-icon.png" width="48" height="48" alt=""><span><strong>VideoStreamEdit</strong><small>Stream metadata editor</small></span></a><nav>')
        html = html.replace('<script src="/app.js"></script>', '<script src="/assets/v19.js"></script>')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v19.css":
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css", "v14-brand.css", "v15-path.css", "v16-bulk.css", "v18-value-popup.css", "v19-titles.css", "v20-clone.css", "v22-bulk-clone.css", "v23-session-changes.css", "v25-template-history.css", "v26-stream-layout.css", "v27-fast-defaults.css", "v28-movie-import.css", "v29-change-highlights.css", "v30-destination-order.css", "v31-destination-browser.css", "v32-output-folder.css", "v33-global-busy.css", "v36-inline-combobox.css", "v37-filename.css", "v38-movie-filters.css", "v39-movie-index.css", "v40-track-suggestions.css")
        return asset("\n".join((STATIC_DIR / name).read_text() for name in names), "text/css")
    if request.method == "GET" and request.url.path == "/assets/v19.js":
        javascript = (STATIC_DIR / "v33-global-busy.js").read_text() + "\n" + (STATIC_DIR / "v5.js").read_text().replace("'/api/movies'", "'/api/v19/movies'").replace("'/api/tv'", "'/api/v19/tv'")
        for name in ("v8-addon.js", "v9-session.js", "v10-progress.js", "v11-plex.js", "v12-context.js", "v15-path.js", "v16-bulk.js", "v18-value-popup.js", "v19-titles.js", "v20-clone.js", "v21-navigation.js", "v22-bulk-clone.js", "v23-session-changes.js", "v25-removal-safety.js", "v25-template-history.js", "v27-fast-defaults.js", "v28-movie-import.js", "v29-change-highlights.js", "v30-destination-order.js", "v31-destination-browser.js", "v32-output-folder.js", "v36-inline-combobox.js", "v37-filename.js", "v39-movie-index.js", "v42-track-suggestions.js"):
            javascript += "\n" + (STATIC_DIR / name).read_text()
        javascript = javascript.replace("/api/media/details", "/api/v13/media/details").replace("/api/v11/plex/sync", "/api/v19/plex/sync").replace("/api/v7/media/edit", "/api/v43/media/edit")
        return asset(javascript, "text/javascript")
    return await call_next(request)
