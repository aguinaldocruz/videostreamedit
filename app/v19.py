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


def queued_change_summary(task_type: str, label: str, payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        return label or task_type.replace("_", " ").title()
    if task_type == "filtered_stream_edit":
        request = payload.get("request") or {}
        filters = request.get("filters") or {}
        stream = str(filters.get("stream_type") or "stream").capitalize()
        changes = []
        for field, title in (("language", "language"), ("region", "region"), ("track_name", "track name")):
            if field in (request.get("changed_fields") or []):
                before = filters.get(field)
                after = request.get(field)
                changes.append(f"{title}: {before or '<empty>'} → {after or '<empty>'}")
        if request.get("integrate"):
            changes.append("integrate external subtitle")
        if request.get("remove"):
            changes.append("remove matching stream")
        return f"{stream}: " + "; ".join(changes or ["filtered stream change"])
    if task_type == "media_edit":
        edit = payload.get("edit") or payload
        changes = []
        for track in edit.get("tracks") or []:
            kind = str(track.get("codec_type") or "stream").capitalize()
            number = int(track.get("type_index") or 0) + 1
            values = []
            if "language" in track:
                values.append(f"language → {track.get('language') or '<empty>'}")
            if "region" in track:
                values.append(f"region → {track.get('region') or '<empty>'}")
            if "title" in track:
                values.append(f"track name → {track.get('title') or '<empty>'}")
            if values:
                changes.append(f"{kind} {number}: " + ", ".join(values))
        removed = len(edit.get("remove") or [])
        integrated = sum(bool(item.get("embed")) for item in edit.get("external_subtitles") or [])
        if removed:
            changes.append(f"remove {removed} stream{'s' if removed != 1 else ''}")
        if integrated:
            changes.append(f"integrate {integrated} external subtitle{'s' if integrated != 1 else ''}")
        return "; ".join(changes[:5]) or label or "Media stream change"
    if task_type == "movie_import":
        return label or "Import movie and apply stream changes"
    if task_type == "subtitle_html_cleanup":
        return "Remove subtitle HTML tags"
    return label or task_type.replace("_", " ").title()


def change_requests_by_path() -> dict[str, list[dict]]:
    with connection() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_change_request'").fetchone()
        if not exists:
            return {}
        rows = db.execute(
            """SELECT marker.path,queue.id,queue.status,queue.task_type,queue.label,queue.payload_json,marker.requested_at
                 FROM media_change_request marker JOIN task_queue queue ON queue.id=marker.task_id
                WHERE queue.status IN ('pending','running','failed') ORDER BY queue.id"""
        ).fetchall()
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row["path"], []).append({
            "id": row["id"], "status": row["status"], "requested_at": row["requested_at"],
            "summary": queued_change_summary(row["task_type"], row["label"], row["payload_json"]),
        })
    return result


def change_requested_paths() -> set[str]:
    return set(change_requests_by_path())


@app.get("/api/v19/movies")
def plex_movies_with_alternatives() -> list[dict]:
    aliases = aliases_by_path()
    requested = change_requests_by_path()
    return [{**movie, "alternative_titles": aliases.get(movie["path"], []), "change_requested": movie["path"] in requested, "change_requests": requested.get(movie["path"], [])} for movie in plex_movies()]


@app.get("/api/v19/tv")
def plex_tv_with_alternatives() -> list[dict]:
    aliases = aliases_by_path()
    requested = change_requests_by_path()
    shows = plex_tv()
    for show in shows:
        paths = [episode["path"] for season in show["seasons"] for episode in season["episodes"]]
        show["alternative_titles"] = next((aliases[path] for path in paths if aliases.get(path)), [])
        for season in show["seasons"]:
            for episode in season["episodes"]:
                episode["change_requested"] = episode["path"] in requested
                episode["change_requests"] = requested.get(episode["path"], [])
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
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css", "v14-brand.css", "v15-path.css", "v16-bulk.css", "v18-value-popup.css", "v19-titles.css", "v20-clone.css", "v22-bulk-clone.css", "v23-session-changes.css", "v25-template-history.css", "v26-stream-layout.css", "v27-fast-defaults.css", "v28-movie-import.css", "v29-change-highlights.css", "v30-destination-order.css", "v31-destination-browser.css", "v32-output-folder.css", "v33-global-busy.css", "v36-inline-combobox.css", "v37-filename.css", "v38-movie-filters.css", "v39-movie-index.css", "v40-track-suggestions.css", "v44-prompt-settings.css", "v46-navigation-pending.css", "v48-setup-tabs.css", "v49-stream-preview.css", "v50-stream-preview.css", "v51-subtitle-properties.css", "v54-split-index.css", "v58-manual-audio-name.css", "v60-preview-layout.css", "v61-preview-overflow.css", "v63-index-controls.css", "v65-task-queue.css", "v67-index-schedules.css", "v68-plex-sync.css", "v69-stream-preview.css", "v77-bulk-track-name.css", "v78-change-requested.css", "v79-season-filters.css", "v82-movie-streams.css", "v83-media-review.css", "v84-activity.css", "v85-remove-cycle.css", "v86-tasks-layout.css")
        return asset("\n".join((STATIC_DIR / name).read_text() for name in names), "text/css")
    if request.method == "GET" and request.url.path == "/assets/v19.js":
        javascript = (STATIC_DIR / "v33-global-busy.js").read_text() + "\n" + (STATIC_DIR / "v5.js").read_text().replace("'/api/movies'", "'/api/v19/movies'").replace("'/api/tv'", "'/api/v19/tv'")
        for name in ("v8-addon.js", "v9-session.js", "v10-progress.js", "v11-plex.js", "v12-context.js", "v15-path.js", "v16-bulk.js", "v18-value-popup.js", "v19-titles.js", "v20-clone.js", "v21-navigation.js", "v22-bulk-clone.js", "v23-session-changes.js", "v25-removal-safety.js", "v25-template-history.js", "v27-fast-defaults.js", "v28-movie-import.js", "v29-change-highlights.js", "v30-destination-order.js", "v31-destination-browser.js", "v32-output-folder.js", "v36-inline-combobox.js", "v37-filename.js", "v39-movie-index.js", "v42-track-suggestions.js", "v44-prompt-settings.js", "v45-keyboard-navigation.js", "v46-navigation-pending.js", "v47-suggestion-shortcut.js", "v48-setup-tabs.js", "v69-stream-preview.js", "v51-subtitle-properties.js", "v54-split-index.js", "v56-background-index.js", "v58-manual-audio-name.js", "v62-escape-close.js", "v63-index-controls.js", "v65-task-queue.js", "v66-close-pending.js", "v67-index-schedules.js", "v68-plex-sync.js", "v71-pending-html.js", "v72-preview-html-detection.js", "v75-rename-refresh.js", "v76-apply-queue.js", "v77-bulk-track-name.js", "v78-change-requested.js", "v79-season-filters.js", "v81-performance.js", "v82-movie-streams.js", "v83-media-review.js", "v84-activity.js", "v85-remove-cycle.js", "v86-tasks-layout.js"):
            javascript += "\n" + (STATIC_DIR / name).read_text()
        javascript = javascript.replace("/api/media/details", "/api/v13/media/details").replace("/api/v11/plex/sync", "/api/v19/plex/sync").replace("/api/v7/media/edit", "/api/v43/media/edit")
        return asset(javascript, "text/javascript")
    return await call_next(request)
