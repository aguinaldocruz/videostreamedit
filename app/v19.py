from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.v11 import connection, paged_metadata, plex_authorized_file, plex_movies, plex_tv, sync_plex
from app.v2 import probe
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



class VideoTitleRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=30000)


class VideoTitleEditRequest(BaseModel):
    path: str
    title: str = Field(default="", max_length=1000)


class ReportSubtitleActionRequest(BaseModel):
    action: Literal["image_convert", "html_cleanup"]
    streams: list[dict] = Field(min_length=1, max_length=30000)


def first_video_title(path: Path) -> tuple[int, str]:
    video_index = 0
    for stream in probe(path).get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        tags = stream.get("tags") or {}
        return video_index, str(tags.get("title") or "").strip()
    raise HTTPException(422, "Media has no video stream")


@app.post("/api/v19/video-titles")
def video_titles(request: VideoTitleRequest) -> dict:
    paths = list(dict.fromkeys(request.paths))
    if not paths:
        return {"items": {}}
    placeholders = ",".join("?" for _ in paths)
    with connection() as db:
        rows = db.execute(
            f"SELECT path,title,video_index FROM media_video_title WHERE path IN ({placeholders})", paths
        ).fetchall()
    return {"items": {row["path"]: {"title": row["title"], "index": row["video_index"]} for row in rows}}


@app.post("/api/v19/video-title/edit")
def edit_video_title(request: VideoTitleEditRequest) -> dict:
    path = plex_authorized_file(request.path)
    if path.suffix.lower() not in {".mkv", ".mka", ".mks", ".mk3d"}:
        raise HTTPException(422, "Video track title editing currently requires a Matroska file")
    with connection() as db:
        old = db.execute("SELECT title FROM media_video_title WHERE path=?", (str(path),)).fetchone()
    current = str(old["title"] if old else "")
    command = ["mkvpropedit", str(path), "--edit", "track:v1"]
    if request.title.strip(): command += ["--set", f"name={request.title.strip()}"]
    else: command += ["--delete", "name"]
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)
    except FileNotFoundError as exc:
        raise HTTPException(503, "mkvpropedit is not installed") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(422, (getattr(exc, "stderr", None) or "Video track title update failed")[-2000:]) from exc
    title = request.title.strip(); stat = path.stat()
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO media_video_title(path,title,video_index,modified_ns,size,indexed_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)", (str(path), title, 0, stat.st_mtime_ns, stat.st_size))
    logger.info("change=video_track_title file=%s from=%s to=%s", str(path).replace("\n", "\\n"), current, title)
    return {"path": str(path), "title": title}


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
    with connection() as db:
        detection = {}
        for row in db.execute("SELECT path,metadata_language,detected_language,confidence FROM portuguese_language_detection").fetchall():
            current = detection.get(str(row["path"]))
            if current is None or float(row["confidence"]) > current["confidence"]:
                detection[str(row["path"])] = {"confidence": float(row["confidence"]), "metadata_language": str(row["metadata_language"] or ""), "detected_language": str(row["detected_language"] or "")}
    return [{**movie, "alternative_titles": aliases.get(movie["path"], []), "change_requested": movie["path"] in requested, "change_requests": requested.get(movie["path"], []), "portuguese_detection_confidence": (detection.get(str(movie["path"])) or {}).get("confidence"), "portuguese_detection_metadata": (detection.get(str(movie["path"])) or {}).get("metadata_language"), "portuguese_detection_language": (detection.get(str(movie["path"])) or {}).get("detected_language")} for movie in plex_movies()]


