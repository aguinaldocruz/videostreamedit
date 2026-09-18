from __future__ import annotations

import json
import logging
import re
import subprocess
import hashlib
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.v11 import connection, paged_metadata, plex_authorized_file, plex_movies, plex_tv, sync_plex
from app.v2 import probe
from app.v5 import external_subtitles, canonical_language
from app.v16 import STATIC_DIR, app, asset


logger = logging.getLogger("videostreamedit")


@app.on_event("startup")
def initialize_plex_title_aliases() -> None:
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS plex_title_aliases (path TEXT PRIMARY KEY, alternatives TEXT NOT NULL DEFAULT '[]')")
        db.execute("CREATE TABLE IF NOT EXISTS duplicate_language_report_suppressions (path TEXT NOT NULL, stream_type TEXT NOT NULL, language TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(path,stream_type,language))")


def title_values(item: dict) -> list[str]:
    values = []
    for key in ("title", "originalTitle", "titleSort"):
        value = str(item.get(key) or "").strip()
        if value and value.casefold() not in {existing.casefold() for existing in values}:
            values.append(value)
    return values



class ForcedEvaluationRequest(BaseModel):
    path: str


def _subtitle_metrics(text: str, duration: float) -> dict:
    blocks = re.split(r"\n\s*\n", text.strip()) if text.strip() else []
    cues = 0
    covered = 0.0
    characters = 0
    marker_cues = 0
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing = next((line for line in lines if "-->" in line), "")
        if not timing:
            continue
        cues += 1
        match = re.search(r"(\d{1,2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,\.](\d{3})", timing)
        if match:
            values = [int(value) for value in match.groups()]
            start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
            end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
            covered += max(0.0, end - start)
        payload = " ".join(line for line in lines if "-->" not in line and not line.isdigit())
        payload = re.sub(r"<[^>]+>", " ", payload).strip()
        characters += len(payload)
        if re.search(r"[\[\(].{1,80}[\]\)]|♪|♫|\b(?:[A-Z][A-Z .'-]{2,}|signs?|speaks? foreign)\b", payload, re.I):
            marker_cues += 1
    minutes = max(duration / 60.0, 1.0)
    density = cues / minutes
    coverage = min(1.0, covered / duration) if duration > 0 else 0.0
    sparse = density <= 8 and coverage <= 0.35
    marker_ratio = marker_cues / cues if cues else 0.0
    score = 0.0
    if sparse: score += 0.45
    if density <= 3: score += 0.20
    if coverage <= 0.12: score += 0.15
    if marker_ratio >= 0.20: score += 0.15
    if cues and characters / cues <= 45: score += 0.05
    return {"cues": cues, "coverage": round(coverage, 3), "density": round(density, 2), "marker_ratio": round(marker_ratio, 3), "score": round(min(score, 0.95), 3), "text_available": bool(cues)}


def _extract_subtitle_text(media: Path, index: int, codec: str) -> str:
    if codec.lower() in {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}:
        try:
            result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media), "-map", f"0:s:{index}", "-f", "srt", "pipe:1"], capture_output=True, text=True, timeout=180, check=True)
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    return ""


