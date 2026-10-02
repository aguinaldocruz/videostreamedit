from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.v2 import probe
from app.v5 import external_subtitles
from app.v28 import authorized_import_file
from app.v82 import app
from app import subtitle_cache as _subtitle_cache  # noqa: F401 - registers cache schema startup
from app.v11 import connection

logger = logging.getLogger("uvicorn.error")
TEXT_SUBTITLE_CODECS = {"ass", "ssa", "subrip", "srt", "text", "mov_text", "webvtt", "microdvd", "jacosub", "sami", "realtext", "subviewer", "subviewer1", "vplayer"}
GRAPHICAL_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "pgs", "dvd_subtitle", "dvb_subtitle", "vobsub", "xsub", "ass-image"}
OPEN_SUBTITLES_STAGE = Path(os.getenv("VSE_OPEN_SUBTITLES_STAGE", "/data/opensubtitles-staging")).resolve()


def _stage_manifest(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def _staged_subtitle_items(media: Path) -> list[dict]:
    """Return OpenSubtitles downloads associated with this media.

    New downloads have a sidecar manifest. The filename fallback keeps older
    staged downloads visible when their original manifest was not written.
    """
    if not OPEN_SUBTITLES_STAGE.is_dir():
        return []
    result=[]
    stem=media.stem.casefold().replace("_", " ").replace(".", " ")
    tokens=[token for token in stem.split() if len(token)>=3]
    for candidate in sorted(OPEN_SUBTITLES_STAGE.iterdir(), key=lambda item: item.name.casefold()):
        if not candidate.is_file() or candidate.name.endswith(".json"):
            continue
        data={}
        manifest=_stage_manifest(candidate)
        if manifest.is_file():
            try: data=json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError): data={}
        original=str(data.get("original_path") or "")
        associated=original == str(media)
        if not associated:
            name=candidate.stem.casefold().replace("_", " ").replace(".", " ")
            associated=bool(tokens and sum(1 for token in tokens if token in name)>=max(2, min(4, len(tokens))))
        if not associated:
            continue
        try: size=candidate.stat().st_size
        except OSError: size=0
        result.append({"staged_path":str(candidate),"file_name":candidate.name,"language":str(data.get("language") or "und"),"bytes":size,"manifest":bool(data),"original_path":original})
    return result


class StagedSubtitleAction(BaseModel):
    path: str
    staged_path: str
    language: str = "und"
    region: str = ""


def _checked_staged(path: str) -> Path:
    candidate=Path(path).resolve()
    if candidate.parent != OPEN_SUBTITLES_STAGE or not candidate.is_file() or candidate.name.endswith(".json"):
        raise HTTPException(400, "Invalid staged subtitle path")
    return candidate


@app.get("/api/v83/media-review/staged-subtitles")
def staged_subtitles_for_review(path: str) -> dict:
    media=authorized_import_file(path)
    return {"items":_staged_subtitle_items(media)}


@app.get("/api/v83/media-review/staged-subtitles/file")
def staged_subtitle_file(path: str, staged_path: str):
    media=authorized_import_file(path); staged=_checked_staged(staged_path)
    if str(staged) not in {item["staged_path"] for item in _staged_subtitle_items(media)}:
        raise HTTPException(400, "This staged subtitle is not associated with the selected media")
    return FileResponse(staged, media_type="text/plain", headers={"Cache-Control":"no-store"})


@app.post("/api/v83/media-review/staged-subtitles/approve")
def approve_staged_subtitle(request: StagedSubtitleAction) -> dict:
    media=authorized_import_file(request.path)
    staged=_checked_staged(request.staged_path)
    items={item["staged_path"] for item in _staged_subtitle_items(media)}
    if str(staged) not in items:
        raise HTTPException(400, "This staged subtitle is not associated with the selected media")
    language=(request.language or "und").strip().lower().replace("_", "-")
    region=(request.region or "").strip().upper()
    suffix=f".{language}{('-'+region) if region else ''}.srt"
    target=media.with_name(media.stem+suffix)
    number=2
    while target.exists():
        target=media.with_name(media.stem+suffix[:-4]+f"-{number}.srt");number+=1
    shutil.copy2(staged,target)
    staged.unlink(missing_ok=True);_stage_manifest(staged).unlink(missing_ok=True)
    try:
        from app.v80 import request_media_indexes
        request_media_indexes(str(media), ["core", "subtitles"], "Approved staged OpenSubtitles subtitle", detection_scope={"subtitle_indices":"all"})
    except Exception as exc:
        logger.warning("media_review event=staged_subtitle_index_queue_failed path=%s error=%s", str(media), exc)
    return {"approved":True,"path":str(media),"subtitle_path":str(target),"message":"Subtitle approved and placed beside the media; subtitle indexing was queued."}