@app.get("/api/v19/tv")
def plex_tv_with_alternatives() -> list[dict]:
    aliases = aliases_by_path()
    requested = change_requests_by_path()
    shows = plex_tv()
    # Index activity is derived from the live queue, so the filter remains useful
    # while a long-running core/subtitle/preview index is in progress.
    with connection() as db:
        busy_paths = {
            str(row["path"])
            for row in db.execute(
                "SELECT DISTINCT path FROM index_task_queue WHERE status IN ('pending','running')"
            ).fetchall()
        }
        # Use already indexed audio/subtitle streams only. Listing must not probe
        # episode files, especially for large shows. Video metadata rows are
        # deliberately excluded; external sidecars count as subtitle streams.
        detection = {}
        for row in db.execute("SELECT path,metadata_language,detected_language,confidence FROM portuguese_language_detection").fetchall():
            current = detection.get(str(row["path"]))
            if current is None or float(row["confidence"]) > current["confidence"]:
                detection[str(row["path"])] = {"confidence": float(row["confidence"]), "metadata_language": str(row["metadata_language"] or ""), "detected_language": str(row["detected_language"] or "")}
        stream_counts = {
            str(row["path"]): {
                "audio": int(row["audio_count"] or 0),
                "subtitle": int(row["subtitle_count"] or 0),
                "external": int(row["external_count"] or 0),
            }
            for row in db.execute(
                "SELECT path, "
                "SUM(CASE WHEN stream_type='audio' THEN 1 ELSE 0 END) AS audio_count, "
                "SUM(CASE WHEN stream_type='subtitle' THEN 1 ELSE 0 END) AS subtitle_count, "
                "SUM(CASE WHEN stream_type='external' THEN 1 ELSE 0 END) AS external_count "
                "FROM media_stream_index WHERE stream_type IN ('audio','subtitle','external') GROUP BY path"
            ).fetchall()
        }
    for show in shows:
        paths = [episode["path"] for season in show["seasons"] for episode in season["episodes"]]
        show["index_busy"] = any(path in busy_paths for path in paths)
        confidences = [detection[path] for path in paths if path in detection]
        show["portuguese_detection_confidence"] = max((detection[path]["confidence"] for path in paths if path in detection), default=None)
        top_detection = max((detection[path] for path in paths if path in detection), key=lambda item: item["confidence"], default=None)
        show["portuguese_detection_metadata"] = top_detection["metadata_language"] if top_detection else None
        show["portuguese_detection_language"] = top_detection["detected_language"] if top_detection else None
        show["alternative_titles"] = next((aliases[path] for path in paths if aliases.get(path)), [])
        for season in show["seasons"]:
            for episode in season["episodes"]:
                episode["change_requested"] = episode["path"] in requested
                episode["change_requests"] = requested.get(episode["path"], [])
                episode["stream_counts"] = stream_counts.get(episode["path"])
                episode_detection = detection.get(episode["path"]) or {}
                episode["portuguese_detection_confidence"] = episode_detection.get("confidence")
                episode["portuguese_detection_metadata"] = episode_detection.get("metadata_language")
                episode["portuguese_detection_language"] = episode_detection.get("detected_language")
    return shows


IMAGE_SUBTITLE_CODECS = (
    "dvd_subtitle", "dvb_subtitle", "hdmv_pgs_subtitle", "pgssub", "pgs",
    "vobsub", "xsub", "sup", "idx", "s_hdmv/pgs", "s_vobsub", "s_dvbsub",
)