@app.post("/api/v19/stream/evaluate-forced")
def evaluate_forced_subtitles(request: ForcedEvaluationRequest) -> dict:
    from app.v79 import analyze_sdh, common_detection_languages, detect_common_variant

    media = plex_authorized_file(request.path)
    info = probe(media)
    duration = float((info.get("format") or {}).get("duration") or 0)
    subtitles = []
    subtitle_index = 0
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags") or {}
        codec = str(stream.get("codec_name") or "unknown")
        text = _extract_subtitle_text(media, subtitle_index, codec)
        metrics = _subtitle_metrics(text, duration)
        sdh_label, sdh_confidence, sdh_evidence = analyze_sdh(text)
        allowed = {value.casefold().split("-", 1)[0].split("_", 1)[0] for value in common_detection_languages()}
        detected_language, language_confidence, language_evidence = detect_common_variant(text, allowed) if text else ("", 0.0, "")
        subtitles.append({"detected_language": detected_language, "language_confidence": language_confidence, "language_evidence": language_evidence, "source": "embedded", "type_index": subtitle_index, "codec": codec, "language": tags.get("language") or "", "title": tags.get("title") or "", "forced": bool((stream.get("disposition") or {}).get("forced")), "default": bool((stream.get("disposition") or {}).get("default")), "sdh_label": sdh_label, "sdh_confidence": sdh_confidence, "sdh_evidence": sdh_evidence, **metrics})
        subtitle_index += 1
    for item in external_subtitles(media):
        path = Path(str(item.get("path") or item.get("external_path") or ""))
        text = ""
        if path.is_file() and path.suffix.lower() in {".srt", ".vtt", ".ass", ".ssa"}:
            try: text = path.read_text(encoding="utf-8", errors="replace")
            except OSError: text = ""
        metrics = _subtitle_metrics(text, duration)
        sdh_label, sdh_confidence, sdh_evidence = analyze_sdh(text)
        allowed = {value.casefold().split("-", 1)[0].split("_", 1)[0] for value in common_detection_languages()}
        detected_language, language_confidence, language_evidence = detect_common_variant(text, allowed) if text else ("", 0.0, "")
        subtitles.append({"detected_language": detected_language, "language_confidence": language_confidence, "language_evidence": language_evidence, "source": "external", "path": str(path), "codec": path.suffix.lstrip(".") or "unknown", "language": item.get("language") or "", "title": item.get("title") or path.name, "forced": bool(item.get("forced")), "default": bool(item.get("default")), "sdh_label": sdh_label, "sdh_confidence": sdh_confidence, "sdh_evidence": sdh_evidence, **metrics})
    text_items = [item for item in subtitles if item["text_available"]]
    for item in subtitles:
        if item["text_available"]:
            if item["score"] >= 0.70: item["recommendation"] = "Likely forced"
            elif item["score"] <= 0.25 and item["coverage"] >= 0.45: item["recommendation"] = "Likely full subtitle"
            else: item["recommendation"] = "Uncertain"
        else:
            item["recommendation"] = "Cannot evaluate automatically (graphical or unsupported subtitle)"
    return {"path": str(media), "subtitle_count": len(subtitles), "analyzed": len(text_items), "message": "Analysis compares all subtitle tracks in this media; recommendations are non-destructive.", "subtitles": subtitles}


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


def audio_detection_by_path() -> dict[str, dict]:
    with connection() as db:
        try:
            rows = db.execute("SELECT path,metadata_language,detected_language,confidence FROM audio_language_detection WHERE mismatch=1").fetchall()
        except Exception:
            return {}
    result = {}
    for row in rows:
        item = {"confidence": float(row["confidence"]), "metadata_language": str(row["metadata_language"] or ""), "detected_language": str(row["detected_language"] or "")}
        current = result.get(str(row["path"]))
        if current is None or item["confidence"] > current["confidence"]:
            result[str(row["path"])] = item
    return result


@app.get("/api/v19/movies")
def plex_movies_with_alternatives() -> list[dict]:
    aliases = aliases_by_path()
    requested = change_requests_by_path()
    audio_detection = audio_detection_by_path()
    with connection() as db:
        detection = {}
        for row in db.execute("SELECT path,metadata_language,detected_language,confidence FROM portuguese_language_detection").fetchall():
            current = detection.get(str(row["path"]))
            if current is None or float(row["confidence"]) > current["confidence"]:
                detection[str(row["path"])] = {"confidence": float(row["confidence"]), "metadata_language": str(row["metadata_language"] or ""), "detected_language": str(row["detected_language"] or "")}
    return [{**movie, "alternative_titles": aliases.get(movie["path"], []), "change_requested": movie["path"] in requested, "change_requests": requested.get(movie["path"], []), "portuguese_detection_confidence": (detection.get(str(movie["path"])) or {}).get("confidence"), "portuguese_detection_metadata": (detection.get(str(movie["path"])) or {}).get("metadata_language"), "portuguese_detection_language": (detection.get(str(movie["path"])) or {}).get("detected_language"), "audio_detection_confidence": (audio_detection.get(str(movie["path"])) or {}).get("confidence"), "audio_detection_metadata": (audio_detection.get(str(movie["path"])) or {}).get("metadata_language"), "audio_detection_language": (audio_detection.get(str(movie["path"])) or {}).get("detected_language")} for movie in plex_movies()]


