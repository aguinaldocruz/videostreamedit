from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.v2 import probe
from app.v5 import external_subtitles, plex_language_pair, split_tag
from app.v11 import STATIC_DIR, app, connection, plex_authorized_file


def matroska_tracks(path: Path) -> dict[str, list[dict]]:
    found = {"audio": [], "subtitle": []}
    if path.suffix.lower() not in {".mkv", ".mka", ".mks", ".mk3d"}:
        return found
    try:
        result = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True, timeout=90, check=True)
        for track in json.loads(result.stdout).get("tracks", []):
            track_type = "subtitle" if track.get("type") == "subtitles" else track.get("type")
            if track_type in found:
                found[track_type].append(track.get("properties") or {})
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return found





def _indexed_media_details(media: Path) -> dict | None:
    """Return a cheap editor snapshot when the unified index matches the file."""
    try:
        stat = media.stat()
    except OSError:
        return None
    try:
        with connection() as db:
            # Never let a queue/migration lock make opening Stream Properties
            # wait for the database busy timeout. Fall back to ffprobe instead.
            raw = getattr(db, "raw", None)
            if raw is not None:
                raw.execute("SET LOCAL statement_timeout = '750ms'")
            state = db.execute(
                "SELECT modified_ns,size FROM media_stream_index_state WHERE path=?",
                (str(media),),
            ).fetchone()
            if not state or int(state["modified_ns"] or -1) != int(stat.st_mtime_ns) or int(state["size"] or -1) != int(stat.st_size):
                return None
            rows = db.execute(
                "SELECT source,stream_type,type_index,external_path,codec,language,region,track_name,is_default,is_forced,filename_tags "
                "FROM media_stream_index WHERE path=? ORDER BY stream_type,type_index,external_path",
                (str(media),),
            ).fetchall()
            if not rows:
                return None
            detections = [dict(row) for row in db.execute(
                "SELECT source,type_index,external_path,metadata_language,detected_language,confidence,evidence,analysis_status,analysis_reason,cue_count,text_chars,text_coverage,markup_count,damage "
                "FROM portuguese_language_detection WHERE path=?", (str(media),)
            ).fetchall()]
            try:
                audio_rows = {int(row["type_index"]): dict(row) for row in db.execute(
                    "SELECT type_index,metadata_language,detected_language,confidence,mismatch FROM audio_language_detection WHERE path=? AND mismatch=1", (str(media),)
                ).fetchall()}
            except Exception:
                audio_rows = {}
    except Exception:
        return None
    streams=[]; indexed_external={}
    for row in rows:
        value=dict(row)
        try:
            tags=json.loads(value.get("filename_tags") or "[]")
        except (TypeError,ValueError,json.JSONDecodeError):
            tags=[]
        item={
            "codec_type": "subtitle" if value["stream_type"] == "external" else value["stream_type"],
            "type_index": int(value["type_index"] or 0),
            "codec": value.get("codec") or "unknown",
            "language": value.get("language") or "",
            "region": value.get("region") or "",
            "title": value.get("track_name") or "",
            "default": bool(value.get("is_default")),
            "forced": bool(value.get("is_forced")),
            "external": value["source"] == "external" or value["stream_type"] == "external",
        }
        if item["external"]:
            item["path"] = value.get("external_path") or ""
            item["name"] = Path(item["path"]).name if item["path"] else ""
            item["filename_tags"] = tags
            indexed_external[str(item["path"])] = item
        else:
            streams.append(item)
    external=[]
    for item in external_subtitles(media):
        indexed=indexed_external.get(str(item.get("path")))
        merged={key:indexed[key] for key in ("language","region","title","forced","codec","filename_tags") if indexed and key in indexed}
        external.append({**item, **merged})
    # A removed sidecar must not reappear just because its old index row still
    # exists. The incremental index will retire that row separately.
    for item in streams:
        if item["codec_type"] == "audio":
            detection=audio_rows.get(int(item["type_index"]))
            if detection:
                item["audio_detection"]=detection
    from app.detection_policy import filter_editor_findings
    detections = filter_editor_findings(str(media), streams, detections)
    return {"path":str(media),"streams":streams,"external_subtitles":external,"portuguese_detection":detections,"source":"unified-index"}