@app.get("/api/v19/reports/image-subtitles")
def image_subtitle_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    placeholders = ",".join("?" for _ in IMAGE_SUBTITLE_CODECS)
    with connection() as db:
        rows = db.execute(
            f"SELECT path,type_index,stream_type,external_path,language,region,track_name,codec FROM media_stream_index "
            f"WHERE stream_type IN ('subtitle','external') AND lower(trim(codec)) IN ({placeholders}) "
            "ORDER BY path,type_index,external_path",
            IMAGE_SUBTITLE_CODECS,
        ).fetchall()
    refs_by_path = {}
    for row in rows:
        refs_by_path.setdefault(str(row["path"]), []).append(dict(row))
    by_path = {path: len(refs) for path, refs in refs_by_path.items()}
    if kind == "movies":
        items = []
        for movie in plex_movies():
            count = by_path.get(str(movie["path"]), 0)
            if count:
                items.append({
                    "title": Path(str(movie.get("name") or movie["path"])).stem,
                    "root_name": movie.get("root_name") or "",
                    "media_count": 1,
                    "image_subtitle_count": count,
                    "paths": [str(movie["path"])],
                    "streams": refs_by_path.get(str(movie["path"]), []),
                })
    else:
        grouped = {}
        for show in plex_tv():
            match_count = subtitle_count = 0
            for season in show["seasons"]:
                for episode in season["episodes"]:
                    count = by_path.get(str(episode["path"]), 0)
                    if count:
                        match_count += 1
                        subtitle_count += count
            if match_count:
                grouped[(str(show["id"]), str(show["name"]))] = {
                    "title": str(show["name"]),
                    "root_name": str(show.get("root_name") or ""),
                    "media_count": match_count,
                    "image_subtitle_count": subtitle_count,
                    "paths": [episode["path"] for season in show["seasons"] for episode in season["episodes"] if refs_by_path.get(str(episode["path"]))],
                    "streams": [ref for season in show["seasons"] for episode in season["episodes"] for ref in refs_by_path.get(str(episode["path"]), [])],
                }
        items = list(grouped.values())
    items.sort(key=lambda item: item["title"].casefold())
    return {"kind": kind, "items": items, "title_count": len(items), "media_count": sum(item["media_count"] for item in items)}


@app.get("/api/v19/reports/html-subtitles")
def html_subtitle_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    with connection() as db:
        rows = db.execute(
            "SELECT path,source,type_index,external_path,codec FROM subtitle_extended_index "
            "WHERE markup LIKE '%HTML tags%' ORDER BY path,type_index,external_path"
        ).fetchall()
    refs_by_path = {}
    for row in rows:
        refs_by_path.setdefault(str(row["path"]), []).append({"path": str(row["path"]), "source": row["source"], "type_index": int(row["type_index"]), "external_path": row["external_path"] or "", "codec": row["codec"] or ""})
    by_path = {path: len(refs) for path, refs in refs_by_path.items()}
    if kind == "movies":
        items = []
        for movie in plex_movies():
            count = by_path.get(str(movie["path"]), 0)
            if count:
                items.append({"title": Path(str(movie.get("name") or movie["path"])).stem, "root_name": movie.get("root_name") or "", "media_count": 1, "html_subtitle_count": count, "paths": [str(movie["path"])], "streams": refs_by_path.get(str(movie["path"]), [])})
    else:
        items = []
        for show in plex_tv():
            media_count = sum(1 for season in show["seasons"] for episode in season["episodes"] if by_path.get(str(episode["path"]), 0))
            subtitle_count = sum(by_path.get(str(episode["path"]), 0) for season in show["seasons"] for episode in season["episodes"])
            if media_count:
                items.append({"title": str(show["name"]), "root_name": str(show.get("root_name") or ""), "media_count": media_count, "html_subtitle_count": subtitle_count, "paths": [episode["path"] for season in show["seasons"] for episode in season["episodes"] if refs_by_path.get(str(episode["path"]))], "streams": [ref for season in show["seasons"] for episode in season["episodes"] for ref in refs_by_path.get(str(episode["path"]), [])]})
    items.sort(key=lambda item: item["title"].casefold())
    return {"kind": kind, "items": items, "title_count": len(items), "media_count": sum(item["media_count"] for item in items)}