@app.post("/api/v83/media-review/staged-subtitles/reject")
def reject_staged_subtitle(request: StagedSubtitleAction) -> dict:
    media=authorized_import_file(request.path);staged=_checked_staged(request.staged_path)
    if str(staged) not in {item["staged_path"] for item in _staged_subtitle_items(media)}:
        raise HTTPException(400, "This staged subtitle is not associated with the selected media")
    staged.unlink(missing_ok=True);_stage_manifest(staged).unlink(missing_ok=True)
    return {"rejected":True,"path":str(media),"message":"Staged subtitle rejected and removed."}


@app.on_event("startup")
def ensure_review_capability_table() -> None:
    with connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS media_review_capability (
            path TEXT PRIMARY KEY, modified_ns BIGINT NOT NULL, size BIGINT NOT NULL,
            metadata_json TEXT NOT NULL, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")


def _review_metadata(media: Path) -> dict:
    """Return cached ffprobe metadata, refreshing only after a fingerprint change.

    This is capability metadata, not a preview cache: it contains no rendered
    media and is cheap to invalidate when the source changes.
    """
    stat = media.stat()
    with connection() as db:
        row = db.execute("SELECT modified_ns,size,metadata_json FROM media_review_capability WHERE path=?", (str(media),)).fetchone()
    if row and int(row["modified_ns"]) == int(stat.st_mtime_ns) and int(row["size"]) == int(stat.st_size):
        try:
            return json.loads(row["metadata_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    metadata = probe(media)
    payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    with connection() as db:
        db.execute("""INSERT INTO media_review_capability(path,modified_ns,size,metadata_json,updated_at)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET modified_ns=excluded.modified_ns,size=excluded.size,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP
        """, (str(media), int(stat.st_mtime_ns), int(stat.st_size), payload))
    return metadata


def _aac_browser_safe(stream: dict) -> bool:
    codec = str(stream.get("codec_name") or "").casefold()
    profile = str(stream.get("profile") or "").casefold().replace("-", " ")
    tag = str(stream.get("codec_tag_string") or "").casefold()
    rate = int(float(stream.get("sample_rate") or 0)) if str(stream.get("sample_rate") or "").replace(".", "", 1).isdigit() else 0
    channels = int(stream.get("channels") or 0)
    if codec != "aac" or rate not in (0, 44100, 48000) or channels > 6:
        return False
    # AAC-LC is object type 2 and the most portable browser/HLS profile.
    if profile in {"lc", "aac lc", "low complexity"}:
        return True
    # Some containers omit the profile but expose the MPEG-4 object tag.
    return not profile and ("mp4a.40.2" in tag or tag == "mp4a")


def _review_plan(metadata: dict, mode: str, audio_index: int | None, subtitles: list[dict], subtitle_source: str, subtitle_index: int | None) -> dict:
    streams = metadata.get("streams") or []
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    video = videos[0] if videos else {}
    audio = audios[audio_index] if audio_index is not None and audio_index < len(audios) else {}
    video_codec = str(video.get("codec_name") or "").casefold()
    video_profile = str(video.get("profile") or "").casefold()
    video_copy = mode == "audio" or (video_codec == "h264" and "10" not in video_profile and "high 10" not in video_profile)
    audio_copy = mode in ("subtitle", "video") or _aac_browser_safe(audio)
    selected_subtitle = subtitles[subtitle_index] if subtitle_source == "embedded" and subtitle_index is not None and subtitle_index < len(subtitles) else {}
    subtitle_codec = str(selected_subtitle.get("codec_name") or "").casefold()
    subtitle_supported = subtitle_source == "none" or subtitle_source == "external" or subtitle_codec in TEXT_SUBTITLE_CODECS
    if mode == "subtitle":
        strategy = "subtitle_text"
        estimate = "Usually immediate"
    elif mode == "video" and video_copy:
        strategy = "direct_remux"
        estimate = "Usually a few seconds"
    elif mode == "video":
        strategy = "full_transcode"
        estimate = "Longer preparation; video conversion required"
    elif video_copy and audio_copy:
        strategy = "direct_remux"
        estimate = "Usually a few seconds"
    elif video_copy:
        strategy = "audio_transcode"
        estimate = "Short preparation; video is copied"
    else:
        strategy = "full_transcode"
        estimate = "Longer preparation; video conversion required"
    return {"strategy": strategy, "estimate": estimate, "video_copy": video_copy, "audio_copy": audio_copy,
            "video_codec": video_codec or "unknown", "video_profile": video_profile or "unknown",
            "audio_codec": str(audio.get("codec_name") or "unknown"), "audio_profile": str(audio.get("profile") or "unknown"),
            "audio_sample_rate": audio.get("sample_rate"), "audio_channels": audio.get("channels"),
            "subtitle_codec": subtitle_codec or "none", "subtitle_supported": subtitle_supported}





def _subtitle_file(media: Path, source: str, subtitle_index: int | None, external_path: str | None, directory: Path) -> str | None:
    if source == "none":
        return None
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if source == "embedded":
        if subtitle_index is None:
            raise HTTPException(400, "Select a subtitle or choose No subtitles")
        command += ["-i", str(media), "-map", f"0:s:{subtitle_index}"]
    elif source == "staged":
        staged=_checked_staged(external_path or "")
        if str(staged) not in {item["staged_path"] for item in _staged_subtitle_items(media)}:
            raise HTTPException(400, "The selected staged subtitle does not belong to this media")
        command += ["-i", str(staged), "-map", "0:0"]
    else:
        candidates = {str(item["path"]): Path(item["path"]) for item in external_subtitles(media)}
        source_path = candidates.get(str(external_path or ""))
        if not source_path:
            raise HTTPException(400, "The selected external subtitle does not belong to this media")
        if source_path.suffix.lower() in {".sub", ".idx", ".sup", ".vob", ".pgs"}:
            raise HTTPException(422, "Graphical subtitles are unsupported in full media streaming review; choose No subtitles")
        command += ["-i", str(source_path), "-map", "0:0"]
    output = directory / "subtitle.vtt"
    result = subprocess.run(command + ["-f", "webvtt", "-y", str(output)], capture_output=True, timeout=120)
    if result.returncode or not output.is_file() or output.stat().st_size < 8:
        detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise HTTPException(422, detail or "The selected subtitle could not be converted to WebVTT")
    return "subtitle.vtt"


@app.get("/api/v83/media-review/capability")
def media_review_capability(
    path: str,
    audio_index: int | None = Query(default=None, ge=0),
    mode: str = Query(default="av", pattern="^(av|audio|video|subtitle)$"),
    subtitle_source: str = Query(default="none", pattern="^(none|embedded|external|staged)$"),
    subtitle_index: int | None = Query(default=None, ge=0),
    external_path: str | None = None,
):
    media = authorized_import_file(path)
    metadata = _review_metadata(media)
    streams = metadata.get("streams") or []
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    subtitles = [item for item in streams if item.get("codec_type") == "subtitle"]
    if mode in ("av", "audio") and (audio_index is None or audio_index >= len(audios)):
        raise HTTPException(400, "Select an audio stream before preparing playback")
    if mode == "subtitle" and subtitle_source == "none":
        raise HTTPException(400, "Select a text subtitle for subtitle-only preview")
    plan = _review_plan(metadata, mode, audio_index, subtitles, subtitle_source, subtitle_index)
    if subtitle_source == "staged":
        staged=_checked_staged(external_path or "")
        if str(staged) not in {item["staged_path"] for item in _staged_subtitle_items(media)}:
            raise HTTPException(400, "The selected staged subtitle does not belong to this media")
        plan["subtitle_supported"] = True
        plan["subtitle_codec"] = "srt"
    if subtitle_source == "external" and external_path:
        if Path(external_path).suffix.lower() in {".sub", ".idx", ".sup", ".vob", ".pgs"}:
            plan["subtitle_supported"] = False
    if not plan["subtitle_supported"]:
        plan["message"] = "Graphical subtitles cannot be overlaid; choose No subtitles or a text subtitle"
    else:
        plan["message"] = {"direct_remux": "The selected streams can play without conversion.", "audio_transcode": "Video will be copied; only audio compatibility conversion is needed.", "full_transcode": "Video conversion is required before playback.", "subtitle_text": "The subtitle will be extracted as text without changing the media."}[plan["strategy"]]
    return {"duration": float((metadata.get("format") or {}).get("duration") or 0), "plan": plan}


@app.get("/api/v83/media-review/stream")
def retired_review_stream():
    raise HTTPException(410, "The player has been upgraded. Reload the page to use bounded playback.")


from app import review_playback, review_audio  # register bounded playback and supervised audio APIs
from app import subtitle_cache_schedule as _subtitle_cache_schedule  # noqa: F401 - scheduled cache APIs