def _probe_details_fast(media: Path) -> dict:
    """Lightweight fallback for a stale/unindexed file.

    Full Plex-aware mkvmerge parsing remains the authority during indexing, but
    opening the editor should not wait on it. ffprobe stream tags are enough to
    render the editor immediately; the next index refresh reconciles variants.
    """
    counters={"audio":0,"subtitle":0}; streams=[]
    for stream in probe(media).get("streams", []):
        codec_type=stream.get("codec_type")
        if codec_type not in counters: continue
        index=counters[codec_type]; counters[codec_type]+=1
        tags=stream.get("tags") or {}
        language,region=split_tag(tags.get("language") or tags.get("language_ietf") or "")
        streams.append({"codec_type":codec_type,"type_index":index,"codec":stream.get("codec_name") or "unknown","language":language,"region":region,"title":tags.get("title") or "","default":bool((stream.get("disposition") or {}).get("default")),"forced":bool((stream.get("disposition") or {}).get("forced")),"external":False})
    return {"path":str(media),"streams":streams,"external_subtitles":external_subtitles(media),"portuguese_detection":[],"source":"probe-fast","stale":True}


@app.get("/api/v13/media/details-fast")
def media_details_fast(path: str, refresh: int | None = None) -> dict:
    media=plex_authorized_file(path)
    # A post-edit reload must observe the file that was just committed rather
    # than the old core-index snapshot.  Normal editor opens remain fast and
    # indexed; refresh is intentionally scoped to the requested media.
    # After an edit, ffprobe exposes only legacy ``por`` and can omit
    # Matroska's language_ietf (for example pt-BR). Use the Plex-aware parser
    # so a close/reopen cannot make a regional value appear to revert.
    if refresh is not None:
        return media_details_with_ietf(str(media))
    indexed=_indexed_media_details(media)
    if indexed is not None:
        return indexed
    # First open after a rewrite, or before initial indexing, uses a bounded
    # ffprobe fallback so the dialog remains interactive. The normal core index
    # subsequently reconciles Plex-specific language variants authoritatively.
    return _probe_details_fast(media)

@app.get("/api/v13/media/details")
def media_details_with_ietf(path: str) -> dict:
    media = plex_authorized_file(path)
    mkv = matroska_tracks(media)
    counters = {"audio": 0, "subtitle": 0}
    streams = []
    for stream in probe(media).get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type not in counters:
            continue
        type_index = counters[codec_type]
        counters[codec_type] += 1
        tags = stream.get("tags") or {}
        language, legacy_region = split_tag(tags.get("language") or "")
        properties = mkv[codec_type][type_index] if type_index < len(mkv[codec_type]) else {}
        if not language:
            language, legacy_region = split_tag(properties.get("language") or "")
        ietf_language, ietf_region = split_tag(properties.get("language_ietf") or "")
        # FFmpeg's Matroska muxer can store a BCP-47 region as
        # tag_language_variant instead of LanguageIETF. Treat it as the
        # region of the legacy language so older remuxes do not repeatedly
        # offer the same automatic region correction.
        variant_region = str(properties.get("tag_language_variant") or "").strip().upper()
        if ietf_language:
            language = ietf_language
        language, effective_region = plex_language_pair(language, ietf_region or variant_region or legacy_region)
        streams.append({
            "codec_type": codec_type, "type_index": type_index,
            "codec": stream.get("codec_name") or "unknown",
            "language": language, "region": effective_region,
            "title": tags.get("title") or properties.get("track_name") or "",
            "default": bool((stream.get("disposition") or {}).get("default")),
            "forced": bool((stream.get("disposition") or {}).get("forced")),
            "external": False,
        })
    with connection() as db:
        detections = [dict(row) for row in db.execute(
            "SELECT source,type_index,external_path,metadata_language,detected_language,confidence,evidence,analysis_status,analysis_reason,cue_count,text_chars,text_coverage,markup_count,damage "
            "FROM portuguese_language_detection WHERE path=?", (str(media),)
        ).fetchall()]
        try:
            audio_rows = {int(row["type_index"]): dict(row) for row in db.execute(
                "SELECT type_index,metadata_language,detected_language,confidence,mismatch FROM audio_language_detection WHERE path=? AND mismatch=1", (str(media),)
            ).fetchall()}
        except Exception:
            audio_rows = {}
    for stream in streams:
        if stream.get("codec_type") == "audio" and int(stream.get("type_index", -1)) in audio_rows:
            stream["audio_detection"] = audio_rows[int(stream["type_index"])]
    from app.detection_policy import filter_editor_findings
    detections = filter_editor_findings(str(media), streams, detections)
    return {"path": str(media), "streams": streams, "external_subtitles": external_subtitles(media), "portuguese_detection": detections}
