from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.v2 import DATA_DIR, probe
from app.v5 import canonical_language, external_subtitles
from app.v11 import (
    connection,
    paged_metadata,
    plex_authorized_file,
    plex_movies,
    plex_tv,
    plex_request,
    plex_external_ids,
    sync_plex,
)
from app.v16 import STATIC_DIR, app
from app.preflight_dispatcher import enqueue_bulk_preflight, register_approval_handler, register_handler

logger = logging.getLogger("videostreamedit")
_listing_cache: dict[str, tuple[float, object]] = {}
from app.subtitle_detector_config import SUBTITLE_DETECTOR_VERSION
from app.subtitle_html import MARKUP_VERSION
from app.plex_secret import decrypt_token, encrypt_token


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
        if re.search(r"[\[\(].{1,80}[\]\)]|♪|♫|\b(?:[A-Z][A-Z .'-]{2,}|signs?|speaks? foreign)\b", payload, re.IGNORECASE):
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
    from app.v51 import damage_kind
    from app.v79 import (
        analyze_sdh,
        calibrate_subtitle_confidence,
        common_detection_languages,
        detect_common_variant,
        normalized_evidence_sample,
        subtitle_quality_issue,
    )

    media = plex_authorized_file(request.path)
    from app.v86 import assert_media_editable
    assert_media_editable(str(media))
    from app.detection_policy import subtitle_assessment, source_stamp, is_final
    initial_stamp = source_stamp(media)
    evaluation_cache = {}
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
        evaluation_cache[('embedded', subtitle_index)] = text
        metrics = _subtitle_metrics(text, duration)
        sdh_label, sdh_confidence, sdh_evidence = analyze_sdh(text)
        allowed = {value.casefold().split("-", 1)[0].split("_", 1)[0] for value in common_detection_languages()}
        detected_language, language_confidence, language_evidence = detect_common_variant(text, allowed) if text else ("", 0.0, "")
        language_confidence = calibrate_subtitle_confidence(language_confidence, int(metrics.get("cues") or 0), len(re.sub(r"\s+", " ", text).strip()), float(metrics.get("coverage") or 0.0))
        quality_issue = subtitle_quality_issue(text, damage_kind(text))
        detected_language, language_confidence, language_evidence, assessment = subtitle_assessment(text, codec, tags.get('language'), '', allowed)
        subtitles.append({"detected_language": detected_language, "language_confidence": language_confidence, "language_evidence": language_evidence, "quality_issue": quality_issue, "evidence_sample": normalized_evidence_sample(text), "source": "embedded", "type_index": subtitle_index, "codec": codec, "language": tags.get("language") or "", "title": tags.get("title") or "", "forced": bool((stream.get("disposition") or {}).get("forced")), "default": bool((stream.get("disposition") or {}).get("default")), "sdh_label": sdh_label, "sdh_confidence": sdh_confidence, "sdh_evidence": sdh_evidence, **metrics})
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
        language_confidence = calibrate_subtitle_confidence(language_confidence, int(metrics.get("cues") or 0), len(re.sub(r"\s+", " ", text).strip()), float(metrics.get("coverage") or 0.0))
        quality_issue = subtitle_quality_issue(text, damage_kind(text))
        subtitles.append({"detected_language": detected_language, "language_confidence": language_confidence, "language_evidence": language_evidence, "quality_issue": quality_issue, "evidence_sample": normalized_evidence_sample(text), "source": "external", "path": str(path), "codec": path.suffix.lstrip(".") or "unknown", "language": item.get("language") or "", "title": item.get("title") or path.name, "forced": bool(item.get("forced")), "default": bool(item.get("default")), "sdh_label": sdh_label, "sdh_confidence": sdh_confidence, "sdh_evidence": sdh_evidence, **metrics})
        evaluation_cache[('external', str(path))] = (text, '')
        detected_language, language_confidence, language_evidence, assessment = subtitle_assessment(text, path.suffix.lstrip('.'), item.get('language'), item.get('region'), allowed)
        subtitles[-1].update(detected_language=detected_language, language_confidence=language_confidence, language_evidence=language_evidence)
    if is_final(str(media)) or source_stamp(media) != initial_stamp:
        raise HTTPException(409, 'Media changed or became Final Version during evaluation; results discarded')
    from app.v79 import inspect_portuguese_language
    inspect_portuguese_language(str(media), text_cache=evaluation_cache)
    text_items = [item for item in subtitles if item["text_available"]]
    for item in subtitles:
        if item.get("quality_issue"):
            item["recommendation"] = "Review subtitle quality first"
        elif item["text_available"]:
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
    # Video-track title edits are media mutations too; Final Version is a
    # hard view-only lock shared by the stream editor and report actions.
    from app.v86 import assert_media_editable
    assert_media_editable(request.path)
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
    from app.matroska_remux import ensure_front_track_headers
    ensure_front_track_headers(path, live=True)
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


def aliases_by_path(paths: set[str] | None = None) -> dict[str, list[str]]:
    with connection() as db:
        query = "SELECT path,alternatives FROM plex_title_aliases"
        args: list[str] = []
        if paths:
            marks = ",".join("?" for _ in paths)
            query += f" WHERE path IN ({marks})"
            args.extend(paths)
        rows = db.execute(query, args).fetchall()
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
        return label or "Import media and apply stream changes"
    if task_type == "subtitle_html_cleanup":
        return "Remove subtitle HTML tags"
    return label or task_type.replace("_", " ").title()


def change_requests_by_path(paths: set[str] | None = None) -> dict[str, list[dict]]:
    with connection() as db:
        exists = db.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='media_change_request'").fetchone()
        if not exists:
            return {}
        query = """SELECT marker.path,queue.id,queue.status,queue.task_type,queue.label,queue.payload_json,marker.requested_at
                 FROM media_change_request marker JOIN task_queue queue ON queue.id=marker.task_id
                WHERE queue.status IN ('pending','running','failed')"""
        args: list[str] = []
        if paths:
            marks = ",".join("?" for _ in paths)
            query += f" AND marker.path IN ({marks})"
            args.extend(paths)
        query += " ORDER BY queue.id"
        rows = db.execute(query, args).fetchall()
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row["path"], []).append({
            "id": row["id"], "status": row["status"], "requested_at": row["requested_at"],
            "summary": queued_change_summary(row["task_type"], row["label"], row["payload_json"]),
        })
    return result


def change_requested_paths() -> set[str]:
    return set(change_requests_by_path())