@app.post("/api/v19/reports/subtitle-action")
def queue_report_subtitle_action(request: ReportSubtitleActionRequest) -> dict:
    from app import v65 as task_queue
    queued = 0
    errors = []
    seen = set()
    for raw in request.streams:
        path = str(raw.get("path") or "").strip()
        if not path:
            continue
        source = str(raw.get("source") or "embedded")
        type_index = int(raw.get("type_index", -1))
        external_path = str(raw.get("external_path") or "")
        key = (path, source, type_index, external_path)
        if key in seen:
            continue
        seen.add(key)
        if request.action == "html_cleanup":
            payload = {"path": path, "type_index": type_index if source != "external" else None, "external_path": external_path or None}
            task_queue.enqueue("subtitle_html_cleanup", payload, "Remove HTML tags from report match", deduplicate=True)
            queued += 1
            continue
        language = str(raw.get("language") or "").strip()
        if not language or language.casefold() in {"und", "unknown", "zxx"}:
            errors.append(f"{Path(path).name}: image subtitle language is not set")
            continue
        payload = {"path": path, "type_index": type_index, "external_path": external_path or None, "language": language, "region": str(raw.get("region") or "")}
        task_queue.enqueue("image_subtitle_convert", payload, "Convert image subtitle to SRT", deduplicate=True)
        queued += 1
    if queued:
        logger.info("task_queue event=report_subtitle_action action=%s queued=%d errors=%d", request.action, queued, len(errors))
    return {"queued": queued, "errors": errors}


@app.get("/api/v19/reports/portuguese-language")
def portuguese_language_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    with connection() as db:
        rows = db.execute(
            "SELECT d.path,d.detected_language,d.confidence,d.metadata_region,d.evidence "
            "FROM portuguese_language_detection d JOIN plex_media p ON p.path=d.path "
            "WHERE d.confidence>=0.60 ORDER BY d.path"
        ).fetchall()
    by_path = {}
    for row in rows:
        by_path.setdefault(str(row["path"]), []).append({"detected_language": row["detected_language"], "confidence": round(float(row["confidence"]) * 100, 1), "metadata_region": row["metadata_region"], "evidence": row["evidence"]})
    items = []
    if kind == "movies":
        for movie in plex_movies():
            mismatches = by_path.get(str(movie["path"]), [])
            if mismatches:
                items.append({"title": Path(str(movie.get("name") or movie["path"])).stem, "root_name": movie.get("root_name") or "", "media_count": 1, "mismatches": mismatches})
    else:
        for show in plex_tv():
            episodes = []
            for season in show["seasons"]:
                for episode in season["episodes"]:
                    mismatches = by_path.get(str(episode["path"]), [])
                    if mismatches:
                        episodes.append({"episode": episode.get("name") or Path(episode["path"]).stem, "path": str(episode["path"]), "mismatches": mismatches})
            if episodes:
                items.append({"title": str(show["name"]), "root_name": str(show.get("root_name") or ""), "media_count": len(episodes), "episodes": episodes})
    items.sort(key=lambda item: item["title"].casefold())
    return {"kind": kind, "items": items, "title_count": len(items), "media_count": sum(item["media_count"] for item in items)}