@app.get("/api/v19/tv")
def plex_tv_with_alternatives() -> list[dict]:
    aliases = aliases_by_path()
    requested = change_requests_by_path()
    shows = plex_tv()
    audio_detection = audio_detection_by_path()
    # Index activity is derived from the live queue, so the filter remains useful
    # while a long-running core/subtitle/preview index is in progress.
    with connection() as db:
        busy_paths = {
            str(row["path"])
            for row in db.execute(
                "SELECT DISTINCT path FROM index_task_queue WHERE status IN ('pending','running','failed')"
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
        top_audio = max((audio_detection[path] for path in paths if path in audio_detection), key=lambda item: item["confidence"], default=None)
        show["audio_detection_confidence"] = top_audio["confidence"] if top_audio else None
        show["audio_detection_metadata"] = top_audio["metadata_language"] if top_audio else None
        show["audio_detection_language"] = top_audio["detected_language"] if top_audio else None
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
                audio_episode = audio_detection.get(episode["path"]) or {}
                episode["audio_detection_confidence"] = audio_episode.get("confidence")
                episode["audio_detection_metadata"] = audio_episode.get("metadata_language")
                episode["audio_detection_language"] = audio_episode.get("detected_language")
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
        # HTML cleanup is valid only for text subtitle codecs.  Older index
        # rows (or a stale codec inspection) may contain markup metadata for a
        # graphical stream, but those must never appear as actionable HTML
        # cleanup entries.  Also hide both the preflight and child task while
        # either is waiting, so the report cannot enqueue a duplicate request.
        text_codecs = ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text")
        placeholders = ",".join("?" for _ in text_codecs)
        rows = db.execute(
            "SELECT path,source,type_index,external_path,codec FROM subtitle_extended_index "
            "WHERE markup LIKE ? AND lower(codec) IN (" + placeholders + ") "
            "AND path NOT IN (SELECT CAST(payload_json AS JSONB)->>'path' FROM task_queue "
            "WHERE task_type IN ('subtitle_html_preflight','subtitle_html_cleanup') "
            "AND status IN ('pending','running')) "
            "AND path NOT IN (SELECT path FROM index_task_queue WHERE job='subtitles' "
            "AND status IN ('pending','running')) "
            "ORDER BY path,type_index,external_path",
            ("%HTML tags%", *text_codecs),
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


@app.get("/api/v19/reports/damaged-subtitles")
def damaged_subtitle_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    text_codecs = ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text")
    placeholders = ",".join("?" for _ in text_codecs)
    with connection() as db:
        rows = db.execute(
            "SELECT path,source,type_index,external_path,codec,damage FROM subtitle_extended_index "
            "WHERE damage IS NOT NULL AND damage!='' AND damage!='None' "
            "AND lower(codec) IN (" + placeholders + ") "
            "AND path NOT IN (SELECT path FROM index_task_queue WHERE job='subtitles' AND status IN ('pending','running')) "
            "ORDER BY path,type_index,external_path",
            text_codecs,
        ).fetchall()
    refs_by_path = {}
    for row in rows:
        refs_by_path.setdefault(str(row["path"]), []).append({
            "path": str(row["path"]), "source": row["source"],
            "type_index": int(row["type_index"]), "external_path": row["external_path"] or "",
            "codec": row["codec"] or "", "damage": row["damage"] or "",
        })
    if kind == "movies":
        items = []
        for movie in plex_movies():
            path = str(movie["path"])
            refs = refs_by_path.get(path, [])
            if refs:
                items.append({"title": Path(str(movie.get("name") or path)).stem,
                              "root_name": movie.get("root_name") or "", "media_count": 1,
                              "damaged_subtitle_count": len(refs), "paths": [path], "streams": refs})
    else:
        items = []
        for show in plex_tv():
            episodes = []
            for season in show["seasons"]:
                for episode in season["episodes"]:
                    refs = refs_by_path.get(str(episode["path"]), [])
                    if refs:
                        episodes.append({"path": str(episode["path"]), "episode": episode.get("episode") or episode.get("name") or Path(str(episode["path"])).stem, "streams": refs})
            if episodes:
                items.append({"title": str(show["name"]), "root_name": str(show.get("root_name") or ""),
                              "media_count": len(episodes), "damaged_subtitle_count": sum(len(ep["streams"]) for ep in episodes),
                              "paths": [ep["path"] for ep in episodes], "episodes": episodes,
                              "streams": [stream for ep in episodes for stream in ep["streams"]]})
    items.sort(key=lambda item: item["title"].casefold())
    return {"kind": kind, "items": items, "title_count": len(items), "media_count": sum(item["media_count"] for item in items)}


@app.post("/api/v19/reports/html-subtitles/revalidate")
def revalidate_html_subtitle_report(kind: str = "all") -> dict:
    """Reinspect media currently represented by the HTML report.

    The report is backed by a cache; this schedules the subtitle index worker
    against those paths so codec, extraction and markup are rebuilt from the
    current file before the next report load.
    """
    if kind not in {"all", "tv", "movies"}:
        raise HTTPException(400, "Kind must be all, tv or movies")
    from app.v80 import enqueue as enqueue_index
    media_kind = {"tv": "episode", "movies": "movie"}.get(kind)
    with connection() as db:
        if media_kind:
            rows = db.execute(
                "SELECT DISTINCT s.path FROM subtitle_extended_index s JOIN plex_media p ON p.path=s.path "
                "WHERE s.markup LIKE '%HTML tags%' AND lower(s.codec) IN ('subrip','srt','ass','ssa','webvtt','mov_text','text') AND p.kind=?",
                (media_kind,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT DISTINCT s.path FROM subtitle_extended_index s JOIN plex_media p ON p.path=s.path "
                "WHERE s.markup LIKE '%HTML tags%' AND lower(s.codec) IN ('subrip','srt','ass','ssa','webvtt','mov_text','text')"
            ).fetchall()
    queued = 0
    for row in rows:
        if enqueue_index("subtitles", str(row[0]), "Revalidate HTML subtitle report"):
            queued += 1
    logger.info("index_queue event=html_report_revalidation kind=%s discovered=%d queued=%d", kind, len(rows), queued)
    return {"discovered": len(rows), "queued": queued, "kind": kind}


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
            task_queue.enqueue("subtitle_html_preflight", payload, "Preflight HTML cleanup from report match", deduplicate=True)
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
            "SELECT d.path,d.source,d.type_index,d.external_path,d.metadata_language,d.detected_language,d.confidence,d.metadata_region,d.evidence,d.sdh_label,d.sdh_confidence,d.sdh_evidence "
            "FROM portuguese_language_detection d JOIN plex_media p ON p.path=d.path "
            "WHERE d.confidence>=0.60 AND d.path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running')) ORDER BY d.path"
        ).fetchall()
    by_path = {}
    for row in rows:
        by_path.setdefault(str(row["path"]), []).append({"source": row["source"], "type_index": int(row["type_index"]), "external_path": row["external_path"], "metadata_language": row["metadata_language"], "metadata_region": row["metadata_region"], "detected_language": row["detected_language"], "confidence": round(float(row["confidence"]) * 100, 1), "evidence": row["evidence"], "sdh_label": row["sdh_label"] or "", "sdh_confidence": round(float(row["sdh_confidence"] or 0) * 100, 1), "sdh_evidence": row["sdh_evidence"] or ""})
    items = []
    if kind == "movies":
        for movie in plex_movies():
            mismatches = by_path.get(str(movie["path"]), [])
            if mismatches:
                items.append({"title": Path(str(movie.get("name") or movie["path"])).stem, "path": str(movie["path"]), "root_name": movie.get("root_name") or "", "media_count": 1, "mismatches": mismatches})
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
    fixable = 0
    for item in items:
        groups = item.get("episodes") or [{"mismatches": item.get("mismatches") or []}]
        fixable += sum(1 for group in groups for mismatch in group.get("mismatches") or [] if float(mismatch.get("confidence") or 0) >= 80 and mismatch.get("source") == "embedded")
    return {"kind": kind, "items": items, "title_count": len(items), "media_count": sum(item["media_count"] for item in items), "fixable_above_80": fixable}


class LanguageDetectionFixRequest(BaseModel):
    kind: Literal["tv", "movies"]


def _detected_language_pair(value: str) -> tuple[str, str] | None:
    key = str(value or "").strip().casefold().replace("_", "-")
    if key in {"pt-br", "pob", "por-br"}: return "pt", "BR"
    if key in {"pt-pt", "pt", "por-pt"}: return "pt", "PT"
    if key in {"en", "eng"}: return "en", ""
    return None


@app.post("/api/v19/reports/portuguese-language/fix")
def fix_portuguese_language_report(request: LanguageDetectionFixRequest) -> dict:
    # Build one queued media_edit per file, grouping all embedded subtitle
    # streams above 80% certainty so each file is processed only once.
    with connection() as db:
        rows = db.execute("""SELECT d.path,d.source,d.type_index,d.detected_language,d.confidence,p.kind
            FROM portuguese_language_detection d JOIN plex_media p ON p.path=d.path
            WHERE d.confidence>=0.80 AND d.source='embedded'
              AND ((?='movies' AND p.kind='movie') OR (?='tv' AND p.kind='episode'))
            ORDER BY d.path,d.type_index""", (request.kind, request.kind)).fetchall()
    grouped: dict[str, list[dict]] = {}
    skipped = 0
    for row in rows:
        pair = _detected_language_pair(row["detected_language"])
        if not pair:
            skipped += 1
            continue
        grouped.setdefault(str(row["path"]), []).append({"codec_type": "subtitle", "type_index": int(row["type_index"]), "language": pair[0], "region": pair[1]})
    from app import v65 as task_queue
    queued = 0
    task_ids = []
    for path, tracks in grouped.items():
        task = task_queue.enqueue("media_edit", {"edit": {"path": path, "tracks": tracks}, "reindex_indexes": ["core", "subtitles"]}, "Fix detected subtitle languages (above 80%)", deduplicate=True)
        task_ids.append(task["id"]); queued += len(tracks)
    logger.info("task_queue event=language_detection_fix kind=%s media=%d streams=%d skipped=%d", request.kind, len(task_ids), queued, skipped)
    return {"queued_media": len(task_ids), "queued_streams": queued, "skipped": skipped, "task_ids": task_ids}




class DuplicateLanguageSettings(BaseModel):
    audio: list[str] = Field(default_factory=list, max_length=100)
    subtitle: list[str] = Field(default_factory=list, max_length=100)

def _duplicate_language_values(stream_type: str) -> list[str]:
    key = "duplicate_report_audio_languages" if stream_type == "audio" else "duplicate_report_subtitle_languages"
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key=?", (key,)).fetchone()
        fallback = db.execute("SELECT value FROM language_detection_settings WHERE key='common_languages'").fetchone()
    try: values = json.loads(row["value"]) if row and row["value"] else json.loads(fallback["value"] if fallback else "[]")
    except (TypeError, ValueError, json.JSONDecodeError): values = []
    return sorted({canonical_language(str(value)) for value in values if canonical_language(str(value)) and canonical_language(str(value)) not in {"und", "unknown"}})

@app.get("/api/v19/settings/duplicate-languages")
def get_duplicate_language_settings() -> dict:
    return {"audio": _duplicate_language_values("audio"), "subtitle": _duplicate_language_values("subtitle")}

@app.put("/api/v19/settings/duplicate-languages")
def save_duplicate_language_settings(request: DuplicateLanguageSettings) -> dict:
    def clean(values): return sorted({canonical_language(str(value)) for value in values if canonical_language(str(value)) and canonical_language(str(value)) not in {"und", "unknown"}})
    audio, subtitle = clean(request.audio), clean(request.subtitle)
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES('duplicate_report_audio_languages',?)", (json.dumps(audio, ensure_ascii=False),))
        db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES('duplicate_report_subtitle_languages',?)", (json.dumps(subtitle, ensure_ascii=False),))
    return {"audio": audio, "subtitle": subtitle}

def _duplicate_group_fingerprint(streams: list[dict]) -> str:
    fields = [{"source": str(row.get("source") or ""), "type_index": int(row.get("type_index") or 0), "external_path": str(row.get("external_path") or ""), "language": canonical_language(str(row.get("language") or "")), "region": str(row.get("region") or "").upper(), "track_name": str(row.get("track_name") or ""), "codec": str(row.get("codec") or "")} for row in streams]
    return hashlib.sha256(json.dumps(sorted(fields, key=lambda value: (value["source"], value["type_index"], value["external_path"])), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

class DuplicateLanguageSuppressRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    stream_type: Literal["audio", "subtitle"]
    language: str = Field(min_length=1, max_length=32)

@app.post("/api/v19/reports/duplicate-languages/suppress")
def suppress_duplicate_language_report(request: DuplicateLanguageSuppressRequest) -> dict:
    path = str(Path(request.path).resolve()); language = canonical_language(request.language)
    with connection() as db:
        rows = db.execute("SELECT source,type_index,external_path,codec,language,region,track_name FROM media_stream_index WHERE path=? AND stream_type=?", (path, request.stream_type)).fetchall()
    streams = [dict(row) for row in rows if canonical_language(str(row["language"] or "")) == language]
    if len(streams) < 2: return {"suppressed": False, "reason": "The duplicate no longer exists"}
    fingerprint = _duplicate_group_fingerprint(streams)
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO duplicate_language_report_suppressions(path,stream_type,language,fingerprint) VALUES(?,?,?,?)", (path, request.stream_type, language, fingerprint))
    logger.info("report=duplicate_language_suppressed path=%s type=%s language=%s", path.replace("\n", "\\n"), request.stream_type, language)
    return {"suppressed": True, "path": path, "stream_type": request.stream_type, "language": language}

@app.get("/api/v19/reports/duplicate-languages")
def duplicate_language_report(kind: str, stream_type: str) -> dict:
    if kind not in {"tv", "movies"} or stream_type not in {"audio", "subtitle"}: raise HTTPException(400, "Invalid report selection")
    allowed = set(_duplicate_language_values(stream_type))
    with connection() as db:
        rows = db.execute("SELECT path,source,type_index,external_path,codec,language,region,track_name FROM media_stream_index WHERE stream_type=? AND path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running')) ORDER BY path,type_index", (stream_type,)).fetchall()
        suppressed = {(str(row["path"]), canonical_language(str(row["language"])), str(row["fingerprint"])) for row in db.execute("SELECT path,language,fingerprint FROM duplicate_language_report_suppressions WHERE stream_type=?", (stream_type,)).fetchall()}
    by_path = {}
    for row in rows:
        language = canonical_language(str(row["language"] or ""))
        if not language or (allowed and language not in allowed): continue
        by_path.setdefault(str(row["path"]), {}).setdefault(language, []).append(dict(row))
    duplicates = {}
    for path, groups in by_path.items():
        visible = {}
        for language, streams in groups.items():
            if len(streams) > 1 and (path, language, _duplicate_group_fingerprint(streams)) not in suppressed: visible[language] = streams
        if visible: duplicates[path] = visible
    def detail(path, groups): return [{"language": language, "count": len(streams), "streams": streams} for language, streams in sorted(groups.items()) if len(streams)>1]
    items=[]
    if kind == "movies":
        for movie in plex_movies():
            path=str(movie["path"]); groups=duplicates.get(path)
            if groups: items.append({"title": Path(str(movie.get("name") or path)).stem, "path": path, "root_name": movie.get("root_name") or "", "duplicates": detail(path, groups)})
    else:
        for show in plex_tv():
            episodes=[]
            for season in show["seasons"]:
                for episode in season["episodes"]:
                    path=str(episode["path"]); groups=duplicates.get(path)
                    if groups: episodes.append({"episode": episode.get("name") or Path(path).stem, "path": path, "duplicates": detail(path, groups)})
            if episodes: items.append({"title": str(show["name"]), "root_name": str(show.get("root_name") or ""), "media_count": len(episodes), "episodes": episodes})
    items.sort(key=lambda item: item["title"].casefold())
    return {"kind": kind, "stream_type": stream_type, "languages": sorted(allowed), "items": items, "title_count": len(items), "media_count": sum(item.get("media_count",1) for item in items)}

@app.get("/api/v19/reports/forced-streams")
def forced_stream_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    with connection() as db:
        setting = db.execute("SELECT value FROM language_detection_settings WHERE key='forced_report_excluded_track_names'").fetchone()
        rows = db.execute("""SELECT path,stream_type,type_index,external_path,codec,language,region,track_name
            FROM media_stream_index WHERE is_forced=1 AND path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running')) ORDER BY path,type_index""").fetchall()
    try:
        excluded = {str(value).strip().casefold() for value in (json.loads(setting["value"]) if setting else []) if str(value).strip()}
    except (TypeError, ValueError, json.JSONDecodeError):
        excluded = set()
    by_path = {}
    for row in rows:
        if str(row["track_name"] or "").strip().casefold() in excluded:
            continue
        by_path.setdefault(str(row["path"]), []).append(dict(row))
    items = []
    if kind == "movies":
        for movie in plex_movies():
            path = str(movie["path"]); refs = by_path.get(path, [])
            if refs:
                items.append({"title": Path(str(movie.get("name") or path)).stem, "path": path, "root_name": movie.get("root_name") or "", "media_count": 1, "forced_count": len(refs), "streams": refs})
    else:
        for show in plex_tv():
            episodes = []
            for season in show["seasons"]:
                for episode in season["episodes"]:
                    path = str(episode["path"]); refs = by_path.get(path, [])
                    if refs:
                        episodes.append({"episode": episode.get("name") or Path(path).stem, "path": path, "forced_count": len(refs), "streams": refs})
            if episodes:
                items.append({"title": str(show["name"]), "root_name": str(show.get("root_name") or ""), "media_count": len(episodes), "forced_count": sum(e["forced_count"] for e in episodes), "episodes": episodes})
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