def report_blocked_paths() -> set[str]:
    """Return media hidden from actionable reports.

    Pending edits and preflight work are hidden immediately. Final Version
    media are also hidden permanently until the user explicitly unfreezes them;
    they remain indexed and browseable outside report views. Failed tasks are
    intentionally not blocked so a finding can be reviewed or retried.
    """
    blocked: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"path", "media_path", "source", "original_path", "file"} and isinstance(item, str) and item:
                    blocked.add(str(Path(item)))
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    with connection() as db:
        rows = db.execute(
            "SELECT marker.path FROM media_change_request marker JOIN task_queue task ON task.id=marker.task_id "
            "WHERE task.status IN ('pending','running')"
        ).fetchall()
        blocked.update(str(row["path"]) for row in rows if row["path"])
        # A preflight can be pending before its child task/marker exists.
        for row in db.execute("SELECT media_path,payload_json FROM preflight_requests WHERE status IN ('pending','running')").fetchall():
            if row["media_path"]:
                blocked.add(str(row["media_path"]))
            try:
                collect(json.loads(row["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        # Cover queue rows created by older callers that did not insert a
        # media_change_request marker yet.
        for row in db.execute("SELECT payload_json FROM task_queue WHERE status IN ('pending','running')").fetchall():
            try:
                collect(json.loads(row["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        # Share the same approval rule with detection; avoid loading the whole
        # catalog just to hide the approved subset. Findings are retained so
        # unfreezing restores eligibility without needing a catalog rescan.
        from app.detection_policy import final_paths
        blocked.update(final_paths(db=db))
    return blocked


def audio_detection_by_path(paths: set[str] | None = None) -> dict[str, dict]:
    if paths is not None and not paths:
        return {}
    from app.detection_policy import final_paths, language_matches
    excluded = final_paths(paths)
    with connection() as db:
        try:
            query = "SELECT d.path,d.metadata_language,d.detected_language,d.confidence,s.language AS current_language,s.region AS current_region FROM audio_language_detection d JOIN media_stream_index s ON s.path=d.path AND s.stream_type='audio' AND s.source='embedded' AND s.type_index=d.type_index WHERE d.mismatch=1"
            args: list[str] = []
            if paths:
                marks = ",".join("?" for _ in paths)
                query += f" AND d.path IN ({marks})"
                args.extend(paths)
            rows = db.execute(query, args).fetchall()
        except Exception:
            return {}
    result = {}
    for row in rows:
        if row['path'] in excluded or language_matches(row['detected_language'], row['current_language'], row['current_region']): continue
        if not language_matches(row['metadata_language'], row['current_language'], row['current_region']): continue
        item = {"confidence": float(row["confidence"]), "metadata_language": str(row["metadata_language"] or ""), "detected_language": str(row["detected_language"] or "")}
        current = result.get(str(row["path"]))
        if current is None or item["confidence"] > current["confidence"]:
            result[str(row["path"])] = item
    return result


def _canonical_detection_value(language: str, region: str = "") -> tuple[str, str]:
    from app.detection_policy import language_key
    return language_key(language, region)



def _detection_row_current(row) -> bool:
    current_language = row["current_language"] if "current_language" in row.keys() else None
    if current_language is None:
        return False
    current = _canonical_detection_value(current_language, row["current_region"] or "")
    stored = _canonical_detection_value(row["metadata_language"] or "", row["metadata_region"] or "")
    return current == stored


def subtitle_detection_by_path(paths: set[str] | None = None) -> dict[str, dict]:
    """Aggregate mismatch and no-confidence subtitle findings per media path, ignoring stale metadata rows."""
    if paths is not None and not paths:
        return {}
    from app.detection_policy import final_paths, language_matches
    excluded = final_paths(paths)
    with connection() as db:
        query = "SELECT d.path,d.source,d.type_index,d.external_path,d.metadata_language,d.metadata_region,d.detected_language,d.confidence,d.analysis_status,s.language AS current_language,s.region AS current_region FROM portuguese_language_detection d LEFT JOIN media_stream_index s ON s.path=d.path AND s.stream_type IN ('subtitle','external') AND s.source=d.source AND s.type_index=d.type_index AND COALESCE(s.external_path,'')=COALESCE(d.external_path,'')"
        args: list[str] = []
        if paths:
            marks = ",".join("?" for _ in paths)
            query += f" WHERE d.path IN ({marks})"
            args.extend(paths)
        rows = db.execute(query, args).fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        if row['path'] in excluded: continue
        if row['analysis_status'] == 'mismatch' and language_matches(row['detected_language'], row['current_language'], row['current_region']): continue
        if not _detection_row_current(row):
            continue
        status = str(row["analysis_status"] or "")
        if status not in {"mismatch", "no_confidence", "unreadable"}:
            continue
        path = str(row["path"])
        confidence = float(row["confidence"] or 0)
        detected = str(row["detected_language"] or "")
        item = result.setdefault(path, {
            "confidence": 0.0,
            "metadata_language": "",
            "metadata_region": "",
            "detected_language": "",
            "no_confidence": False,
        })
        if str(row["analysis_status"] or "") in {"no_confidence", "unreadable"}:
            item["no_confidence"] = True
            continue
        if confidence > float(item["confidence"]):
            item.update({
                "confidence": confidence,
                "metadata_language": str(row["metadata_language"] or ""),
                "metadata_region": str(row["metadata_region"] or ""),
                "detected_language": detected,
            })
    return result


@app.get("/api/v19/movies")
def plex_movies_with_alternatives() -> list[dict]:
    cached = _listing_cache.get("movies")
    if cached and time.monotonic() - cached[0] < 2.0:
        return cached[1]  # type: ignore[return-value]
    # Scope every enrichment query to the movie catalog. The previous version
    # scanned detection/change rows for TV episodes as well, making a simple
    # movie-list refresh compete with long subtitle jobs.
    movies = plex_movies()
    paths = {str(movie["path"]) for movie in movies}
    aliases = aliases_by_path(paths)
    requested = change_requests_by_path(paths)
    audio_detection = audio_detection_by_path(paths)
    detection = subtitle_detection_by_path(paths)
    result = [{**movie, "alternative_titles": aliases.get(movie["path"], []), "change_requested": movie["path"] in requested, "change_requests": requested.get(movie["path"], []), "portuguese_detection_confidence": (detection.get(str(movie["path"])) or {}).get("confidence"), "portuguese_detection_metadata": (detection.get(str(movie["path"])) or {}).get("metadata_language"), "portuguese_detection_region": (detection.get(str(movie["path"])) or {}).get("metadata_region"), "portuguese_detection_language": (detection.get(str(movie["path"])) or {}).get("detected_language"), "portuguese_detection_no_confidence": bool((detection.get(str(movie["path"])) or {}).get("no_confidence")), "audio_detection_confidence": (audio_detection.get(str(movie["path"])) or {}).get("confidence"), "audio_detection_metadata": (audio_detection.get(str(movie["path"])) or {}).get("metadata_language"), "audio_detection_language": (audio_detection.get(str(movie["path"])) or {}).get("detected_language")} for movie in movies]
    _listing_cache["movies"] = (time.monotonic(), result)
    return result



def _tv_summary_payload() -> list[dict]:
    aliases = aliases_by_path()
    with connection() as db:
        rows = db.execute("SELECT library_key,library_name,show_title,count(*) AS episode_count FROM plex_media WHERE kind='episode' GROUP BY library_key,library_name,show_title ORDER BY show_title COLLATE NOCASE").fetchall()
        path_rows = db.execute("SELECT library_key,show_title,path FROM plex_media WHERE kind='episode'").fetchall()
        note_rows = db.execute("SELECT entity_key,note,reviewed,plex_sync_change,final_version FROM media_notes WHERE entity_type='tv'").fetchall()
        busy_paths = {str(row['path']) for row in db.execute("SELECT DISTINCT path FROM index_task_queue WHERE status IN ('pending','running','failed')").fetchall()}
    notes = {str(row['entity_key']): row for row in note_rows}
    paths_by_key: dict[str, list[str]] = {}
    for row in path_rows:
        key = f"{row['library_key']}:{row['show_title'] or 'Unknown show'}"
        paths_by_key.setdefault(key, []).append(str(row['path']))
    scoped_paths = {path for paths in paths_by_key.values() for path in paths}
    detections = subtitle_detection_by_path(scoped_paths)
    audio_detections = audio_detection_by_path(scoped_paths)
    result = []
    for row in rows:
        key = f"{row['library_key']}:{row['show_title'] or 'Unknown show'}"
        paths = paths_by_key.get(key, [])
        top = max((detections[path] for path in paths if path in detections), key=lambda item: item['confidence'], default=None)
        top_audio = max((audio_detections[path] for path in paths if path in audio_detections), key=lambda item: item['confidence'], default=None)
        note = notes.get(key)
        result.append({
            "id": key, "name": row['show_title'] or "Unknown show", "root_name": row['library_name'],
            "episode_count": int(row['episode_count']), "alternative_titles": next((aliases[path] for path in paths if aliases.get(path)), []),
            "seasons": [], "index_busy": any(path in busy_paths for path in paths),
            "note": str(note['note']) if note else "", "reviewed": bool(note['reviewed']) if note else False, "plex_sync_change": bool(note['plex_sync_change']) if note else False,
            "portuguese_detection_confidence": top['confidence'] if top else None, "portuguese_detection_metadata": top['metadata_language'] if top else None, "portuguese_detection_language": top['detected_language'] if top else None, "portuguese_detection_no_confidence": any(bool(detections[path].get('no_confidence')) for path in paths if path in detections),
            "audio_detection_confidence": top_audio['confidence'] if top_audio else None, "audio_detection_metadata": top_audio['metadata_language'] if top_audio else None, "audio_detection_language": top_audio['detected_language'] if top_audio else None,
        })
    return result


@app.get("/api/v19/tv/summary")
def tv_summary_read_model() -> list[dict]:
    cached = _listing_cache.get("tv-summary")
    if cached and time.monotonic() - cached[0] < 2.0:
        return cached[1]  # type: ignore[return-value]
    result = _tv_summary_payload()
    _listing_cache["tv-summary"] = (time.monotonic(), result)
    return result
@app.get("/api/v19/tv")
def plex_tv_with_alternatives(show_id: str | None = None) -> list[dict]:
    shows = plex_tv(show_id)
    # Selected-show browsing is the hot path. Restrict enrichment queries to
    # its episodes instead of scanning every detection, alias, and task row.
    selected_paths = {
        str(episode["path"])
        for show in shows
        for season in show["seasons"]
        for episode in season["episodes"]
    }
    if show_id and selected_paths:
        from app.v79 import prune_missing_external_sidecars
        prune_missing_external_sidecars(selected_paths)
    scoped = selected_paths if show_id else None
    aliases = aliases_by_path(scoped)
    requested = change_requests_by_path(scoped)
    audio_detection = audio_detection_by_path(scoped)
    # Index activity is derived from the live queue, so the filter remains useful
    # while a long-running core/subtitle/preview index is in progress.
    with connection() as db:
        busy_query = "SELECT DISTINCT path FROM index_task_queue WHERE status IN ('pending','running','failed')"
        busy_args: list[str] = []
        if scoped:
            marks = ",".join("?" for _ in selected_paths)
            busy_query += f" AND path IN ({marks})"
            busy_args.extend(scoped)
        busy_paths = {str(row["path"]) for row in db.execute(busy_query, busy_args).fetchall()}
        # Use already indexed audio/subtitle streams only. Listing must not probe
        # episode files, especially for large shows. Video metadata rows are
        # deliberately excluded; external sidecars count as subtitle streams.
        detection = subtitle_detection_by_path(scoped)
        stream_query = (
            "SELECT path, "
            "SUM(CASE WHEN stream_type='audio' THEN 1 ELSE 0 END) AS audio_count, "
            "SUM(CASE WHEN stream_type='subtitle' THEN 1 ELSE 0 END) AS subtitle_count, "
            "SUM(CASE WHEN stream_type='external' THEN 1 ELSE 0 END) AS external_count "
            "FROM media_stream_index WHERE stream_type IN ('audio','subtitle','external')"
        )
        stream_args: list[str] = []
        if scoped:
            marks = ",".join("?" for _ in selected_paths)
            stream_query += f" AND path IN ({marks})"
            stream_args.extend(scoped)
        stream_query += " GROUP BY path"
        stream_counts = {
            str(row["path"]): {
                "audio": int(row["audio_count"] or 0),
                "subtitle": int(row["subtitle_count"] or 0),
                "external": int(row["external_count"] or 0),
            }
            for row in db.execute(stream_query, stream_args).fetchall()
        }
        note_rows = db.execute("SELECT entity_key,final_version FROM media_notes WHERE entity_type='tv'").fetchall()
    notes = {str(row["entity_key"]): row for row in note_rows}
    for show in shows:
        paths = [episode["path"] for season in show["seasons"] for episode in season["episodes"]]
        show["index_busy"] = any(path in busy_paths for path in paths)
        show["portuguese_detection_confidence"] = max((detection[path]["confidence"] for path in paths if path in detection), default=None)
        top_detection = max((detection[path] for path in paths if path in detection), key=lambda item: item["confidence"], default=None)
        show["portuguese_detection_metadata"] = top_detection["metadata_language"] if top_detection else None
        show["portuguese_detection_region"] = top_detection.get("metadata_region") if top_detection else None
        show["portuguese_detection_language"] = top_detection["detected_language"] if top_detection else None
        show["portuguese_detection_no_confidence"] = any(bool(detection[path].get("no_confidence")) for path in paths if path in detection)
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
                episode["portuguese_detection_region"] = episode_detection.get("metadata_region")
                episode["portuguese_detection_language"] = episode_detection.get("detected_language")
                episode["portuguese_detection_no_confidence"] = bool(episode_detection.get("no_confidence"))
                audio_episode = audio_detection.get(episode["path"]) or {}
                episode["audio_detection_confidence"] = audio_episode.get("confidence")
                episode["audio_detection_metadata"] = audio_episode.get("metadata_language")
                episode["audio_detection_language"] = audio_episode.get("detected_language")
                episode_note = notes.get("episode:" + str(episode["path"]))
                show_note = notes.get(str(show.get("id") or ""))
                episode["final_version"] = bool((episode_note and episode_note["final_version"]) or (show_note and show_note["final_version"]))
    return shows


IMAGE_SUBTITLE_CODECS = (
    "dvd_subtitle", "dvb_subtitle", "hdmv_pgs_subtitle", "pgssub", "pgs",
    "vobsub", "xsub", "sup", "idx", "s_hdmv/pgs", "s_vobsub", "s_dvbsub",
)


def _report_episode_label(row) -> str:
    """Keep SQL-backed TV report rows identifiable even when episode titles repeat."""
    season = int(row.get("season_number") or 0)
    episode = int(row.get("episode_number") or 0)
    return f"S{season:02d}E{episode:02d} · {row.get('title') or Path(str(row.get('path') or '')).stem}"


@app.get("/api/v19/reports/image-subtitles")
def image_subtitle_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    blocked_paths = report_blocked_paths()
    placeholders = ",".join("?" for _ in IMAGE_SUBTITLE_CODECS)
    with connection() as db:
        rows = db.execute(
            f"SELECT path,source,type_index,stream_type,external_path,language,region,track_name,codec FROM media_stream_index "
            f"WHERE stream_type IN ('subtitle','external') AND lower(trim(codec)) IN ({placeholders}) "
            "ORDER BY path,type_index,external_path",
            IMAGE_SUBTITLE_CODECS,
        ).fetchall()
    # Image membership is a codec finding, not a language-detection finding.
    rows = [row for row in rows if str(row["path"]) not in blocked_paths]
    active_paths = set()
    with connection() as db:
        active = db.execute("SELECT payload_json FROM preflight_requests WHERE operation_type=? AND status IN ('pending','running')", ("image_subtitle_convert_bulk",)).fetchall()
    for active_row in active:
        try:
            active_payload = json.loads(active_row["payload_json"] or "{}")
            active_paths.update(str(item.get("path") or "") for item in active_payload.get("_bulk_items", []))
        except (TypeError, json.JSONDecodeError):
            continue
    refs_by_path = {}
    for row in rows:
        if str(row["path"]) in active_paths:
            continue
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
                    "episodes": [{"path": str(episode["path"]), "episode": episode.get("name") or Path(episode["path"]).stem,
                                  "media_count": 1, "image_subtitle_count": len(refs_by_path[str(episode["path"])]),
                                  "streams": refs_by_path[str(episode["path"])]}
                                 for season in show["seasons"] for episode in season["episodes"] if str(episode["path"]) in refs_by_path],
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
    blocked_paths = report_blocked_paths()
    with connection() as db:
        # HTML cleanup is valid only for text subtitle codecs.  Older index
        # rows (or a stale codec inspection) may contain markup metadata for a
        # graphical stream, but those must never appear as actionable HTML
        # cleanup entries.  Also hide both the preflight and child task while
        # either is waiting, so the report cannot enqueue a duplicate request.
        text_codecs = ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text")
        placeholders = ",".join("?" for _ in text_codecs)
        rows = db.execute(
            "SELECT subtitle_extended_index.path,source,type_index,external_path,codec FROM subtitle_extended_index "
            "JOIN subtitle_extended_media inspected ON inspected.path=subtitle_extended_index.path "
            "WHERE inspected.markup_version>=? AND markup LIKE ? AND lower(codec) IN (" + placeholders + ") "
            "AND NOT EXISTS (SELECT 1 FROM task_queue t "
            "WHERE t.task_type='subtitle_html_cleanup' AND t.status IN ('pending','running') "
            "AND CAST(t.payload_json AS JSONB)->>'path'=subtitle_extended_index.path) "
            "AND NOT EXISTS (SELECT 1 FROM preflight_requests p "
            "WHERE p.operation_type='subtitle_html_cleanup' AND p.status IN ('pending','running') "
            "AND p.media_path=subtitle_extended_index.path) "
            "ORDER BY path,type_index,external_path",
            (MARKUP_VERSION, "%HTML tags%", *text_codecs),
        ).fetchall()
    rows = [row for row in rows if str(row["path"]) not in blocked_paths]
    refs_by_path = {}
    for row in rows:
        refs_by_path.setdefault(str(row["path"]), []).append({"path": str(row["path"]), "source": row["source"], "type_index": (int(row["type_index"]) if row["type_index"] is not None else -1), "external_path": row["external_path"] or "", "codec": row["codec"] or ""})
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
                items.append({"title": str(show["name"]), "root_name": str(show.get("root_name") or ""), "media_count": media_count, "html_subtitle_count": subtitle_count, "episodes": [{"path": str(ep["path"]), "episode": ep.get("name") or Path(ep["path"]).stem, "media_count": 1, "html_subtitle_count": len(refs_by_path[str(ep["path"])]), "streams": refs_by_path[str(ep["path"])]} for season in show["seasons"] for ep in season["episodes"] if str(ep["path"]) in refs_by_path], "paths": [episode["path"] for season in show["seasons"] for episode in season["episodes"] if refs_by_path.get(str(episode["path"]))], "streams": [ref for season in show["seasons"] for episode in season["episodes"] for ref in refs_by_path.get(str(episode["path"]), [])]})
    items.sort(key=lambda item: item["title"].casefold())
    return {"kind": kind, "items": items, "title_count": len(items), "media_count": sum(item["media_count"] for item in items)}


@app.get("/api/v19/reports/damaged-subtitles")
def damaged_subtitle_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    blocked_paths = report_blocked_paths()
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
    rows = [row for row in rows if str(row["path"]) not in blocked_paths]
    refs_by_path = {}
    for row in rows:
        refs_by_path.setdefault(str(row["path"]), []).append({
            "path": str(row["path"]), "source": row["source"],
            "type_index": (int(row["type_index"]) if row["type_index"] is not None else -1), "external_path": row["external_path"] or "",
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
    from app.subtitle_damage_report import reason_summary
    return {"kind": kind, "items": items, "title_count": len(items), "media_count": sum(item["media_count"] for item in items),
            "damage_summary": reason_summary(items)}


@app.get('/api/v19/reports/damaged-subtitles/examples')
def damaged_subtitle_examples(kind: str, reason: str) -> dict:
    """On-demand evidence, strictly from the current report and complete caches."""
    from app.subtitle_damage_report import EvidenceRanking, report_tracks, reasons, reason_matches
    from app.subtitle_cache import CACHE_FORMAT_VERSION
    from app.v51 import damage_kind
    report = damaged_subtitle_report(kind)
    selected = [t for t in report_tracks(report['items']) if reason in reasons(t['damage'])]
    if not selected:
        return EvidenceRanking(reason).result(0)
    ranking = EvidenceRanking(reason)
    # Small batches bound memory use. No probe/stat/extraction or new jobs.
    for offset in range(0, len(selected), 64):
        batch = selected[offset:offset + 64]
        identities = {(t['path'], t['source'], t['type_index'], t['external_path']): t for t in batch}
        with connection() as db:
            db.execute("SET LOCAL statement_timeout='15s'")
            cached = db.execute('''SELECT c.path,c.source,c.type_index,c.external_path,c.text_content,c.source_encoding
                FROM jsonb_to_recordset(CAST(? AS jsonb)) AS wanted(path text,source text,type_index int,external_path text)
                JOIN subtitle_cache_track c ON (c.path,c.source,c.type_index,c.external_path)=
                    (wanted.path,wanted.source,wanted.type_index,wanted.external_path)
                JOIN subtitle_cache_media m ON m.path=c.path
                WHERE m.format_version=? AND m.expected_tracks=m.cached_tracks
                  AND c.extraction_status IN ('ready','empty')
                  AND NOT EXISTS (SELECT 1 FROM subtitle_cache_pending p WHERE p.path=c.path)
                  AND NOT EXISTS (SELECT 1 FROM subtitle_cache_failure f WHERE f.path=c.path)''',
                (json.dumps(batch), CACHE_FORMAT_VERSION)).fetchall()
        for row in cached:
            track = identities[(row['path'], row['source'], row['type_index'], row['external_path'])]
            current = [reason] if reason_matches(row['text_content'], reason, row['source'], row['source_encoding'], damage_kind) else []
            ranking.add(track, row['text_content'], row['source_encoding'], current)
    return ranking.result(len(selected))


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
    html_items = []
    image_items = []
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
            payload = {"path": path, "type_index": type_index if source != "external" else None, "external_path": external_path or None, "_report_html_verified": True}
            html_items.append(payload)
            continue
        language = str(raw.get("language") or "").strip()
        payload = {"path": path, "source": source, "type_index": type_index, "external_path": external_path or None, "language": language, "region": str(raw.get("region") or "")}
        image_items.append(payload)
    preflight = None
    preflight_ids = []
    if html_items:
        preflight = enqueue_bulk_preflight("subtitle_html_cleanup", html_items, mode="queued", priority=70, deduplicate=True)
        preflight_ids.append(preflight.get("id"))
        queued += len(html_items)
    if image_items:
        image_preflight = enqueue_bulk_preflight("image_subtitle_convert_bulk", image_items, mode="queued", priority=35, deduplicate=True)
        preflight_ids.append(image_preflight.get("id"))
        preflight = preflight or image_preflight
        queued += len(image_items)
    if queued:
        logger.info("task_queue event=report_subtitle_action action=%s queued=%d errors=%d preflight=%s", request.action, queued, len(errors), preflight.get("id") if preflight else None)
    return {"queued": queued, "errors": errors, "preflight_id": preflight.get("id") if preflight else None, "preflight_ids": [value for value in preflight_ids if value]}


@app.get("/api/v19/reports/portuguese-language")
def portuguese_language_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    blocked_paths = report_blocked_paths()
    with connection() as db:
        rows = db.execute(
            "SELECT d.path,d.source,d.type_index,d.external_path,d.metadata_language,d.detected_language,d.confidence,d.metadata_region,d.evidence,d.sdh_label,d.sdh_confidence,d.sdh_evidence,d.evidence_sample,d.analysis_status,d.analysis_reason,d.cue_count,d.text_chars,d.text_coverage,d.markup_count,d.damage,s.language AS current_language,s.region AS current_region "
            "FROM portuguese_language_detection d JOIN plex_media p ON p.path=d.path LEFT JOIN media_stream_index s ON s.path=d.path AND s.stream_type IN ('subtitle','external') AND s.source=d.source AND s.type_index=d.type_index AND COALESCE(s.external_path,'')=COALESCE(d.external_path,'') "
            "WHERE d.detector_version>=? AND d.confidence>=0.60 AND d.analysis_status='mismatch' "
            "AND ((?='movies' AND p.kind='movie') OR (?='tv' AND p.kind='episode')) "
            "AND d.path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running')) ORDER BY d.path",
            (SUBTITLE_DETECTOR_VERSION, kind, kind),
        ).fetchall()
    from app.detection_policy import language_matches
    rows = [row for row in rows if str(row["path"]) not in blocked_paths and _detection_row_current(row) and not language_matches(row['detected_language'], row['current_language'], row['current_region'])]
    by_path = {}
    for row in rows:
        by_path.setdefault(str(row["path"]), []).append({"source": row["source"], "type_index": (int(row["type_index"]) if row["type_index"] is not None else -1), "external_path": row["external_path"], "metadata_language": row["metadata_language"], "metadata_region": row["metadata_region"], "detected_language": row["detected_language"], "confidence": round(float(row["confidence"]) * 100, 1), "evidence": row["evidence"], "evidence_sample": row["evidence_sample"] or "", "analysis_status": row["analysis_status"] or "mismatch", "analysis_reason": row["analysis_reason"] or "", "cue_count": int(row["cue_count"] or 0), "text_chars": int(row["text_chars"] or 0), "text_coverage": round(float(row["text_coverage"] or 0) * 100, 1), "markup_count": int(row["markup_count"] or 0), "damage": row["damage"] or "", "sdh_label": row["sdh_label"] or "", "sdh_confidence": round(float(row["sdh_confidence"] or 0) * 100, 1), "sdh_evidence": row["sdh_evidence"] or ""})
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


class SubtitleRevalidateRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=30000)
    subtitle_indices: list[int] | None = None


@app.get("/api/v19/reports/subtitle-no-confidence")
def subtitle_no_confidence_report(kind: str, status: str | None = None, reason: str | None = None) -> dict:
    """List current low-confidence subtitle findings with optional filters."""
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    blocked_paths = report_blocked_paths()
    allowed_statuses = {"no_confidence", "unreadable"}
    if status and status not in allowed_statuses:
        raise HTTPException(400, "Unsupported subtitle analysis status")
    clauses = ["d.detector_version>=?", "((?='movies' AND p.kind='movie') OR (?='tv' AND p.kind='episode'))", "d.path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running'))"]
    params: list[object] = [SUBTITLE_DETECTOR_VERSION, kind, kind]
    if status:
        clauses.append("d.analysis_status=?")
        params.append(status)
    else:
        clauses.append("d.analysis_status IN ('no_confidence','unreadable')")
    if reason:
        clauses.append("LOWER(COALESCE(d.analysis_reason,'')) LIKE LOWER(?)")
        params.append(f"%{reason.strip()}%")
    with connection() as db:
        rows = db.execute(
            "SELECT d.path,d.source,d.type_index,d.external_path,d.metadata_language,d.metadata_region,d.analysis_status,d.analysis_reason,d.confidence,d.cue_count,d.text_chars,d.text_coverage,d.markup_count,d.damage,d.evidence_sample,p.title,p.show_title,p.kind,s.language AS current_language,s.region AS current_region "
            "FROM portuguese_language_detection d JOIN plex_media p ON p.path=d.path JOIN media_stream_index s ON s.path=d.path AND s.stream_type IN ('subtitle','external') AND s.source=d.source AND s.type_index=d.type_index AND COALESCE(s.external_path,'')=COALESCE(d.external_path,'') WHERE " + " AND ".join(clauses) + " ORDER BY p.title COLLATE NOCASE,d.path,d.type_index",
            params,
        ).fetchall()
    rows = [row for row in rows if str(row["path"]) not in blocked_paths and _detection_row_current(row)]
    items = []
    for row in rows:
        items.append({"path": str(row["path"]), "title": str(row["title"] or Path(str(row["path"])).stem), "show_title": str(row["show_title"] or ""), "source": row["source"], "type_index": (int(row["type_index"]) if row["type_index"] is not None else -1), "external_path": row["external_path"] or "", "metadata_language": row["metadata_language"] or "", "metadata_region": row["metadata_region"] or "", "status": row["analysis_status"], "reason": row["analysis_reason"] or "", "confidence": round(float(row["confidence"] or 0) * 100, 1), "cue_count": int(row["cue_count"] or 0), "text_chars": int(row["text_chars"] or 0), "text_coverage": round(float(row["text_coverage"] or 0) * 100, 1), "markup_count": int(row["markup_count"] or 0), "damage": row["damage"] or "", "evidence_sample": row["evidence_sample"] or ""})
    return {"kind": kind, "status": status, "reason": reason or "", "items": items, "media_count": len({item["path"] for item in items}), "stream_count": len(items)}


@app.post("/api/v19/reports/subtitle-revalidate")
def revalidate_subtitle_report(request: SubtitleRevalidateRequest) -> dict:
    """Queue targeted subtitle inspection for selected media/streams."""
    from app.v80 import enqueue
    queued = 0
    seen = set()
    indices = request.subtitle_indices or "all"
    for raw_path in request.paths:
        path = str(Path(raw_path))
        if not path or path in seen:
            continue
        seen.add(path)
        scope = {"subtitle_indices": indices, "skip_detection": True}
        queued += int(enqueue("subtitles", path, "User revalidated subtitle inspection report", scope) or 0)
    return {"requested": len(seen), "queued": queued}


class LanguageDetectionFixRequest(BaseModel):
    kind: Literal["tv", "movies"]


def preflight_language_fix(payload: dict, fingerprint: dict) -> dict:
    path = str(payload.get("path") or "")
    if not fingerprint.get("exists"):
        return {"decision": "invalid", "reason": "Media file is not accessible", "path": path}
    requested = list(payload.get("tracks") or [])
    approved_tracks = []
    with connection() as db:
        for track in requested:
            try:
                index = int(track.get("type_index", -1))
            except (TypeError, ValueError):
                continue
            row = db.execute("SELECT d.detected_language,d.confidence,s.language,s.region FROM portuguese_language_detection d LEFT JOIN media_stream_index s ON s.path=d.path AND s.stream_type IN ('subtitle','external') AND s.source='embedded' AND s.type_index=d.type_index WHERE d.path=? AND d.source='embedded' AND d.type_index=?", (path, index)).fetchone()
            pair = _detected_language_pair(row["detected_language"] if row else "") if row else None
            if not row or not pair or float(row["confidence"] or 0) < 0.80:
                continue
            if str(row["language"] or "").casefold() == pair[0].casefold() and str(row["region"] or "").casefold() == pair[1].casefold():
                continue
            approved_tracks.append({"codec_type": "subtitle", "type_index": index, "language": pair[0], "region": pair[1]})
    if not approved_tracks:
        return {"decision": "skipped", "reason": "No high-confidence language mismatch remains", "path": path}
    return {"decision": "approved", "path": path, "tracks": approved_tracks, "fingerprint": fingerprint}

def approve_language_fix_bulk(payload: dict, result: dict) -> dict:
    from app import v65 as tasks
    child_ids = []
    for item in payload.get("_bulk_items", []):
        plan = item.get("_preflight_result") or {}
        tracks = plan.get("tracks") or item.get("tracks") or []
        if not tracks:
            continue
        edit = {"path": str(item.get("path") or ""), "tracks": tracks}
        child = tasks.enqueue("media_edit", {"edit": edit, "reindex_indexes": ["core", "subtitles"]}, "Fix detected subtitle languages (above 80%)", deduplicate=True)
        if child and child.get("id") is not None:
            child_ids.append(int(child["id"]))
    return {"task_ids": child_ids, "queued": len(child_ids), "task_type": "media_edit"}

def _detected_language_pair(value: str) -> tuple[str, str] | None:
    key = str(value or "").strip().casefold().replace("_", "-")
    if key in {"pt-br", "pob", "por-br"}: return "pt", "BR"
    if key in {"pt-pt", "pt", "por-pt"}: return "pt", "PT"
    if key in {"en", "eng"}: return "en", ""
    return None


register_handler("language_detection_fix_bulk", preflight_language_fix)
register_approval_handler("language_detection_fix_bulk", approve_language_fix_bulk)

@app.post("/api/v19/reports/portuguese-language/fix")
def fix_portuguese_language_report(request: LanguageDetectionFixRequest) -> dict:
    with connection() as db:
        rows = db.execute("""SELECT d.path,d.type_index,d.detected_language,d.confidence,p.kind
            FROM portuguese_language_detection d JOIN plex_media p ON p.path=d.path
            WHERE d.confidence>=0.80 AND d.source='embedded'
              AND ((?='movies' AND p.kind='movie') OR (?='tv' AND p.kind='episode'))
            ORDER BY d.path,d.type_index""", (request.kind, request.kind)).fetchall()
    grouped: dict[str, list[dict]] = {}
    skipped = 0
    for row in rows:
        if not _detected_language_pair(row["detected_language"]):
            skipped += 1
            continue
        grouped.setdefault(str(row["path"]), []).append({"type_index": (int(row["type_index"]) if row["type_index"] is not None else -1), "detected_language": str(row["detected_language"] or "")})
    items = [{"path": path, "tracks": tracks} for path, tracks in grouped.items()]
    preflight = enqueue_bulk_preflight("language_detection_fix_bulk", items, mode="queued", priority=60, deduplicate=True) if items else None
    queued_streams = sum(len(item["tracks"]) for item in items)
    logger.info("preflight event=language_detection_fix kind=%s media=%d streams=%d skipped=%d id=%s", request.kind, len(items), queued_streams, skipped, preflight.get("id") if preflight else None)
    return {"queued_media": len(items), "queued_streams": queued_streams, "skipped": skipped, "task_ids": [], "preflight_id": preflight.get("id") if preflight else None}



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
    blocked_paths = report_blocked_paths()
    allowed = set(_duplicate_language_values(stream_type))
    with connection() as db:
        rows = db.execute("SELECT path,source,type_index,external_path,codec,language,region,track_name FROM media_stream_index WHERE stream_type=? AND path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running')) ORDER BY path,type_index", (stream_type,)).fetchall()
        rows = [row for row in rows if str(row["path"]) not in blocked_paths]
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
    language_totals = {}
    for item in items:
        media_items = item.get("episodes") or [item]
        for media in media_items:
            for duplicate in media.get("duplicates", []):
                bucket = language_totals.setdefault(duplicate["language"], {"language": duplicate["language"], "media_count": 0, "stream_count": 0})
                bucket["media_count"] += 1
                bucket["stream_count"] += int(duplicate.get("count", 0))
    return {"kind": kind, "stream_type": stream_type, "languages": sorted(allowed), "language_counts": sorted(language_totals.values(), key=lambda value: value["language"].casefold()), "items": items, "title_count": len(items), "media_count": sum(item.get("media_count",1) for item in items)}

@app.get("/api/v19/reports/forced-streams")
def forced_stream_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    blocked_paths = report_blocked_paths()
    with connection() as db:
        setting = db.execute("SELECT value FROM language_detection_settings WHERE key='forced_report_excluded_track_names'").fetchone()
        rows = db.execute("""SELECT path,stream_type,type_index,external_path,codec,language,region,track_name
            FROM media_stream_index WHERE is_forced=1 AND path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running')) ORDER BY path,type_index""").fetchall()
    rows = [row for row in rows if str(row["path"]) not in blocked_paths]
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



@app.get("/api/v19/reports/english-only")
def english_only_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}: raise HTTPException(400, "Kind must be tv or movies")
    blocked=report_blocked_paths()
    with connection() as db:
        rows=db.execute("SELECT i.path,i.stream_type,i.language,p.title,p.show_title,p.season_number,p.episode_number FROM media_stream_index i JOIN plex_media p ON p.path=i.path WHERE ((?='movies' AND p.kind='movie') OR (?='tv' AND p.kind='episode')) AND i.stream_type IN ('audio','subtitle','external') AND i.path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running')) ORDER BY p.show_title,p.season_number,p.episode_number,p.title,i.type_index",(kind,kind)).fetchall()
    def en(v):
        c=str(v or '').strip().casefold().replace('_','-')
        return c in {'en','eng','english'} or c.startswith('en-')
    groups={}
    for row in rows:
        if str(row['path']) in blocked: continue
        groups.setdefault(str(row['path']),[]).append(dict(row))
    matches=[]
    for path,streams in groups.items():
        audio=[x for x in streams if x['stream_type']=='audio']; subs=[x for x in streams if x['stream_type'] in {'subtitle','external'}]
        mode='audio_only_english' if audio and not subs and all(en(x.get('language')) for x in audio) else ('subtitle_only_english' if subs and not audio and all(en(x.get('language')) for x in subs) else '')
        if mode:
            r=streams[0]; matches.append({'path':path,'title':r.get('title') or Path(path).stem,'show_title':r.get('show_title') or 'Unknown show','season_number':r.get('season_number'),'episode_number':r.get('episode_number'),'mode':mode,'audio_count':len(audio),'subtitle_count':len(subs),'streams':streams})
    if kind=='movies':
        items=[{'title':Path(str(x['title'])).stem,'path':x['path'],'media_count':1,'mode':x['mode'],'audio_count':x['audio_count'],'subtitle_count':x['subtitle_count'],'streams':x['streams']} for x in matches]
    else:
        grouped={}
        for x in matches: grouped.setdefault(x['show_title'],[]).append({'episode':_report_episode_label(x),'path':x['path'],'mode':x['mode'],'audio_count':x['audio_count'],'subtitle_count':x['subtitle_count'],'streams':x['streams']})
        items=[{'title':k,'media_count':len(v),'episodes':sorted(v,key=lambda e:str(e['episode']).casefold())} for k,v in grouped.items()]
    items.sort(key=lambda x:str(x['title']).casefold())
    return {'kind':kind,'items':items,'title_count':len(items),'media_count':sum(x.get('media_count',1) for x in items)}

@app.get("/api/v19/reports/audio-only")
def audio_only_report(kind: str) -> dict:
    """List indexed media with audio streams and no embedded or external subtitles."""
    if kind not in {"tv", "movies"}: raise HTTPException(400, "Kind must be tv or movies")
    blocked_paths=report_blocked_paths()
    with connection() as db:
        rows=db.execute("""SELECT i.path,i.stream_type,i.type_index,i.external_path,i.language,i.region,i.track_name,i.codec,
                                  p.title,p.show_title,p.season_number,p.episode_number,p.kind
            FROM media_stream_index i JOIN plex_media p ON p.path=i.path
            WHERE ((?='movies' AND p.kind='movie') OR (?='tv' AND p.kind='episode'))
              AND i.stream_type IN ('audio','subtitle','external')
              AND i.path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running'))
            ORDER BY p.show_title,p.season_number,p.episode_number,p.title,i.stream_type,i.type_index""",(kind,kind)).fetchall()
    by_path={}
    media={}
    for row in rows:
        path=str(row["path"])
        if path in blocked_paths: continue
        by_path.setdefault(path,[]).append(dict(row)); media[path]=dict(row)
    eligible={path:v for path,v in by_path.items() if any(x.get("stream_type")=="audio" for x in v) and not any(x.get("stream_type") in {"subtitle","external"} for x in v)}
    if kind=="movies":
        items=[]
        for path,streams in eligible.items():
            row=media[path]
            items.append({"title":Path(str(row.get("title") or path)).stem,"path":path,"root_name":row.get("library_name") or "","media_count":1,"audio_count":sum(x.get("stream_type")=="audio" for x in streams),"streams":streams})
    else:
        grouped={}
        for path,streams in eligible.items():
            row=media[path]; show=str(row.get("show_title") or "Unknown show")
            grouped.setdefault(show,[]).append({"episode":_report_episode_label(row),"path":path,"audio_count":sum(x.get("stream_type")=="audio" for x in streams),"streams":streams})
        items=[{"title":show,"root_name":"","media_count":len(eps),"audio_count":sum(e["audio_count"] for e in eps),"episodes":sorted(eps,key=lambda e:str(e["episode"]).casefold())} for show,eps in grouped.items()]
    items.sort(key=lambda x:x["title"].casefold())
    return {"kind":kind,"items":items,"title_count":len(items),"media_count":sum(x.get("media_count",1) for x in items),"audio_count":sum(x.get("audio_count",0) for x in items)}


@app.get("/api/v19/reports/external-only")
def external_only_report(kind: str) -> dict:
    """List media with external subtitles, including media with embedded subtitles."""
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    blocked_paths = report_blocked_paths()
    with connection() as db:
        rows = db.execute("""SELECT i.path,i.stream_type,i.type_index,i.external_path,i.language,i.region,i.track_name,i.codec,
                                  p.title,p.show_title,p.season_number,p.episode_number,p.kind
            FROM media_stream_index i JOIN plex_media p ON p.path=i.path
            WHERE ((?='movies' AND p.kind='movie') OR (?='tv' AND p.kind='episode'))
              AND i.stream_type IN ('audio','subtitle','external')
              AND i.path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running'))
            ORDER BY p.show_title,p.season_number,p.episode_number,p.title,i.stream_type,i.type_index""", (kind, kind)).fetchall()
    by_path = {}
    media = {}
    for row in rows:
        path = str(row["path"])
        if path in blocked_paths:
            continue
        by_path.setdefault(path, []).append(dict(row))
        media[path] = dict(row)
    eligible = {
        path: streams for path, streams in by_path.items()
        if any(x.get("stream_type") == "external" for x in streams)
    }
    if kind == "movies":
        items = []
        for path, streams in eligible.items():
            row = media[path]
            items.append({"title": Path(str(row.get("title") or path)).stem, "path": path,
                          "root_name": row.get("library_name") or "", "media_count": 1,
                          "external_count": sum(x.get("stream_type") == "external" for x in streams),
                          "streams": streams})
    else:
        grouped = {}
        for path, streams in eligible.items():
            row = media[path]
            show = str(row.get("show_title") or "Unknown show")
            grouped.setdefault(show, []).append({
                "episode": _report_episode_label(row), "path": path,
                "external_count": sum(x.get("stream_type") == "external" for x in streams),
                "streams": streams,
            })
        items = [{"title": show, "root_name": "", "media_count": len(eps),
                  "external_count": sum(e["external_count"] for e in eps),
                  "episodes": sorted(eps, key=lambda e: str(e["episode"]).casefold())}
                 for show, eps in grouped.items()]
    items.sort(key=lambda x: str(x["title"]).casefold())
    return {"kind": kind, "items": items, "title_count": len(items),
            "media_count": sum(x.get("media_count", 1) for x in items),
            "external_count": sum(x.get("external_count", 0) for x in items)}


@app.get("/api/v19/reports/video-titles")
def video_title_report(kind: Literal["movies", "tv"]) -> dict:
    blocked = report_blocked_paths()
    with connection() as db:
        rows = db.execute("""SELECT p.path,p.title,p.show_title,p.season_number,p.episode_number,v.title AS video_title
            FROM media_video_title v JOIN plex_media p ON p.path=v.path
            WHERE p.kind=? AND trim(coalesce(v.title,''))<>''
            AND NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE q.path=p.path AND q.status IN ('pending','running'))
            ORDER BY p.show_title,p.season_number,p.episode_number,p.title""",
            ("movie" if kind == "movies" else "episode",)).fetchall()
    media = [dict(row) for row in rows if row["path"] not in blocked]
    if kind == "movies":
        items = [{**row, "media_count": 1} for row in media]
    else:
        groups = {}
        for row in media:
            groups.setdefault(row["show_title"] or "Unknown show", []).append({**row, "episode": _report_episode_label(row)})
        items = [{"title": title, "episodes": episodes, "media_count": len(episodes)} for title, episodes in groups.items()]
    return {"kind": kind, "items": items, "title_count": len(items), "media_count": len(media)}


class VideoTitleReportRemoval(BaseModel):
    kind: Literal["movies", "tv"]
    paths: list[str] = Field(min_length=1, max_length=30000)


@app.post("/api/v19/reports/video-titles/remove")
def queue_video_title_removal(request: VideoTitleReportRemoval) -> dict:
    from app.preflight_dispatcher import enqueue_bulk_preflight
    result = video_title_report(request.kind)
    eligible = {item["path"] for group in result["items"] for item in (group.get("episodes") or [group])}
    paths = [path for path in dict.fromkeys(request.paths) if path in eligible]
    if not paths:
        return {"queued": 0, "skipped": len(request.paths)}
    pending = enqueue_bulk_preflight("video_title_cleanup", [{"path": path} for path in paths], mode="queued", priority=80, deduplicate=True)
    return {"queued": len(paths), "preflight_id": pending["id"]}


def validate_video_title_cleanup(payload: dict, fingerprint: dict) -> dict:
    from app.v86 import assert_media_editable
    path = plex_authorized_file(payload["path"])
    assert_media_editable(str(path))
    if path.suffix.lower() not in {".mkv", ".mka", ".mks", ".mk3d"}:
        return {"decision": "invalid", "reason": "Video-title removal requires Matroska; conversion is not automatic", "path": str(path)}
    has_title = any(str((stream.get("tags") or {}).get("title") or "").strip() for stream in probe(path).get("streams", []) if stream.get("codec_type") == "video")
    return {"decision": "approved" if has_title else "skipped", "reason": "Video stream titles found" if has_title else "Video stream titles already empty", "path": str(path)}


def approve_video_title_cleanup(payload: dict, result: dict) -> dict:
    from app.v65 import enqueue
    ids = []
    for item in payload.get("_bulk_items") or []:
        edit = {"path": item["path"], "clear_video_titles": True,
                "default_audio": "__preserve__", "forced_audio": "__preserve__",
                "default_subtitle": "__preserve__", "forced_subtitle": "__preserve__"}
        job = enqueue("media_edit", {"edit": edit}, f"Remove video stream titles · {Path(item['path']).name}", deduplicate=True)
        ids.append(job["id"])
    return {"task_ids": ids, "queued": len(ids), "task_type": "media_edit"}


from app.preflight_dispatcher import register_handler, register_approval_handler
register_handler("video_title_cleanup", validate_video_title_cleanup)
register_approval_handler("video_title_cleanup", approve_video_title_cleanup)


@app.get("/api/v19/reports/availability")
def report_availability() -> dict:
    """Summarize findings, rechecking only changed Matroska warnings."""
    from app.matroska_layout import active_remux_paths, revalidate_warning_rows
    keys = ("image", "damaged", "html", "language", "confidence", "duplicate_audio",
            "duplicate_subtitle", "uncommon", "forced", "audio_only", "english_only", "external_only", "video_titles", "matroska_layout")
    found = {key: {"tv": set(), "movies": set()} for key in keys}
    blocked = report_blocked_paths()
    active_layout_remux = active_remux_paths()

    def mark(key, path, kind):
        if path not in blocked and kind in {"movie", "episode"}:
            found[key]["movies" if kind == "movie" else "tv"].add(path)

    common = {value.casefold().replace("_", "-").split("-", 1)[0] for value in _configured_common_languages()}
    allowed = {kind: set(_duplicate_language_values(kind)) for kind in ("audio", "subtitle")}
    with connection() as db:
        pending_rows = db.execute("SELECT path,job FROM index_task_queue WHERE status IN ('pending','running')").fetchall()
        pending = {str(row["path"]) for row in pending_rows}
        pending_subtitles = {str(row["path"]) for row in pending_rows if row["job"] == "subtitles"}
        layout_rows = db.execute(
            "SELECT c.path,c.size,c.modified_ns,p.kind FROM matroska_layout_check c JOIN plex_media p ON p.path=c.path "
            "WHERE c.status='tracks_after_cluster'"
        ).fetchall()
        layout_rows = [row for row in layout_rows if str(row["path"]) not in blocked
                       and str(row["path"]) not in pending and str(row["path"]) not in active_layout_remux]
        current_layout_warnings = revalidate_warning_rows(layout_rows)
        for row in layout_rows:
            if str(row["path"]) in current_layout_warnings and str(row["path"]) not in pending and str(row["path"]) not in active_layout_remux:
                mark("matroska_layout", str(row["path"]), row["kind"])
        for row in db.execute("SELECT v.path,p.kind FROM media_video_title v JOIN plex_media p ON p.path=v.path WHERE trim(coalesce(v.title,''))<>''").fetchall():
            if row["path"] not in pending:
                mark("video_titles", row["path"], row["kind"])
        excluded_row = db.execute("SELECT value FROM language_detection_settings WHERE key='forced_report_excluded_track_names'").fetchone()
        try:
            excluded = {str(value).strip().casefold() for value in json.loads(excluded_row["value"] or "[]")} if excluded_row else set()
        except (TypeError, ValueError):
            excluded = set()
        marks = ",".join("?" for _ in IMAGE_SUBTITLE_CODECS)
        for row in db.execute(
            "SELECT DISTINCT i.path,p.kind FROM media_stream_index i JOIN plex_media p ON p.path=i.path "
            "WHERE i.stream_type IN ('subtitle','external') AND lower(trim(i.codec)) IN (" + marks + ")",
            IMAGE_SUBTITLE_CODECS,
        ).fetchall():
            mark("image", str(row["path"]), row["kind"])
        for row in db.execute(
            "SELECT i.path,p.kind,i.track_name FROM media_stream_index i JOIN plex_media p ON p.path=i.path WHERE i.is_forced=1"
        ).fetchall():
            path = str(row["path"])
            if path not in pending and str(row["track_name"] or "").strip().casefold() not in excluded:
                mark("forced", path, row["kind"])

        # Read only grouped language counts, not all stream details/filename tags.
        rows = db.execute(
            "SELECT i.path,p.kind,i.stream_type,i.language,count(*) AS stream_count "
            "FROM media_stream_index i JOIN plex_media p ON p.path=i.path "
            "WHERE p.kind IN ('movie','episode') AND i.stream_type IN ('audio','subtitle','external') "
            "GROUP BY i.path,p.kind,i.stream_type,i.language"
        ).fetchall()
        media = {}
        for row in rows:
            path = str(row["path"])
            if path in blocked or path in pending:
                continue
            entry = media.setdefault(path, {"kind": row["kind"], "audio": 0, "subtitle": 0, "external": 0,
                "non_en_audio": 0, "non_en_subtitle": 0, "uncommon_audio": set(), "uncommon_subtitle": set(),
                "duplicates": {"audio": {}, "subtitle": {}}})
            stream_type = str(row["stream_type"])
            language = str(row["language"] or "").strip()
            normalized = language.casefold().replace("_", "-")
            count = int(row["stream_count"])
            entry[stream_type] += count
            base_type = "subtitle" if stream_type == "external" else stream_type
            if normalized not in {"en", "eng", "english"} and not normalized.startswith("en-"):
                entry["non_en_" + base_type] += count
            if normalized and normalized != "und" and normalized.split("-", 1)[0] not in common:
                entry["uncommon_" + base_type].add(language.casefold())
            if stream_type in allowed:
                canonical = canonical_language(language)
                if canonical and (not allowed[stream_type] or canonical in allowed[stream_type]):
                    counts = entry["duplicates"][stream_type]
                    counts[canonical] = counts.get(canonical, 0) + count

        suppressions = {(str(row["path"]), str(row["stream_type"]), canonical_language(str(row["language"]))): str(row["fingerprint"])
                        for row in db.execute("SELECT path,stream_type,language,fingerprint FROM duplicate_language_report_suppressions").fetchall()}
        # Fetch dismissed groups in one query, avoiding a round-trip for every dismissal.
        suppressed_streams = {}
        if suppressions:
            for row in db.execute(
                "SELECT i.path,i.stream_type,i.source,i.type_index,i.external_path,i.codec,i.language,i.region,i.track_name "
                "FROM media_stream_index i WHERE EXISTS (SELECT 1 FROM duplicate_language_report_suppressions s "
                "WHERE s.path=i.path AND s.stream_type=i.stream_type)"
            ).fetchall():
                key = (str(row["path"]), str(row["stream_type"]), canonical_language(str(row["language"] or "")))
                suppressed_streams.setdefault(key, []).append(dict(row))
        for path, entry in media.items():
            kind = entry["kind"]
            subs = entry["subtitle"] + entry["external"]
            if entry["audio"] and not subs:
                mark("audio_only", path, kind)
            if (entry["audio"] and not subs and not entry["non_en_audio"]) or (subs and not entry["audio"] and not entry["non_en_subtitle"]):
                mark("english_only", path, kind)
            if entry["external"]:
                mark("external_only", path, kind)
            if len(entry["uncommon_audio"]) > 2 or len(entry["uncommon_subtitle"]) > 2:
                mark("uncommon", path, kind)
            for stream_type, counts in entry["duplicates"].items():
                for language, count in counts.items():
                    if count < 2:
                        continue
                    fingerprint = suppressions.get((path, stream_type, language))
                    if fingerprint:
                        streams = suppressed_streams.get((path, stream_type, language), [])
                        if _duplicate_group_fingerprint(streams) == fingerprint:
                            continue
                    mark("duplicate_" + stream_type, path, kind)
                    break

        codecs = ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text")
        marks = ",".join("?" for _ in codecs)
        for row in db.execute(
            "SELECT DISTINCT s.path,p.kind,s.markup,s.damage,inspected.markup_version FROM subtitle_extended_index s "
            "JOIN plex_media p ON p.path=s.path "
            "LEFT JOIN subtitle_extended_media inspected ON inspected.path=s.path "
            "WHERE lower(s.codec) IN (" + marks + ") "
            "AND (s.markup LIKE ? OR (s.damage IS NOT NULL AND s.damage!='' AND s.damage!='None'))",
            (*codecs, "%HTML tags%"),
        ).fetchall():
            path = str(row["path"])
            if int(row["markup_version"] or 0) >= MARKUP_VERSION and "HTML tags" in str(row["markup"] or ""):
                mark("html", path, row["kind"])
            if row["damage"] and row["damage"] != "None" and path not in pending_subtitles:
                mark("damaged", path, row["kind"])
        for row in db.execute(
            "SELECT DISTINCT d.path,p.kind,d.analysis_status,d.detected_language,d.metadata_language,d.metadata_region,s.language AS current_language,s.region AS current_region FROM portuguese_language_detection d "
            "JOIN plex_media p ON p.path=d.path JOIN media_stream_index s ON s.path=d.path AND s.stream_type IN ('subtitle','external') AND s.source=d.source AND s.type_index=d.type_index AND COALESCE(s.external_path,'')=COALESCE(d.external_path,'') WHERE d.detector_version>=? AND "
            "((d.analysis_status='mismatch' AND d.confidence>=0.60) OR d.analysis_status IN ('no_confidence','unreadable'))",
            (SUBTITLE_DETECTOR_VERSION,),
        ).fetchall():
            path = str(row["path"])
            from app.detection_policy import language_matches
            if not _detection_row_current(row):
                continue
            if row['analysis_status'] == 'mismatch' and language_matches(row['detected_language'], row['current_language'], row['current_region']):
                continue
            if path not in pending:
                mark("language" if row["analysis_status"] == "mismatch" else "confidence", path, row["kind"])
    counts = {key: {kind: len(paths) for kind, paths in kinds.items()} for key, kinds in found.items()}
    return {"reports": {key: {kind: bool(count) for kind, count in kinds.items()} for key, kinds in counts.items()},
            "counts": counts}




class OpenSubtitlesSettings(BaseModel):
    enabled: bool = False
    api_key: str = ""
    username: str = ""
    password: str = ""
    languages: list[str] = Field(default_factory=lambda: ["pt", "en"])

class OpenSubtitlesSearchRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    query: str = ""
    languages: list[str] | None = None

class OpenSubtitlesDownloadRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    file_id: int = Field(gt=0)
    language: str = ""
    file_name: str = ""

OPEN_SUBTITLES_API = "https://api.opensubtitles.com/api/v1"
OPEN_SUBTITLES_STAGE = DATA_DIR / "opensubtitles-staging"

def _os_settings() -> dict:
    with connection() as db:
        rows=db.execute("SELECT key,value FROM application_settings WHERE key LIKE 'opensubtitles_%'").fetchall()
    values={str(row["key"]):str(row["value"] or "") for row in rows}
    try: languages=json.loads(values.get("opensubtitles_languages", '["pt","en"]'))
    except (TypeError,ValueError,json.JSONDecodeError): languages=["pt","en"]
    enabled = str(values.get("opensubtitles_enabled", "")).strip().lower() in {"1", "true", "yes", "on"}
    return {"enabled":enabled,"api_key":decrypt_token(values.get("opensubtitles_api_key","")),"username":decrypt_token(values.get("opensubtitles_username","")),"password":decrypt_token(values.get("opensubtitles_password","")),"languages":[str(x).strip() for x in languages if str(x).strip()]}

def _os_request(method: str, path: str, settings: dict, payload: dict|None=None, token: str="") -> dict:
    body=json.dumps(payload).encode() if payload is not None else None
    headers={"Accept":"application/json","Content-Type":"application/json","Api-Key":settings["api_key"],"User-Agent":"VideoStreamEdit/1.0"}
    if token: headers["Authorization"]="Bearer "+token
    request=urllib.request.Request(OPEN_SUBTITLES_API+path,data=body,headers=headers,method=method)
    try:
        with urllib.request.urlopen(request,timeout=30) as response: return json.loads(response.read().decode("utf-8","replace"))
    except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,json.JSONDecodeError) as exc:
        detail=getattr(exc,"reason",str(exc)); raise HTTPException(502,f"OpenSubtitles request failed: {detail}") from exc

@app.get("/api/v19/opensubtitles/settings")
def get_opensubtitles_settings() -> dict:
    settings=_os_settings()
    return {"enabled":settings["enabled"],"configured":bool(settings["api_key"] and settings["username"] and settings["password"]),"api_key":("••••" if settings["api_key"] else ""),"username":settings["username"],"languages":settings["languages"]}

@app.put("/api/v19/opensubtitles/settings")
def save_opensubtitles_settings(request: OpenSubtitlesSettings) -> dict:
    languages=[]
    for value in request.languages:
        value=str(value).strip().lower().replace("_","-")
        if value and value not in languages: languages.append(value)
    if not languages: raise HTTPException(400,"At least one searchable language is required")
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO application_settings(key,value) VALUES('opensubtitles_enabled',?)", ("1" if request.enabled else "0",))
        for key,value in (("opensubtitles_api_key",request.api_key.strip()),("opensubtitles_username",request.username.strip()),("opensubtitles_password",request.password)):
            if value: db.execute("INSERT OR REPLACE INTO application_settings(key,value) VALUES(?,?)",(key,encrypt_token(value)))
        db.execute("INSERT OR REPLACE INTO application_settings(key,value) VALUES('opensubtitles_languages',?)",(json.dumps(languages,ensure_ascii=False),))
    return get_opensubtitles_settings()

@app.get("/api/v19/opensubtitles/context")
def opensubtitles_context(path: str) -> dict:
    media_path = str(Path(path))
    with connection() as db:
        row = db.execute("SELECT kind,title,show_title,season_number,episode_number,rating_key,plex_imdb_id,plex_tmdb_id,plex_year FROM plex_media WHERE path=?", (media_path,)).fetchone()
    if row and not (row["plex_imdb_id"] or row["plex_tmdb_id"]) and row["rating_key"]:
        try:
            metadata = plex_request("/library/metadata/" + urllib.parse.quote(str(row["rating_key"]))).get("MediaContainer", {}).get("Metadata", [])
            if metadata:
                imdb_id, tmdb_id, year = plex_external_ids(metadata[0])
                if imdb_id or tmdb_id:
                    with connection() as update_db:
                        update_db.execute("UPDATE plex_media SET plex_imdb_id=?,plex_tmdb_id=?,plex_year=? WHERE path=?", (imdb_id, tmdb_id, year, media_path))
                    row = dict(row)
                    row.update(plex_imdb_id=imdb_id, plex_tmdb_id=tmdb_id, plex_year=year)
        except Exception as exc:
            logger.info("opensubtitles event=plex_identifier_backfill_failed path=%s error=%s", media_path.replace("\n", " "), str(exc).replace("\n", " ")[-200:])
    if not row:
        stem = Path(media_path).stem
        return {"kind":"movie","query":stem,"title":stem,"show_title":"","season_number":None,"episode_number":None,"imdb_id":"","tmdb_id":"","year":None,"match_source":"title"}
    kind = str(row["kind"] or "movie")
    title = str(row["title"] or Path(media_path).stem)
    if kind == "episode":
        show = str(row["show_title"] or title)
        season = int(row["season_number"] or 0)
        episode = int(row["episode_number"] or 0)
        query = f"{show} S{season:02d}E{episode:02d}"
        return {"kind":"episode","query":query,"title":title,"show_title":show,"season_number":season,"episode_number":episode,"imdb_id":str(row["plex_imdb_id"] or ""),"tmdb_id":str(row["plex_tmdb_id"] or ""),"year":int(row["plex_year"] or 0) or None,"match_source":"imdb" if row["plex_imdb_id"] else ("tmdb" if row["plex_tmdb_id"] else "title")}
    return {"kind":"movie","query":Path(media_path).stem,"title":title,"show_title":"","season_number":None,"episode_number":None,"imdb_id":str(row["plex_imdb_id"] or ""),"tmdb_id":str(row["plex_tmdb_id"] or ""),"year":int(row["plex_year"] or 0) or None,"match_source":"imdb" if row["plex_imdb_id"] else ("tmdb" if row["plex_tmdb_id"] else "title")}

def _opensubtitles_quota(settings: dict) -> dict:
    if not settings.get("enabled"):
        return {"available":False,"reason":"disabled"}
    if not settings.get("api_key") or not settings.get("username") or not settings.get("password"):
        return {"available":False,"reason":"not_configured"}
    try:
        login=_os_request("POST","/login",settings,{"username":settings["username"],"password":settings["password"]})
        token=str(login.get("token") or "")
        if not token: return {"available":False,"reason":"login_failed"}
        info=_os_request("GET","/infos/user",settings,token=token)
        data=info.get("data") if isinstance(info,dict) else {}
        if not isinstance(data,dict): data=info if isinstance(info,dict) else {}
        user=data.get("user") if isinstance(data.get("user"),dict) else data
        def number(*keys):
            for key in keys:
                value=user.get(key)
                if value is not None:
                    try: return int(value)
                    except (TypeError,ValueError): pass
            return None
        used=number("downloads_count","downloads_used","download_count")
        limit=number("downloads_limit","download_limit","daily_download_limit")
        remaining=number("downloads_remaining","remaining_downloads")
        if remaining is None and used is not None and limit is not None: remaining=max(0,limit-used)
        reset=user.get("reset_time") or user.get("downloads_reset_time") or user.get("next_reset")
        return {"available":True,"used":used,"limit":limit,"remaining":remaining,"reset_at":str(reset or "")}
    except HTTPException:
        return {"available":False,"reason":"quota_unavailable"}

@app.get("/api/v19/opensubtitles/quota")
def opensubtitles_quota() -> dict:
    return _opensubtitles_quota(_os_settings())

@app.get("/api/v19/opensubtitles/search")
def search_opensubtitles(path: str, query: str = "", languages: str = "") -> dict:
    settings=_os_settings()
    if not settings["enabled"]: raise HTTPException(409,"Enable OpenSubtitles in Setup first")
    if not settings["api_key"]: raise HTTPException(409,"Configure an OpenSubtitles API key in Setup first")
    media=Path(path); search=str(query).strip() or media.stem
    selected=[x.strip().lower() for x in languages.split(",") if x.strip()] or settings["languages"]
    with connection() as db:
        context = db.execute(
            "SELECT kind,title,show_title,season_number,episode_number,plex_imdb_id,plex_tmdb_id,plex_year FROM plex_media WHERE path=?",
            (str(media),),
        ).fetchone()
    params = {"languages": ",".join(selected)}
    match_source = "title"
    if context:
        kind = str(context["kind"] or "movie")
        imdb_id = str(context["plex_imdb_id"] or "")
        tmdb_id = str(context["plex_tmdb_id"] or "")
        if imdb_id:
            params["imdb_id"] = imdb_id
            match_source = "imdb"
        elif tmdb_id:
            params["tmdb_id"] = tmdb_id
            match_source = "tmdb"
        else:
            params["query"] = search
            match_source = "title"
        if kind == "episode":
            # OpenSubtitles has two mutually exclusive episode lookup modes:
            # an episode IMDb/TMDB id by itself, or a parent-show id together
            # with season/episode. Plex stores the episode feature id here, so
            # never append season/episode to an episode id.
            if imdb_id or tmdb_id:
                pass
            else:
                params["type"] = "episode"
                if context["season_number"] is not None:
                    params["season_number"] = str(int(context["season_number"] or 0))
                if context["episode_number"] is not None:
                    params["episode_number"] = str(int(context["episode_number"] or 0))
        elif context["plex_year"]:
            params["year"] = str(int(context["plex_year"]))
    else:
        params["query"] = search
    result=_os_request("GET","/subtitles?"+urllib.parse.urlencode(params),settings)
    # A stale Plex identifier should not hide valid subtitles. Retry the
    # documented title/episode query when an identifier produces no results.
    if not (result.get("data") or []) and match_source in {"imdb", "tmdb"}:
        fallback = {"query": search}
        if context and str(context["kind"] or "movie") == "episode":
            fallback["type"] = "episode"
            if context["season_number"] is not None:
                fallback["season_number"] = str(int(context["season_number"] or 0))
            if context["episode_number"] is not None:
                fallback["episode_number"] = str(int(context["episode_number"] or 0))
        elif context and context["plex_year"]:
            fallback["year"] = str(int(context["plex_year"]))
        fallback["languages"] = params["languages"]
        result = _os_request("GET", "/subtitles?" + urllib.parse.urlencode(fallback), settings)
        if result.get("data"):
            params = fallback
            match_source = "title-fallback"
    entries=[]
    for item in result.get("data") or []:
        attrs=item.get("attributes") or {}; files=attrs.get("files") or []
        entries.append({"id":item.get("id"),"language":attrs.get("language") or "","format":attrs.get("format") or "","release":attrs.get("release") or attrs.get("movie_name") or "","hearing_impaired":bool(attrs.get("hearing_impaired")),"fps":attrs.get("fps"),"files":[{"file_id":f.get("file_id"),"file_name":f.get("file_name") or ""} for f in files if f.get("file_id")]})
    return {"items":entries,"total_count":result.get("total_count",len(entries)),"languages":selected,"match_source":match_source,"search_params":{key:value for key,value in params.items() if key not in {"languages"}}}

@app.post("/api/v19/opensubtitles/download")
def download_opensubtitles(request: OpenSubtitlesDownloadRequest) -> dict:
    settings=_os_settings()
    if not settings["enabled"]: raise HTTPException(409,"Enable OpenSubtitles in Setup first")
    if not settings["api_key"] or not settings["username"] or not settings["password"]: raise HTTPException(409,"Configure OpenSubtitles API key, username, and password in Setup first")
    login=_os_request("POST","/login",settings,{"username":settings["username"],"password":settings["password"]})
    token=str(login.get("token") or "")
    if not token: raise HTTPException(502,"OpenSubtitles login did not return a token")
    result=_os_request("POST","/download",settings,{"file_id":request.file_id,"sub_format":"srt"},token)
    link=str(result.get("link") or "")
    if not link: raise HTTPException(502,"OpenSubtitles did not return a download link")
    try:
        with urllib.request.urlopen(urllib.request.Request(link,headers={"User-Agent":"VideoStreamEdit/1.0"}),timeout=60) as response: content=response.read()
    except (urllib.error.URLError,TimeoutError) as exc: raise HTTPException(502,f"Could not download subtitle file: {getattr(exc,'reason',str(exc))}") from exc
    original=Path(request.path).resolve(); OPEN_SUBTITLES_STAGE.mkdir(parents=True,exist_ok=True)
    safe=re.sub(r"[^A-Za-z0-9._-]+","_",request.file_name.strip() or (original.stem+"."+(request.language or "und")+".srt"))
    target=OPEN_SUBTITLES_STAGE/(hashlib.sha256((str(original)+str(request.file_id)).encode()).hexdigest()[:16]+"-"+safe)
    target.write_bytes(content)
    target.with_name(target.name+".json").write_text(json.dumps({"original_path":str(original),"language":request.language or "und","file_name":request.file_name or target.name},ensure_ascii=False),encoding="utf-8")
    return {"staged":True,"staged_path":str(target),"original_path":str(original),"language":request.language or "und","bytes":len(content),"message":"Subtitle downloaded to staging; review before integrating it."}

def _configured_common_languages() -> list[str]:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key=\'common_languages\'").fetchone()
    try:
        values = json.loads(row["value"]) if row else ["pt", "pt-BR", "en"]
    except (TypeError, ValueError, json.JSONDecodeError):
        values = ["pt", "pt-BR", "en"]
    return [str(value).strip() for value in values if str(value).strip()]


def _uncommon_language_rows() -> tuple[dict[str, list[dict]], set[str], set[str]]:
    """Return indexed streams outside configured common-language bases."""
    configured = _configured_common_languages()
    bases = {str(value).strip().casefold().replace("_", "-").split("-", 1)[0] for value in configured if str(value).strip()}
    with connection() as db:
        rows = db.execute("""SELECT path,source,stream_type,type_index,external_path,codec,
            language,region,track_name,filename_tags FROM media_stream_index
            WHERE stream_type IN ('audio','subtitle','external')
            AND path NOT IN (SELECT path FROM index_task_queue WHERE status IN ('pending','running'))
            ORDER BY path,stream_type,type_index""").fetchall()
    result: dict[str, list[dict]] = {}
    for row in rows:
        value = dict(row)
        language = str(value.get("language") or "").strip()
        normalized = language.casefold().replace("_", "-")
        base = normalized.split("-", 1)[0]
        if not normalized or normalized == "und" or base in bases:
            continue
        try:
            value["filename_tags"] = json.loads(value.get("filename_tags") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            value["filename_tags"] = []
        value["stream_type"] = "subtitle" if value.get("stream_type") == "external" else value.get("stream_type")
        value["language"] = language
        result.setdefault(str(value["path"]), []).append(value)
    return result, bases, set(configured)


@app.get("/api/v19/reports/uncommon-languages")
def uncommon_language_report(kind: str) -> dict:
    if kind not in {"tv", "movies"}:
        raise HTTPException(400, "Kind must be tv or movies")
    blocked_paths = report_blocked_paths()
    by_path, _bases, configured = _uncommon_language_rows()
    by_path = {path: rows for path, rows in by_path.items() if path not in blocked_paths}
    def qualifies(rows: list[dict]) -> bool:
        audio = {str(row["language"]).casefold() for row in rows if row["stream_type"] == "audio"}
        subtitle = {str(row["language"]).casefold() for row in rows if row["stream_type"] == "subtitle"}
        return len(audio) > 2 or len(subtitle) > 2
    if kind == "movies":
        items = []
        for movie in plex_movies():
            path = str(movie["path"]); rows = by_path.get(path, [])
            if rows and qualifies(rows):
                items.append({"title": Path(str(movie.get("name") or path)).stem, "path": path,
                              "root_name": movie.get("root_name") or "", "streams": rows})
    else:
        items = []
        for show in plex_tv():
            episodes = []
            for season in show["seasons"]:
                for episode in season["episodes"]:
                    path = str(episode["path"]); rows = by_path.get(path, [])
                    if rows and qualifies(rows):
                        episodes.append({"episode": episode.get("name") or Path(path).stem, "path": path, "streams": rows})
            if episodes:
                items.append({"title": str(show["name"]), "root_name": str(show.get("root_name") or ""),
                              "media_count": len(episodes), "episodes": episodes})
    items.sort(key=lambda item: item["title"].casefold())
    return {"kind": kind, "configured_languages": sorted(configured, key=str.casefold),
            "items": items, "title_count": len(items),
            "media_count": sum(item.get("media_count", 1) for item in items)}


class UncommonLanguageRemoval(BaseModel):
    kind: Literal["tv", "movies"]
    paths: list[str] = Field(min_length=1, max_length=30000)
    languages: list[str] = Field(min_length=1, max_length=200)
    stream_types: list[Literal["audio", "subtitle", "external"]] = Field(min_length=1)
    mode: Literal["now", "queue"] = "queue"


@app.post("/api/v19/reports/uncommon-languages/remove")
def remove_uncommon_languages(request: UncommonLanguageRemoval) -> dict:
    """Use the normal preflight/edit pipeline for report removals."""
    filters = {"presence": "have", "stream_type": None, "stream_types": request.stream_types,
               "language": None, "languages": list(dict.fromkeys(request.languages)),
               "region": None, "language_regions": None, "track_name": None, "filename_tag": None}
    payload = {"paths": list(dict.fromkeys(request.paths)), "filters": filters,
               "changed_fields": ["remove"], "language": "", "region": "", "track_name": "",
               "integrate": False, "remove": True, "default_action": "unchanged",
               "forced_action": "unchanged", "mode": request.mode}
    if request.kind == "tv":
        from app.v79 import SeasonStreamBulkEdit, season_stream_bulk_edit
        return season_stream_bulk_edit(SeasonStreamBulkEdit.model_validate(payload))
    from app.v82 import MovieStreamBulkEdit, movie_stream_bulk_edit
    return movie_stream_bulk_edit(MovieStreamBulkEdit.model_validate(payload))


from app import web_assets as _web_assets  # noqa: E402,F401