@app.middleware("http")
async def v19_title_assets(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/":
        html = (STATIC_DIR / "v5.html").read_text()
        html = html.replace('<title>VideoStreamEdit</title><link rel="stylesheet" href="/app.css">', '<title>VideoStreamEdit · Stream Metadata Editor</title><meta name="theme-color" content="#14191f"><link rel="icon" href="/brand/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="32x32" href="/brand/favicon-32.png"><link rel="icon" type="image/png" sizes="16x16" href="/brand/favicon-16.png"><link rel="apple-touch-icon" sizes="180x180" href="/brand/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><link rel="stylesheet" href="/assets/v19.css">')
        html = html.replace('<header><h1>VideoStreamEdit</h1><div id="page-title" class="header-page-title"><h2>Movies</h2><p>Choose a movie to edit its audio and subtitle streams.</p></div><nav>', '<header><a class="brand" href="/" aria-label="VideoStreamEdit home"><img src="/brand/header-icon.png" width="48" height="48" alt=""><span><strong>VideoStreamEdit</strong><small>Stream metadata editor</small></span></a><div id="page-title" class="header-page-title"><h2>Movies</h2><p>Choose a movie to edit its audio and subtitle streams.</p></div><nav>')
        html = html.replace('<script src="/app.js"></script>', '<script src="/assets/v19.js"></script>')
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    if request.method == "GET" and request.url.path == "/assets/v19.css":
        names = ("v3.css", "v4.css", "v5.css", "v7-addon.css", "v8-addon.css", "v10-progress.css", "v11-plex.css", "v12-context.css", "v14-brand.css", "v15-path.css", "v16-bulk.css", "v18-value-popup.css", "v19-titles.css", "v20-clone.css", "v22-bulk-clone.css", "v23-session-changes.css", "v25-template-history.css", "v26-stream-layout.css", "v27-fast-defaults.css", "v28-movie-import.css", "v29-change-highlights.css", "v30-destination-order.css", "v31-destination-browser.css", "v32-output-folder.css", "v33-global-busy.css", "v36-inline-combobox.css", "v37-filename.css", "v38-movie-filters.css", "v39-movie-index.css", "v40-track-suggestions.css", "v44-prompt-settings.css", "v46-navigation-pending.css", "v48-setup-tabs.css", "v49-stream-preview.css", "v50-stream-preview.css", "v51-subtitle-properties.css", "v54-split-index.css", "v58-manual-audio-name.css", "v60-preview-layout.css", "v61-preview-overflow.css", "v63-index-controls.css", "v65-task-queue.css", "v67-index-schedules.css", "v68-plex-sync.css", "v69-stream-preview.css", "v77-bulk-track-name.css", "v78-change-requested.css", "v79-season-filters.css", "v82-movie-streams.css", "v83-media-review.css", "v84-activity.css", "v85-remove-cycle.css", "v86-tasks-layout.css", "v87-language-region.css", "v88-reports.css")
        return asset("\n".join((STATIC_DIR / name).read_text() for name in names), "text/css")
    if request.method == "GET" and request.url.path == "/assets/v19.js":
        javascript = (STATIC_DIR / "v33-global-busy.js").read_text() + "\n" + (STATIC_DIR / "v5.js").read_text().replace("'/api/movies'", "'/api/v19/movies'").replace("'/api/tv'", "'/api/v19/tv'")
        for name in ("v8-addon.js", "v9-session.js", "v10-progress.js", "v11-plex.js", "v12-context.js", "v15-path.js", "v16-bulk.js", "v18-value-popup.js", "v19-titles.js", "v20-clone.js", "v21-navigation.js", "v22-bulk-clone.js", "v23-session-changes.js", "v25-removal-safety.js", "v25-template-history.js", "v27-fast-defaults.js", "v28-movie-import.js", "v29-change-highlights.js", "v30-destination-order.js", "v31-destination-browser.js", "v32-output-folder.js", "v36-inline-combobox.js", "v37-filename.js", "v39-movie-index.js", "v42-track-suggestions.js", "v44-prompt-settings.js", "v45-keyboard-navigation.js", "v46-navigation-pending.js", "v47-suggestion-shortcut.js", "v48-setup-tabs.js", "v69-stream-preview.js", "v51-subtitle-properties.js", "v54-split-index.js", "v56-background-index.js", "v58-manual-audio-name.js", "v62-escape-close.js", "v63-index-controls.js", "v65-task-queue.js", "v66-close-pending.js", "v67-index-schedules.js", "v68-plex-sync.js", "v71-pending-html.js", "v72-preview-html-detection.js", "v75-rename-refresh.js", "v76-apply-queue.js", "v77-bulk-track-name.js", "v78-change-requested.js", "v79-season-filters.js", "v81-performance.js", "v82-movie-streams.js", "v83-media-review.js", "v84-activity.js", "v85-remove-cycle.js", "v86-tasks-layout.js", "v87-language-region.js", "v88-reports.js"):
            javascript += "\n" + (STATIC_DIR / name).read_text()
        javascript = javascript.replace("/api/media/details", "/api/v13/media/details").replace("/api/v11/plex/sync", "/api/v19/plex/sync").replace("/api/v7/media/edit", "/api/v43/media/edit")
        return asset(javascript, "text/javascript")
    return await call_next(request)
