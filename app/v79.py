from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
import uuid
from contextvars import ContextVar
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v54 as indexes
import app.v65 as tasks
from app.v5 import (
    SUBTITLE_EXTENSIONS,
    external_filename_metadata,
    external_subtitles,
    plex_language_pair,
)
from app.v7 import ReorderEditRequest
from app.v11 import connection
from app.v13 import media_details_with_ietf
from app.v43 import optimized_media_edit
from app.v51 import cached_subtitle_text, damage_kind, decode_external
from app.v78 import app
from app.preflight_dispatcher import enqueue_bulk_preflight, register_approval_handler, register_handler

logger = logging.getLogger("videostreamedit")
_legacy_processors = dict(indexes.processors)
from app.subtitle_detector_config import SUBTITLE_DETECTOR_VERSION

_audio_language_code = tasks._audio_language_code


def extract_subtitle_text_for_detection(media: Path, selector: str) -> str:
    """Extract a bounded text sample without disguising tool failures as empty subtitles."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media),
             "-map", selector, "-t", "900", "-f", "srt", "pipe:1"],
            capture_output=True, timeout=50, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg is unavailable; subtitle text could not be extracted") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg timed out while extracting subtitle text") from exc
    text = (result.stdout or b"").decode("utf-8", errors="replace")
    if result.returncode and not text.strip():
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip().replace("\n", " ")
        raise RuntimeError("FFmpeg could not extract subtitle text" + (f": {detail[-240:]}" if detail else ""))
    return text


class LanguageDetectionSettings(BaseModel):
    common_languages: list[str] = Field(default_factory=list, max_length=200)


class SeasonStreamRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=2000)


class TvShowStatusRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=30000)


class SeasonStreamFilter(BaseModel):
    presence: Literal["have", "not_have"] = "have"
    stream_type: Literal["audio", "subtitle", "external"] | None = None
    stream_types: list[Literal["audio", "subtitle", "external"]] | None = None
    language: str | None = None
    languages: list[str] | None = None
    region: str | None = None
    language_regions: list[str] | None = None
    track_name: str | None = None
    filename_tag: str | None = None


class SeasonStreamBulkEdit(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=2000)
    filters: SeasonStreamFilter
    changed_fields: list[Literal["language", "region", "track_name", "default", "forced", "integrate", "remove"]] = Field(min_length=1)
    language: str = Field(default="", max_length=64)
    region: str = Field(default="", max_length=64)
    track_name: str = Field(default="", max_length=512)
    integrate: bool = False
    remove: bool = False
    default_action: Literal["unchanged", "set", "clear"] = "unchanged"
    forced_action: Literal["unchanged", "set", "clear"] = "unchanged"
    target_keys: list[str] | None = None
    mode: Literal["now", "queue"]


class TvEditSessionOpen(BaseModel):
    show_id: str = Field(min_length=1, max_length=512)
    show_title: str = Field(default="", max_length=512)


class TvEditOperationRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    operation: dict


tv_commit_owner = ContextVar('tv_commit_owner', default='')


@app.on_event('startup')
def _ensure_tv_edit_sessions() -> None:
    """Create the lightweight TV-show draft store.

    Drafts contain operations and fingerprints only; media files are never
    copied while a show is being edited.
    """
    with connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS tv_edit_sessions (
            session_id TEXT PRIMARY KEY, show_id TEXT NOT NULL, show_title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            committed_at TEXT, error TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS tv_edit_operations (
            operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, path TEXT NOT NULL,
            operation_json TEXT NOT NULL, input_signature TEXT NOT NULL DEFAULT '{}',
            sequence INTEGER NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES tv_edit_sessions(session_id))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_tv_edit_operations_session ON tv_edit_operations(session_id, path, sequence)")
        db.execute("ALTER TABLE tv_edit_sessions ADD COLUMN IF NOT EXISTS commit_task_id INTEGER")


def _tv_edit_now() -> str:
    return tasks.utc_now()


def _tv_edit_signature(path: str) -> dict:
    try:
        stat = Path(path).stat()
    except OSError:
        return {"exists": False, "path": path}
    return {"exists": True, "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _tv_edit_session(session_id: str):
    with connection() as db:
        row = db.execute("SELECT * FROM tv_edit_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "TV-show edit session not found")
    return dict(row)


def tv_edit_save_status(session: dict) -> dict:
    with connection() as db:
        if session.get('commit_task_id'):
            task = db.execute('SELECT id,status,progress_current,progress_total,progress_message,error FROM task_queue WHERE id=?', (session['commit_task_id'],)).fetchone()
        else:
            task = db.execute("SELECT id,status,progress_current,progress_total,progress_message,error FROM task_queue WHERE task_type='tv_edit_session_commit' AND payload_json LIKE ? ORDER BY id DESC LIMIT 1", ('%'+session['session_id']+'%',)).fetchone()
        count = db.execute('SELECT count(DISTINCT path) AS n FROM tv_edit_operations WHERE session_id=?', (session['session_id'],)).fetchone()['n']
    return {**session, 'task':dict(task) if task else None, 'task_id':task['id'] if task else None,
            'operation_count':int(count), 'dirty':bool(count) and session['status']=='open'}


@app.get('/api/v79/tv/edit-session-active')
def active_tv_edit_session(show_id: str) -> dict:
    with connection() as db:
        row = db.execute("SELECT * FROM tv_edit_sessions WHERE show_id=? AND status IN ('open','committing') ORDER BY created_at DESC LIMIT 1", (show_id,)).fetchone()
    return {'session':tv_edit_save_status(dict(row)) if row else None}


@app.get('/api/v79/tv/edit-session/{session_id}/status')
def tv_edit_session_status(session_id: str) -> dict:
    return tv_edit_save_status(_tv_edit_session(session_id))


@app.post("/api/v79/tv/edit-session/open")
def open_tv_edit_session(request: TvEditSessionOpen) -> dict:
    _ensure_tv_edit_sessions()
    now = _tv_edit_now()
    with connection() as db:
        active = db.execute("SELECT * FROM tv_edit_sessions WHERE show_id=? AND status IN ('open','committing') ORDER BY created_at DESC LIMIT 1", (request.show_id,)).fetchone()
        if active:
            session = dict(active)
        else:
            session_id = uuid.uuid4().hex
            db.execute("INSERT INTO tv_edit_sessions(session_id,show_id,show_title,status,created_at,updated_at) VALUES(?,?,?,'open',?,?)", (session_id, request.show_id, request.show_title, now, now))
            session = {"session_id": session_id, "show_id": request.show_id, "show_title": request.show_title, "status": "open", "created_at": now, "updated_at": now}
        count = db.execute("SELECT COUNT(*) AS n FROM tv_edit_operations WHERE session_id=?", (session["session_id"],)).fetchone()["n"]
    session["operation_count"] = int(count or 0)
    session["dirty"] = bool(count)
    return tv_edit_save_status(session)


@app.get("/api/v79/tv/edit-session/{session_id}")
def get_tv_edit_session(session_id: str) -> dict:
    session = _tv_edit_session(session_id)
    with connection() as db:
        rows = db.execute("SELECT operation_id,path,operation_json,input_signature,sequence,created_at FROM tv_edit_operations WHERE session_id=? ORDER BY sequence", (session_id,)).fetchall()
    operations = []
    for row in rows:
        item = dict(row)
        try: item["operation"] = json.loads(item.pop("operation_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError): item["operation"] = {}
        try: item["input_signature"] = json.loads(item.get("input_signature") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError): item["input_signature"] = {}
        operations.append(item)
    session.update(operation_count=len(operations), dirty=bool(operations), operations=operations)
    return session


@app.get("/api/v79/tv/edit-session/{session_id}/projection")
def get_tv_edit_projection(session_id: str, path: str) -> dict:
    session = _tv_edit_session(session_id)
    if session["status"] not in {"open", "committing"}:
        return {"session_id": session_id, "path": path, "operations": []}
    with connection() as db:
        rows = db.execute("SELECT operation_json FROM tv_edit_operations WHERE session_id=? AND path=? ORDER BY sequence", (session_id, path)).fetchall()
    operations = []
    for row in rows:
        try:
            value = json.loads(row["operation_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not value.get("_discard"):
            operations.append(value)
    return {"session_id": session_id, "path": path, "operations": operations}


@app.post("/api/v79/tv/edit-session/{session_id}/operation")
def add_tv_edit_operation(session_id: str, request: TvEditOperationRequest) -> dict:
    session = _tv_edit_session(session_id)
    if session["status"] != "open":
        raise HTTPException(409, "This edit session is no longer open")
    note_edit = request.operation.get("note_edit")
    if note_edit is not None:
        if set(request.operation) != {"note_edit"} or not isinstance(note_edit, dict) or not note_edit or set(note_edit) - {"note", "reviewed", "final_version"}:
            raise HTTPException(400, "Invalid TV-show draft note change")
        if "note" in note_edit and (not isinstance(note_edit["note"], str) or len(note_edit["note"]) > 10000):
            raise HTTPException(400, "TV-show draft note is too long")
        if any(not isinstance(note_edit[key], bool) for key in ("reviewed", "final_version") if key in note_edit):
            raise HTTPException(400, "Reviewed and Final version must be true or false")
        if request.path != f"@show:{session['show_id']}":
            with connection() as db:
                media = db.execute("SELECT library_key,show_title,kind FROM plex_media WHERE path=?", (request.path,)).fetchone()
            if not media or media["kind"] != "episode" or f"{media['library_key']}:{media['show_title'] or 'Unknown show'}" != session["show_id"]:
                raise HTTPException(400, "Episode does not belong to this TV-show draft")
    now = _tv_edit_now()
    signature = _tv_edit_signature(request.path)
    with connection() as db:
        locked = db.execute('SELECT status FROM tv_edit_sessions WHERE session_id=? FOR UPDATE', (session_id,)).fetchone()
        if not locked or locked['status'] != 'open':
            raise HTTPException(409, 'This draft has already been submitted for saving')
        if bool(request.operation.get("_discard")):
            db.execute("DELETE FROM tv_edit_operations WHERE session_id=? AND path=?", (session_id, request.path))
            db.execute("UPDATE tv_edit_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            return {"session_id": session_id, "path": request.path, "discarded": True, "dirty": False}
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 AS n FROM tv_edit_operations WHERE session_id=?", (session_id,)).fetchone()["n"]
        operation_id = uuid.uuid4().hex
        db.execute("INSERT INTO tv_edit_operations(operation_id,session_id,path,operation_json,input_signature,sequence,created_at) VALUES(?,?,?,?,?,?,?)", (operation_id, session_id, request.path, json.dumps(request.operation, ensure_ascii=False, separators=(",", ":")), json.dumps(signature, separators=(",", ":")), int(sequence), now))
        db.execute("UPDATE tv_edit_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
    return {"session_id": session_id, "path": request.path, "operation_id": operation_id, "sequence": int(sequence), "dirty": True}


@app.delete("/api/v79/tv/edit-session/{session_id}")
def discard_tv_edit_session(session_id: str) -> dict:
    session = _tv_edit_session(session_id)
    if session["status"] == "committing":
        raise HTTPException(409, "This edit session is currently being saved")
    now = _tv_edit_now()
    with connection() as db:
        locked = db.execute('SELECT status FROM tv_edit_sessions WHERE session_id=? FOR UPDATE', (session_id,)).fetchone()
        if not locked or locked['status']=='committing':
            raise HTTPException(409, 'This draft has already been submitted; it cannot be discarded while saving')
        db.execute("DELETE FROM tv_edit_operations WHERE session_id=?", (session_id,))
        db.execute("UPDATE tv_edit_sessions SET status='discarded',updated_at=? WHERE session_id=?", (now, session_id))
    from app.review_audio import release_draft_audio
    release_draft_audio(session_id)
    return {"session_id": session_id, "status": "discarded", "dirty": False}


@app.post("/api/v79/tv/edit-session/{session_id}/save")
def save_tv_edit_session(session_id: str) -> dict:
    # The durable journal already contains the whole draft. Submit only its
    # reference; consolidation, validation and file work belong to the worker.
    with connection() as db:
        row = db.execute('SELECT * FROM tv_edit_sessions WHERE session_id=? FOR UPDATE', (session_id,)).fetchone()
        if not row: raise HTTPException(404, 'TV-show edit session not found')
        session = dict(row)
        if session['status'] in ('committing','committed'):
            return {**tv_edit_save_status(session), 'queued':session['status']=='committing'}
        if session['status'] != 'open': raise HTTPException(409, 'This edit session is not open')
        count = int(db.execute('SELECT count(DISTINCT path) AS n FROM tv_edit_operations WHERE session_id=?', (session_id,)).fetchone()['n'])
        now = _tv_edit_now()
        if not count:
            db.execute("UPDATE tv_edit_sessions SET status='committed',updated_at=?,committed_at=? WHERE session_id=?", (now,now,session_id))
            return {'session_id':session_id,'status':'committed','queued':False,'operation_count':0}
        task = tasks.enqueue('tv_edit_session_commit', {'session_id':session_id,'show_id':session['show_id'],'journal_reference':True,'_queue_group':session_id}, f"Save TV show edit · {session.get('show_title') or session['show_id']}", deduplicate=True)
        db.execute("UPDATE tv_edit_sessions SET status='committing',commit_task_id=?,updated_at=?,error=NULL WHERE session_id=?", (task['id'],now,session_id))
    return {'session_id':session_id,'status':'committing','queued':True,'task_id':task['id'],'operation_count':count}


def load_tv_commit_journal(session_id: str) -> list[dict]:
    with connection() as db:
        rows = db.execute('SELECT path,operation_json,input_signature,sequence FROM tv_edit_operations WHERE session_id=? ORDER BY sequence', (session_id,)).fetchall()
    consolidated = {}
    for row in rows:
        try: operation = json.loads(row["operation_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError('A stored TV draft operation is invalid; the draft was retained') from exc
        try:
            signature = json.loads(row["input_signature"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            signature = {}
        entry = consolidated.setdefault(str(row["path"]), {"path": str(row["path"]), "journal": [], "_media_signature": signature})
        entry["journal"].append(operation)
    return sorted(consolidated.values(), key=lambda item:item['path'].startswith('@show:'))




@app.get("/api/v79/language-detection/settings")
def get_language_detection_settings() -> dict:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key='common_languages'").fetchone()
    try:
        values = json.loads(row["value"]) if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    values = [str(value).strip() for value in values if str(value).strip()]
    return {"common_languages": values}


@app.put("/api/v79/language-detection/settings")
def update_language_detection_settings(request: LanguageDetectionSettings) -> dict:
    values = []
    for value in request.common_languages:
        value = str(value).strip()
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value[:16])
    if not values:
        raise HTTPException(400, "At least one common language is required")
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES('common_languages',?)", (json.dumps(values, ensure_ascii=False),))
        paths = [row["path"] for row in db.execute("SELECT path FROM plex_media").fetchall()]
    # The setting is part of each detector fingerprint. Queue subtitle
    # inspection incrementally so the new vocabulary is applied without a
    # destructive rebuild, while deduplication prevents duplicate requests.
    try:
        from app.v80 import enqueue_many
        queued = enqueue_many("subtitles", [{"path": path} for path in paths], "Language detection vocabulary changed") if paths else 0
    except Exception as exc:
        queued = 0
        logger.warning("subtitle_inspection event=settings_queue_failed error=%s", str(exc).replace("\n", " ")[-500:])
    logger.info("change=language_detection_common_languages values=%s queued_subtitle_checks=%d", ",".join(values), queued)
    return {"common_languages": values, "queued": queued}


@app.get("/api/v79/language-detection/forced-exclusions")
def get_forced_report_exclusions() -> dict:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key='forced_report_excluded_track_names'").fetchone()
    try:
        values = json.loads(row["value"]) if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    return {"track_names": values}


class ForcedReportExclusions(BaseModel):
    track_names: list[str] = Field(default_factory=list, max_length=200)


@app.put("/api/v79/language-detection/forced-exclusions")
def save_forced_report_exclusions(request: ForcedReportExclusions) -> dict:
    values = []
    for value in request.track_names:
        cleaned = str(value).strip()
        if cleaned and cleaned.casefold() not in {item.casefold() for item in values}:
            values.append(cleaned[:200])
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES('forced_report_excluded_track_names',?)", (json.dumps(values, ensure_ascii=False),))
    logger.info("change=forced_report_exclusions_saved count=%d", len(values))
    return {"track_names": values}


class VoiceDetectionSettings(BaseModel):
    enabled: bool = True
    service_url: str = Field(default="http://language-id:9000", max_length=500)
    sample_seconds: int = Field(default=30, ge=10, le=120)
    sample_positions: list[float] = Field(default_factory=lambda: [0.1, 0.5, 0.9], min_length=1, max_length=9)
    sample_count: int = Field(default=3, ge=2, le=9)


def voice_detection_settings() -> dict:
    defaults = {"enabled": True, "service_url": "http://language-id:9000", "sample_seconds": 30, "sample_positions": [0.1, 0.5, 0.9], "sample_count": 3}
    with connection() as db:
        rows = db.execute("SELECT key,value FROM language_detection_settings WHERE key LIKE 'voice_detection_%'").fetchall()
    values = {str(row["key"]): str(row["value"]) for row in rows}
    try: positions = [max(0.0, min(1.0, float(x))) for x in json.loads(values.get("voice_detection_positions", "[0.1,0.5,0.9]"))]
    except (TypeError, ValueError, json.JSONDecodeError): positions = defaults["sample_positions"]
    try: count = max(2, min(9, int(values.get("voice_detection_sample_count", str(len(positions) or 3)))))
    except (TypeError, ValueError): count = defaults["sample_count"]
    return {"enabled": values.get("voice_detection_enabled", "1") == "1", "service_url": values.get("voice_detection_url", defaults["service_url"]), "sample_seconds": max(10, min(120, int(values.get("voice_detection_sample_seconds", "30")))), "sample_positions": positions or defaults["sample_positions"], "sample_count": count}


@app.get("/api/v79/audio-language-detection/settings")
def get_voice_detection_settings() -> dict:
    return voice_detection_settings()


@app.get("/api/v79/audio-language-detection/health")
def voice_detection_health() -> dict:
    settings = voice_detection_settings()
    if not settings["enabled"]:
        return {"status": "disabled", "message": "Voice detection is disabled"}
    if urlsplit(settings["service_url"]).scheme not in {"http", "https"}:
        return {"status": "unavailable", "message": "Service URL must use HTTP or HTTPS"}
    try:
        request = Request(settings["service_url"].rstrip("/") + "/healthz", headers={"Accept": "application/json"})
        with urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"Service returned HTTP {response.status}")
        return {"status": "healthy", "message": "Voice detection service is reachable"}
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:300]}


@app.get("/api/v79/language-detection/queue-settings")
def get_detection_queue_settings() -> dict:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key='incremental_detection_enabled'").fetchone()
    return {"incremental_enabled": str(row["value"] if row else "1") == "1"}


class DetectionQueueRequest(BaseModel):
    mode: Literal["full", "incremental", "disable"]


@app.post("/api/v79/language-detection/queue")
def queue_language_detection(request: DetectionQueueRequest) -> dict:
    """Queue subtitle and voice detection without doing media work in the request."""
    from app.v65 import enqueue as enqueue_task
    from app.v80 import enqueue_many as enqueue_index_many
    with connection() as db:
        media = [dict(row) for row in db.execute("SELECT path,title FROM plex_media WHERE kind IN ('movie','episode') ORDER BY path").fetchall()]
        if request.mode == "full":
            db.execute("DELETE FROM portuguese_language_detection")
            db.execute("DELETE FROM portuguese_detection_state")
            db.execute("DELETE FROM audio_language_detection")
        db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES('incremental_detection_enabled',?)", ("1" if request.mode == "incremental" else "0",))
    queued_media = media if request.mode == "full" else []
    subtitle_added = enqueue_index_many("subtitles", queued_media, "Full language detection rebuild") if queued_media else 0
    voice_added = 0
    for item in queued_media:
        task = enqueue_task("audio_language_detection", {"path": item["path"]}, "Voice language detection rebuild", deduplicate=True)
        if task.get("status") == "pending":
            voice_added += 1
    logger.info("language_detection event=queue_requested mode=%s media=%d subtitle_added=%d voice_added=%d", request.mode, len(media), subtitle_added, voice_added)
    return {"mode": request.mode, "media": len(queued_media), "subtitle_queued": subtitle_added, "voice_queued": voice_added, "incremental_enabled": request.mode == "incremental"}


@app.put("/api/v79/audio-language-detection/settings")
def save_voice_detection_settings(request: VoiceDetectionSettings) -> dict:
    url = request.service_url.strip().rstrip("/") or "http://language-id:9000"
    count = max(2, min(9, int(request.sample_count)))
    # Keep positions deterministic and evenly distributed; this makes the
    # sample count setting authoritative while retaining the positions field
    # for older clients.
    positions = [round(index / (count + 1), 4) for index in range(1, count + 1)]
    with connection() as db:
        for key, value in (("voice_detection_enabled", "1" if request.enabled else "0"), ("voice_detection_url", url), ("voice_detection_sample_seconds", str(request.sample_seconds)), ("voice_detection_positions", json.dumps(positions)), ("voice_detection_sample_count", str(count))):
            db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES(?,?)", (key, value))
    logger.info("change=voice_detection_settings enabled=%s seconds=%d positions=%s", request.enabled, request.sample_seconds, positions)
    return voice_detection_settings()


class AudioLanguageDetectionRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class StreamLanguageDetectionRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    codec_type: Literal["audio", "subtitle"]
    type_index: int = Field(ge=-1, le=1000)
    external: bool = False


@app.post("/api/v79/language-detection/reset-media")
def reset_media_language_detection(request: AudioLanguageDetectionRequest) -> dict:
    """Acknowledge older browser refresh requests without deleting evidence."""
    path = str(Path(request.path).resolve())
    # Older open browser tabs still call this after checking one stream.
    # Evidence is retained; listing aggregates read the updated results.
    return {"path": path, "reset": False, "removed": 0, "refreshed": True}


@app.post("/api/v79/language-detection/stream")
def detect_stream_language(request: StreamLanguageDetectionRequest) -> dict:
    path = str(Path(request.path).resolve())
    with connection() as db:
        media = db.execute("SELECT kind FROM plex_media WHERE path=?", (path,)).fetchone()
    if not media or media["kind"] not in {"movie", "episode"} or not Path(path).is_file():
        raise HTTPException(404, "Media is not an accessible synchronized Plex item")
    from app.v86 import assert_media_editable
    assert_media_editable(path)
    from app.detection_policy import source_stamp, is_final
    initial_stamp = source_stamp(path)
    if request.codec_type == "audio":
        try:
            result = tasks.process_audio_language_detection(0, {"path": path, "stream_indices": [request.type_index]})
        except Exception as exc:
            raise HTTPException(422, f"Audio language detection failed: {exc}") from exc
        selected = next((item for item in result.get("results", []) if int(item.get("type_index", -1)) == request.type_index), None)
        return {"status": "completed", "codec_type": "audio", **(selected or {"type_index": request.type_index, "detected_language": "", "confidence": 0})}
    source = "external" if request.external else "embedded"
    with connection() as db:
        row = db.execute("SELECT external_path,codec FROM media_stream_index WHERE path=? AND stream_type IN ('subtitle','external') AND source=? AND type_index=?", (path, source, request.type_index)).fetchone()
    if source == 'embedded' and row and str(row['codec'] or '').lower() in {'hdmv_pgs_subtitle','dvd_subtitle','dvb_subtitle','pgs','vobsub'}:
        raise HTTPException(422, 'Graphical subtitle language detection is unsupported; no discrepancy warning was created')
    external_stamp = source_stamp(row['external_path']) if source == 'external' and row and row['external_path'] else None
    try:
        if source == "external":
            if not row or not row["external_path"]:
                raise ValueError("External subtitle is not indexed")
            text = cached_subtitle_text(Path(path), "external", -1, str(row["external_path"]))
            if text is None:
                text, _ = decode_external(Path(row["external_path"]).read_bytes()[:2_000_000])
        else:
            text = cached_subtitle_text(Path(path), "embedded", request.type_index)
            if text is None:
                text = extract_subtitle_text_for_detection(Path(path), f"0:s:{request.type_index}")
        allowed = {value.casefold().split("-", 1)[0].split("_", 1)[0] for value in common_detection_languages()}
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise HTTPException(422, f"Subtitle language detection failed: {exc}") from exc

    # An interactive check is authoritative evidence for this exact stream.
    # Persist it so a correct manual result does not reappear as a stale
    # no-confidence marker when the editor or report is reopened.
    if is_final(path) or source_stamp(path) != initial_stamp or (external_stamp and source_stamp(row['external_path']) != external_stamp):
        raise HTTPException(409, 'Media changed or became Final Version during detection; result discarded')
    with connection() as db:
        metadata = db.execute(
            "SELECT language,region,codec,external_path FROM media_stream_index "
            "WHERE path=? AND stream_type='subtitle' AND source=? AND type_index=?",
            (path, source, request.type_index),
        ).fetchone()
        metadata_language = str(metadata["language"] if metadata else "")
        metadata_region = str(metadata["region"] if metadata else "")
        external_path = str(metadata["external_path"] if metadata else "")
        from app.detection_policy import subtitle_assessment
        detected, confidence, lexical_evidence = detect_common_variant(text, allowed)
        detected, confidence, reason, status = subtitle_assessment(text, metadata['codec'] if metadata else '', metadata_language, metadata_region, allowed)
        confidence = float(confidence or 0.0)
        # Persist the policy explanation, not just the positive-evidence terms.
        # Otherwise no-confidence findings lose their useful reason at the API.
        evidence = lexical_evidence or reason
        signature = _subtitle_analysis_signature(path, {'source': source, 'type_index': request.type_index, 'external_path': external_path, 'codec': metadata['codec'] if metadata else '', 'language': metadata_language, 'region': metadata_region}, common_detection_languages(), Path(path).stat())
        db.execute(
            "DELETE FROM portuguese_language_detection WHERE path=? AND source=? AND type_index=? AND external_path=?",
            (path, source, request.type_index, external_path),
        )
        db.execute(
            "INSERT INTO portuguese_language_detection "
            "(path,source,type_index,external_path,metadata_language,metadata_region,detected_language,confidence,evidence,analysis_status,analysis_reason,analysis_signature,detector_version,checked_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (path, source, request.type_index, external_path, metadata_language, metadata_region, detected or "", confidence, evidence or "", status, reason, signature, SUBTITLE_DETECTOR_VERSION),
        )
        db.execute(
            "INSERT OR REPLACE INTO subtitle_detection_stream_state "
            "(path,source,type_index,external_path,signature,status,reason,checked_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (path, source, request.type_index, external_path, signature, status, reason),
        )
        metrics = _subtitle_metrics(text)
        sdh_label, sdh_confidence, sdh_evidence = analyze_sdh(text)
        db.execute('UPDATE portuguese_language_detection SET cue_count=?,text_chars=?,text_coverage=?,markup_count=?,damage=?,evidence_sample=?,sdh_label=?,sdh_confidence=?,sdh_evidence=? WHERE path=? AND source=? AND type_index=? AND external_path=?',
                   (metrics['cue_count'], metrics['text_chars'], metrics['text_coverage'], metrics['markup_count'], metrics['damage'], normalized_evidence_sample(text), sdh_label, sdh_confidence, sdh_evidence, path, source, request.type_index, external_path))
    return {"status": "completed", "codec_type": "subtitle", "detected_language": detected, "confidence": confidence, "evidence": evidence, "analysis_status": status, "analysis_reason": reason}


@app.post("/api/v79/audio-language-detection/queue")
def queue_audio_language_detection(request: AudioLanguageDetectionRequest) -> dict:
    path = str(Path(request.path))
    with connection() as db:
        row = db.execute("SELECT kind FROM plex_media WHERE path=?", (path,)).fetchone()
    if not row or row["kind"] not in {"movie", "episode"}:
        raise HTTPException(404, "Media is not in the synchronized Plex catalog")
    from app import v65 as task_queue
    task = task_queue.enqueue("audio_language_detection", {"path": path}, "Detect audio stream languages", deduplicate=True)
    return {"queued": True, "task_id": task["id"], "status": task["status"]}


@app.get("/api/v79/reports/audio-language")
def audio_language_report() -> dict:
    # Final Version media stay browseable but are excluded from actionable reports.
    from app.v19 import report_blocked_paths
    blocked_paths = report_blocked_paths()
    with connection() as db:
        rows = db.execute("""SELECT d.path,d.type_index,d.metadata_language,d.metadata_region,
            d.detected_language,d.confidence,d.samples_json,p.kind,p.title,s.language AS current_language,s.region AS current_region
            FROM audio_language_detection d JOIN plex_media p ON p.path=d.path
            JOIN media_stream_index s ON s.path=d.path AND s.stream_type='audio' AND s.source='embedded' AND s.type_index=d.type_index
            WHERE d.mismatch=1 ORDER BY p.title COLLATE NOCASE,d.path,d.type_index""").fetchall()
    items=[]
    from app.v19 import audio_detection_by_path
    valid_paths = audio_detection_by_path({str(row['path']) for row in rows})
    for row in rows:
        if str(row["path"]) in blocked_paths or str(row['path']) not in valid_paths:
            continue
        from app.detection_policy import language_matches
        if language_matches(row['detected_language'], row['current_language'], row['current_region']): continue
        item=dict(row)
        try: item["samples"]=json.loads(item.pop("samples_json") or "[]")
        except (TypeError,ValueError,json.JSONDecodeError): item["samples"]=[]
        detected_code = _audio_language_code(str(item.get("detected_language") or ""))
        agreeing = sum(1 for sample in item["samples"] if _audio_language_code(str(sample.get("language") or "")) == detected_code) if detected_code else 0
        item["sample_agree"] = agreeing
        item["sample_total"] = len(item["samples"])
        item["confidence"]=round(float(item["confidence"])*100,1)
        items.append(item)
    return {"items":items,"media_count":len({item["path"] for item in items}),"stream_count":len(items)}


def common_detection_languages() -> list[str]:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key='common_languages'").fetchone()
    try:
        values = json.loads(row["value"]) if row else ["pt", "pt-BR", "en"]
    except (TypeError, ValueError, json.JSONDecodeError):
        values = ["pt", "pt-BR", "en"]
    return [str(value).strip() for value in values if str(value).strip()]




def external_sidecar_data(path: str, candidates: list[Path] | None = None) -> tuple[list[dict], str]:
    media = Path(path)
    if not media.is_file():
        return [], "missing"
    values = []
    if candidates is None:
        streams = external_subtitles(media)
    else:
        prefix = media.stem.casefold()
        streams = []
        for candidate in candidates:
            candidate_stem = candidate.stem
            folded = candidate_stem.casefold()
            if folded != prefix and not folded.startswith(prefix + "."):
                continue
            suffix = candidate_stem[len(media.stem):].lstrip(".")
            language, region, filename_tags = external_filename_metadata(suffix)
            streams.append({
                "path": str(candidate.resolve()), "name": candidate.name,
                "codec_type": "subtitle", "codec": candidate.suffix.lower().lstrip("."),
                "language": language, "region": region, "title": "",
                "forced": "Forced" in filename_tags, "external": True,
                "filename_tags": filename_tags,
            })
    for stream in streams:
        subtitle = Path(stream["path"])
        try:
            stat = subtitle.stat()
            size, modified_ns = stat.st_size, stat.st_mtime_ns
        except OSError:
            size = modified_ns = 0
        values.append({**stream, "size": size, "modified_ns": modified_ns})
    fingerprint = json.dumps(
        [(item["path"], item["size"], item["modified_ns"]) for item in values],
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return values, hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def persist_external_sidecars(job: str, path: str) -> None:
    values, signature = external_sidecar_data(path)
    with connection() as db:
        if job == "core":
            db.execute("DELETE FROM external_subtitle_index WHERE media_path=?", (path,))
            db.executemany(
                "INSERT INTO external_subtitle_index(media_path,external_path,codec,language,region,track_name,forced,filename_tags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(path, item["path"], str(item.get("codec") or ""), str(item.get("language") or ""), str(item.get("region") or ""), str(item.get("title") or ""), int(bool(item.get("forced"))), json.dumps(item.get("filename_tags") or [], ensure_ascii=False), item["size"], item["modified_ns"]) for item in values],
            )
        db.execute("INSERT OR REPLACE INTO external_sidecar_index_state(job,path,signature,indexed_at) VALUES(?,?,?,CURRENT_TIMESTAMP)", (job, path, signature))


def prune_missing_external_sidecars(paths: set[str]) -> int:
    """Drop vanished sidecars from the read model without probing video files.

    The regular incremental index still handles new or modified sidecars. Only
    remove rows when the media and its parent directory remain accessible.
    """
    removed = 0
    ordered_paths = sorted(paths)
    directory_entries: dict[Path, set[str] | None] = {}
    with connection() as db:
        for start in range(0, len(ordered_paths), 800):
            batch = ordered_paths[start:start + 800]
            if not batch:
                continue
            rows = db.execute(
                f"SELECT path,external_path FROM media_stream_index WHERE source='external' "
                f"AND path IN ({','.join('?' for _ in batch)})", batch,
            ).fetchall()
            for row in rows:
                media = Path(row['path'])
                sidecar = Path(row['external_path'])
                if media.parent not in directory_entries:
                    try:
                        directory_entries[media.parent] = {item.name for item in media.parent.iterdir()}
                    except OSError:
                        directory_entries[media.parent] = None
                names = directory_entries[media.parent]
                if names is None or media.name not in names or sidecar.name in names:
                    continue
                db.execute(
                    "DELETE FROM media_stream_index WHERE path=? AND source='external' AND external_path=?",
                    (str(media), str(sidecar)),
                )
                db.execute(
                    "DELETE FROM external_subtitle_index WHERE media_path=? AND external_path=?",
                    (str(media), str(sidecar)),
                )
                removed += 1
    return removed


def pending_external_sidecars(job: str) -> list[dict]:
    with connection() as db:
        rows = [dict(row) for row in db.execute("""
            SELECT media.path,media.modified,media.size,media.title,state.signature
              FROM plex_media media
              LEFT JOIN external_sidecar_index_state state ON state.job=? AND state.path=media.path
             ORDER BY media.kind,media.title COLLATE NOCASE,media.path
        """, (job,))]
    pending = []
    directory_candidates: dict[Path, list[Path]] = {}
    for directory in {Path(row["path"]).parent for row in rows}:
        try:
            directory_candidates[directory] = [
                candidate for candidate in sorted(directory.iterdir(), key=lambda value: value.name.casefold())
                if candidate.is_file() and candidate.suffix.lower() in SUBTITLE_EXTENSIONS
            ]
        except OSError:
            directory_candidates[directory] = []
    baselines: list[tuple[dict, list[dict], str]] = []
    for row in rows:
        values, signature = external_sidecar_data(row["path"], directory_candidates[Path(row["path"]).parent])
        if row.get("signature") is None:
            baselines.append((row, values, signature))
        elif row["signature"] != signature:
            row.pop("signature", None)
            pending.append(row)
    if baselines:
        with connection() as db:
            if job == "core":
                for row, values, _ in baselines:
                    db.execute("DELETE FROM external_subtitle_index WHERE media_path=?", (row["path"],))
                    db.executemany(
                        "INSERT INTO external_subtitle_index(media_path,external_path,codec,language,region,track_name,forced,filename_tags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [(row["path"], item["path"], str(item.get("codec") or ""), str(item.get("language") or ""), str(item.get("region") or ""), str(item.get("title") or ""), int(bool(item.get("forced"))), json.dumps(item.get("filename_tags") or [], ensure_ascii=False), item["size"], item["modified_ns"]) for item in values],
                    )
            db.executemany(
                "INSERT OR REPLACE INTO external_sidecar_index_state(job,path,signature,indexed_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                [(job, row["path"], signature) for row, _, signature in baselines],
            )
    logger.info("external_sidecar_check event=completed job=%s media=%d baselined=%d pending=%d", job, len(rows), len(baselines), len(pending))
    return pending


def pending_episode_rows() -> list[dict]:
    """Return stale episodes from canonical filesystem freshness state."""
    with connection() as db:
        rows = [dict(row) for row in db.execute("""
            SELECT media.path,media.title,state.modified_ns,state.size
              FROM plex_media media
              LEFT JOIN media_stream_index_state state ON state.path=media.path
             WHERE media.kind='episode'
             ORDER BY media.title COLLATE NOCASE,media.path
        """)]
    pending = []
    for row in rows:
        try:
            stat = Path(row["path"]).stat()
            if row["modified_ns"] is None or int(row["modified_ns"]) != int(stat.st_mtime_ns) or int(row["size"] or -1) != int(stat.st_size):
                pending.append({"path": row["path"], "title": row["title"], "modified": int(stat.st_mtime), "size": int(stat.st_size)})
        except OSError:
            pending.append({"path": row["path"], "title": row["title"], "modified": 0, "size": 0})
    return pending


def inspect_embedded_streams(path: str) -> list[tuple[str, str, str, str]]:
    details = media_details_with_ietf(path)
    embedded = [
        (
            str(stream.get("codec_type") or ""), str(stream.get("language") or "").strip(),
            str(stream.get("region") or "").strip(), str(stream.get("title") or "").strip(),
        )
        for stream in details.get("streams", [])
        if stream.get("codec_type") in {"audio", "subtitle"} and not stream.get("external")
    ]
    external = [
        ("external", str(stream.get("language") or "").strip(), str(stream.get("region") or "").strip(), str(stream.get("title") or "").strip())
        for stream in details.get("external_subtitles", [])
    ]
    return embedded + external


_PT_VARIANT_RESOURCE = Path(__file__).with_name("data") / "pt_variant_lexicon.json"
try:
    _PT_VARIANT_DATA = json.loads(_PT_VARIANT_RESOURCE.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    _PT_VARIANT_DATA = {}
_PT_BR_WORDS = set(_PT_VARIANT_DATA.get("br_words") or {"você", "vocês", "ônibus", "celular", "geladeira", "banheiro", "legal", "a gente", "trem", "arquivo", "tela", "rodoviária"})
_PT_PT_WORDS = set(_PT_VARIANT_DATA.get("pt_words") or {"tu", "comboio", "telemóvel", "frigorífico", "casa de banho", "fixe", "rapariga", "autocarro", "ficheiro", "ecrã", "miúdo", "pequeno-almoço", "bocadinho", "se calhar", "percebido"})
_PT_BR_PATTERNS = tuple(_PT_VARIANT_DATA.get("br_patterns") or ())
_PT_PT_PATTERNS = tuple(_PT_VARIANT_DATA.get("pt_patterns") or ())
_PT_BR_SPELLING = set(_PT_VARIANT_DATA.get("br_spelling") or ())
_PT_PT_SPELLING = set(_PT_VARIANT_DATA.get("pt_spelling") or ())
# Broad stop-word profiles prevent a repeated English song from outweighing
# an otherwise Portuguese subtitle merely because the old list was tiny.
_PT_COMMON_WORDS = {"o", "a", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "e", "que", "em", "no", "na", "nos", "nas", "para", "por", "com", "sem", "se", "não", "sim", "eu", "ele", "ela", "eles", "elas", "me", "te", "seu", "sua", "seus", "suas", "está", "estão", "foi", "ser", "como", "mais", "mas", "ou", "já", "aqui", "isso", "esse", "essa", "onde", "quando", "porque", "vai", "vou", "tem", "têm"}
_EN_WORDS = {"the", "and", "you", "that", "what", "with", "this", "not", "have", "for", "are", "your", "who", "is", "am", "i", "me", "my", "we", "they", "to", "of", "in", "on", "it", "was", "be", "will", "where", "why", "how", "can", "do", "does", "from", "all", "after", "before", "there", "here", "tell", "must", "never", "someone"}
_PT_BR_RE = re.compile(r"\b(?:estou|estamos|está|estão)\s+(?:fazendo|dizendo|vendo|falando|chegando|entrando|saindo|trabalhando|ligando|tentando)\b", re.IGNORECASE)
_PT_PT_RE = re.compile(r"\b(?:estou|estamos|está|estão)\s+a\s+(?:fazer|dizer|ver|falar|chegar|entrar|sair|trabalhar|ligar|tentar)\b", re.IGNORECASE)
_PT_PT_CONTEXT_RE = re.compile(r"\b(?:percebido|estamos\s+a\s+chegar|estão\s+a\s+chegar|se\s+faz\s+favor|com\s+certeza)\b", re.IGNORECASE)
_PT_BR_CONTEXT_RE = re.compile(r"\b(?:estou|estamos|está|estão)\s+(?:fazendo|dizendo|vendo|falando|chegando|entrando|saindo|trabalhando|ligando|tentando)\b", re.IGNORECASE)

def _resource_pattern_count(patterns: tuple[str, ...], value: str) -> int:
    # Repetition is common in songs, credits and malformed OCR. Cap each
    # marker's contribution so one repeated phrase cannot manufacture high
    # language confidence.
    return sum(min(3, len(re.findall(r"(?<!\w)" + re.escape(pattern.casefold()) + r"(?!\w)", value))) for pattern in patterns)


def detect_common_variant(text: str, allowed_languages: set[str] | None = None) -> tuple[str, float, str]:
    allowed = {str(value).casefold().replace("_", "-").split("-", 1)[0] for value in (allowed_languages or {"pt", "en"})}
    normalized = re.sub(r"\s+", " ", text.casefold())

    def term_count(words: set[str]) -> int:
        # Match complete words/phrases only. Raw substring counting makes the
        # PT-PT marker "tu" match inside unrelated words such as "tudo".
        return sum(min(3, len(re.findall(r"(?<!\w)" + re.escape(word) + r"(?!\w)", normalized))) for word in words)

    # Establish the dominant base language first. Regional markers are only
    # considered after Portuguese wins, so a short English song or quotation
    # cannot change the classification of an otherwise Portuguese subtitle.
    pt_common = term_count(_PT_COMMON_WORDS) if "pt" in allowed else 0
    br = (term_count(_PT_BR_WORDS) + _resource_pattern_count(_PT_BR_PATTERNS, normalized) * 2 + 2 * term_count(_PT_BR_SPELLING) + 2 * min(3, len(_PT_BR_RE.findall(normalized))) + 2 * min(3, len(_PT_BR_CONTEXT_RE.findall(normalized)))) if "pt" in allowed else 0
    pt = (term_count(_PT_PT_WORDS) + _resource_pattern_count(_PT_PT_PATTERNS, normalized) * 2 + 2 * term_count(_PT_PT_SPELLING) + 2 * min(3, len(_PT_PT_RE.findall(normalized))) + 2 * min(3, len(_PT_PT_CONTEXT_RE.findall(normalized)))) if "pt" in allowed else 0
    en = term_count(_EN_WORDS) if "en" in allowed else 0
    portuguese = pt_common + br + pt
    if portuguese == 0 and en < 3:
        return "", 0.0, ""
    if en > portuguese * 2.00 and en >= 8:
        detected, score, total = "en", en, en + portuguese
    elif portuguese >= 6 and portuguese >= en * 1.20 and (pt >= 2 or br >= 2):
        # Ambiguous Portuguese is deliberately ignored instead of producing a
        # misleading PT-BR/PT-PT mismatch report.
        if br >= 3 and br >= pt + 2 and br >= pt * 1.5:
            detected, score, total = "pt-BR", portuguese, portuguese + en
        elif pt >= 3 and pt >= br + 2 and pt >= br * 1.5:
            detected, score, total = "pt-PT", portuguese, portuguese + en
        else:
            return "pt", .80, "Portuguese identified; insufficient independent evidence for a regional variant"
    else:
        # A subtitle containing substantial evidence for both Portuguese and
        # English is often a bilingual/dual-language track or an OCR/credit
        # mixture. Do not force either language; preserve an actionable reason
        # for the no-confidence report instead of silently discarding evidence.
        if portuguese >= 6 and en >= 6 and 0.45 <= portuguese / max(en, 1) <= 2.20:
            return "", 0.0, "Mixed Portuguese/English subtitle evidence"
        # Generic Portuguese subtitles often contain no reliable regional
        # markers. Return the base language when the corpus is substantial and
        # clearly dominant; callers can then treat pt as compatible with either
        # Plex Portuguese region without inventing PT-BR/PT-PT certainty.
        if "pt" in allowed and pt_common >= 12 and portuguese >= en * 1.5:
            score = pt_common
            total = pt_common + en
            confidence = min(0.89, 0.60 + max(0.0, score / max(total, 1) - 0.50) * 0.40)
            return "pt", confidence, "Portuguese base-language vocabulary"
        return "", 0.0, ""
    dominance = score / max(total, 1)
    evidence_strength = min(score / 12, 1.0)
    confidence = min(0.99, 0.60 + max(0.0, dominance - 0.50) * 0.40 * evidence_strength)
    if confidence < 0.60:
        return "", 0.0, ""
    vocabulary = _PT_BR_WORDS if detected == "pt-BR" else _PT_PT_WORDS if detected == "pt-PT" else _EN_WORDS
    evidence = ", ".join(sorted(vocabulary, key=lambda word: (-normalized.count(word), word))[:3])
    return detected, confidence, evidence


_SDH_SOUND_RE = re.compile(r"(?:\[[^\]]{1,120}\]|\([^\)]{1,120}\)|♪|♫|\b(?:music|singing|song|laughs?|crying|sobbing|sighs?|gasps?|door|phone|telephone|alarm|applause|inaudible|música|cantando|risos?|choro|suspiro|porta|telefone|alarme|aplausos|inaudível)\b)", re.IGNORECASE)
_SDH_SPEAKER_RE = re.compile(r"(?:^|\n)\s*(?:\[[^\]]{1,60}\]|[A-ZÀ-Ý][A-ZÀ-Ý .'-]{2,}\s*:)", re.MULTILINE)

def analyze_sdh(text: str) -> tuple[str, float, str]:
    normalized = re.sub(r"\s+", " ", text or "")
    if not normalized.strip():
        return "Cannot evaluate", 0.0, "No readable subtitle text"
    cues = max(1, len(re.findall(r"-->[^\n]*", text or "")))
    sound_hits = len(_SDH_SOUND_RE.findall(normalized))
    speaker_hits = len(_SDH_SPEAKER_RE.findall(text or ""))
    music_hits = len(re.findall(r"[♪♫]|\b(?:music|song|música|canção)\b", normalized, re.IGNORECASE))
    score = min(0.99, sound_hits / max(cues * 0.18, 1) * 0.45 + speaker_hits / max(cues * 0.12, 1) * 0.35 + music_hits / max(cues * 0.08, 1) * 0.20)
    if score >= 0.72:
        label = "Likely SDH"
    elif score <= 0.18 and sound_hits == 0 and speaker_hits == 0:
        label = "Likely dialogue-only"
    else:
        label = "Uncertain"
    evidence = []
    if sound_hits: evidence.append(f"{sound_hits} sound/description cue(s)")
    if speaker_hits: evidence.append(f"{speaker_hits} speaker label(s)")
    if music_hits: evidence.append(f"{music_hits} music cue(s)")
    return label, round(score, 3), ", ".join(evidence) or "No distinctive SDH markers"


def calibrate_subtitle_confidence(confidence: float, cue_count: int, text_chars: int, text_coverage: float) -> float:
    """Cap confidence when a subtitle has too little readable evidence."""
    value = max(0.0, min(0.99, float(confidence or 0.0)))
    if cue_count < 3 or text_chars < 120:
        return min(value, 0.60)
    if text_coverage < 0.12:
        return min(value, 0.60)
    return value


def subtitle_quality_issue(text: str, damage: str = "") -> str:
    """Return a conservative user-facing quality issue, if one is present."""
    if str(damage or "") not in {"", "None"}:
        return f"Subtitle text flagged as {damage}"
    if re.search(r"(?:https?://|www\.|\b(?:discord|t\.me|telegram)\b)", text or "", re.IGNORECASE):
        return "Subtitle contains advertising or link-like text"
    return ""


def _subtitle_analysis_signature(path: str, stream: dict, configured: list[str], stat, detector_version: int = SUBTITLE_DETECTOR_VERSION) -> str:
    """Return a stream-level fingerprint for incremental subtitle analysis."""
    external = str(stream.get("external_path") or "")
    external_state = None
    if external:
        try:
            item = Path(external).stat()
            external_state = (item.st_size, item.st_mtime_ns)
        except OSError:
            external_state = ("missing",)
    payload = [detector_version, str(path), stream.get("source"), int(stream['type_index']) if stream.get('type_index') is not None else -1,
               external, str(stream.get("codec") or ""), str(stream.get("language") or ""),
               str(stream.get("region") or ""), stat.st_size, stat.st_mtime_ns, external_state, configured]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalized_evidence_sample(text: str, limit: int = 280) -> str:
    """Return a small diagnostic excerpt, never a subtitle preview cache."""
    value = text or ""
    value = re.sub(r"(?m)^\s*\d+\s*$", " ", value)
    value = re.sub(r"(?m)^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->.*$", " ", value)
    value = re.sub(r"<[^>]{1,200}>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max(0, int(limit))]


def _subtitle_metrics(text: str) -> dict:
    value = text or ""
    cues = len(re.findall(r"-->[^\n]*", value))
    payload = re.sub(r"(?m)^\s*\d+\s*$|^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->.*$", " ", value)
    readable = re.sub(r"\s+", " ", payload).strip()
    markup_count = len(re.findall(r"<\s*/?\s*[a-zA-Z][^>]*>", value))
    return {
        "cue_count": cues,
        "text_chars": len(readable),
        "text_coverage": round(min(1.0, len(readable) / max(cues * 40, 1)), 4) if cues else 0.0,
        "markup_count": markup_count,
        "damage": damage_kind(value),
    }


def inspect_portuguese_language(path: str, detection_scope: dict | None = None, text_cache: dict | None = None, *, cache_only: bool = False) -> None:
    from app.detection_policy import is_final, source_stamp, subtitle_assessment, language_key
    if is_final(path): return
    media = Path(path)
    if not media.is_file():
        return
    detection_scope = detection_scope or {}
    requested = detection_scope.get("subtitle_indices")
    requested_external = set(detection_scope.get('subtitle_external_paths') or [])
    force_targeted = bool(requested or requested_external)
    stat = media.stat()
    initial_stamp = source_stamp(path)
    configured = common_detection_languages()
    bases = {value.casefold().split("-", 1)[0].split("_", 1)[0] for value in configured}
    with connection() as db:
        stream_rows = db.execute(
            "SELECT source,type_index,external_path,codec,language,region FROM media_stream_index "
            "WHERE path=? AND stream_type IN ('subtitle','external')", (path,)
        ).fetchall()
        existing = {
            (str(row["source"]), (int(row["type_index"]) if row["type_index"] is not None else -1), str(row["external_path"] or "")): dict(row)
            for row in db.execute("SELECT * FROM portuguese_language_detection WHERE path=?", (path,)).fetchall()
        }
        states = {
            (str(row["source"]), (int(row["type_index"]) if row["type_index"] is not None else -1), str(row["external_path"] or "")): dict(row)
            for row in db.execute("SELECT * FROM subtitle_detection_stream_state WHERE path=?", (path,)).fetchall()
        }
    wanted = {int(item) for item in requested} if requested and requested != "all" else None
    streams = [dict(row) for row in stream_rows]
    external_stamps = {str(s['external_path']): source_stamp(s['external_path']) for s in streams if s['source'] == 'external' and s['external_path'] and Path(s['external_path']).is_file()}
    if requested != 'all' and (wanted is not None or requested_external):
        streams = [stream for stream in streams
                   if (stream['source'] == 'embedded' and int(stream['type_index']) in (wanted or set()))
                   or (stream['source'] == 'external' and stream['external_path'] in requested_external)]
    cache = text_cache if text_cache is not None else {}
    results: list[dict] = []
    for stream in streams:
        key = (str(stream["source"]), int(stream["type_index"]), str(stream["external_path"] or ""))
        stream_signature = _subtitle_analysis_signature(path, stream, configured, stat)
        prior_state = states.get(key)
        prior_row = existing.get(key)
        if detection_scope.get('subtitle_recompare') and prior_row and prior_row.get('analysis_status') in {'complete', 'mismatch'} and prior_row.get('detector_version') == SUBTITLE_DETECTOR_VERSION:
            from app.detection_policy import language_matches
            reused = dict(prior_row)
            agrees = language_matches(reused['detected_language'], stream['language'], stream['region'])
            reused.update(metadata_language=stream['language'], metadata_region=stream['region'], analysis_signature=stream_signature,
                          analysis_status='complete' if agrees else 'mismatch', analysis_reason='Existing content evidence compared with updated metadata')
            results.append(reused)
            continue
        if prior_state and prior_row and prior_state.get("signature") == stream_signature and not force_targeted:
            results.append(prior_row)
            continue
        metadata_language = language_key(stream["language"])[0]
        base = {
            "path": path, "source": key[0], "type_index": key[1], "external_path": key[2],
            "metadata_language": str(stream["language"] or ""), "metadata_region": str(stream["region"] or ""),
            "detected_language": "", "confidence": 0.0, "evidence": "", "sdh_label": "",
            "sdh_confidence": 0.0, "sdh_evidence": "", "evidence_sample": "", "analysis_status": "complete",
            "analysis_reason": "", "analysis_signature": stream_signature, "detector_version": SUBTITLE_DETECTOR_VERSION,
            "cue_count": 0, "text_chars": 0, "text_coverage": 0.0, "markup_count": 0, "damage": "",
        }
        # Check all subtitles for common-language evidence, even when their
        # current metadata claims a different language.
        try:
            codec = str(stream.get("codec") or "").casefold()
            if stream["source"] == "external":
                text_key = ("external", key[2])
                cached = cache.get(text_key)
                if cached is None:
                    if cache_only:
                        base.update(analysis_status="no_confidence", analysis_reason="Subtitle format is not in the complete text cache")
                        results.append(base)
                        continue
                    text = cached_subtitle_text(media, "external", -1, key[2])
                    cached = (text, "UTF-8 (cached)") if text is not None else decode_external(Path(key[2]).read_bytes()[:2_000_000])
                    cache[text_key] = cached
                text, _ = cached
            elif (codec in {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "subtitles"}
                  or any(token in codec for token in ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text"))
                  or not codec):
                text_key = ("embedded", key[1])
                text = cache.get(text_key)
                if text is None:
                    if cache_only:
                        base.update(analysis_status="no_confidence", analysis_reason="Subtitle format is not in the complete text cache")
                        results.append(base)
                        continue
                    text = cached_subtitle_text(media, "embedded", key[1])
                    if text is None:
                        text = extract_subtitle_text_for_detection(media, f"0:s:{key[1]}")
                    cache[text_key] = text
            else:
                text = ""
                base.update(analysis_status="no_confidence", analysis_reason=f"Unsupported graphical subtitle codec: {codec or 'unknown'}")
            base.update(_subtitle_metrics(text))
            base["evidence_sample"] = normalized_evidence_sample(text)
            sdh_label, sdh_confidence, sdh_evidence = analyze_sdh(text)
            base.update(sdh_label=sdh_label, sdh_confidence=sdh_confidence, sdh_evidence=sdh_evidence)
            detected, confidence, lexical_evidence = detect_common_variant(text, bases)
            detected, confidence, analysis_reason, assessment = subtitle_assessment(text, codec, stream['language'], stream['region'], bases)
            base.update(detected_language=detected, confidence=confidence, evidence=lexical_evidence or analysis_reason, analysis_status=assessment, analysis_reason=analysis_reason)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            base.update(analysis_status="unreadable", analysis_reason=f"Subtitle text could not be read: {str(exc)[:240]}")
        results.append(base)
    columns = ["path", "source", "type_index", "external_path", "metadata_language", "metadata_region", "detected_language", "confidence", "evidence", "sdh_label", "sdh_confidence", "sdh_evidence", "evidence_sample", "analysis_status", "analysis_reason", "analysis_signature", "detector_version", "cue_count", "text_chars", "text_coverage", "markup_count", "damage"]
    placeholders = ",".join("?" for _ in columns)
    if is_final(path) or source_stamp(path) != initial_stamp or any(not Path(p).is_file() or source_stamp(p) != stamp for p, stamp in external_stamps.items()):
        logger.info('language_detection event=discarded_stale_or_final file=%s', path)
        return
    with connection() as db:
        if requested != 'all' and (wanted is not None or requested_external):
            if wanted:
                marks = ",".join("?" for _ in wanted)
                db.execute(f"DELETE FROM portuguese_language_detection WHERE path=? AND source='embedded' AND type_index IN ({marks})", [path, *wanted])
                db.execute(f"DELETE FROM subtitle_detection_stream_state WHERE path=? AND source='embedded' AND type_index IN ({marks})", [path, *wanted])
            if requested_external:
                marks = ','.join('?' for _ in requested_external)
                db.execute(f"DELETE FROM portuguese_language_detection WHERE path=? AND source='external' AND external_path IN ({marks})", [path, *requested_external])
                db.execute(f"DELETE FROM subtitle_detection_stream_state WHERE path=? AND source='external' AND external_path IN ({marks})", [path, *requested_external])
        else:
            db.execute("DELETE FROM portuguese_language_detection WHERE path=?", (path,))
            db.execute("DELETE FROM subtitle_detection_stream_state WHERE path=?", (path,))
        db.executemany(f"INSERT INTO portuguese_language_detection({','.join(columns)},checked_at) VALUES({placeholders},CURRENT_TIMESTAMP)", [[row.get(column, "") for column in columns] for row in results])
        db.executemany("INSERT INTO subtitle_detection_stream_state(path,source,type_index,external_path,signature,status,reason,checked_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)", [(row["path"], row["source"], row["type_index"], row["external_path"], row["analysis_signature"], row["analysis_status"], row["analysis_reason"]) for row in results])
        full_signature = hashlib.sha256(json.dumps(["detector-v9-phase1", stat.st_size, stat.st_mtime_ns, configured, [(row["source"], row["type_index"], row["external_path"], row["analysis_signature"]) for row in results]], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        db.execute("INSERT OR REPLACE INTO portuguese_detection_state(path,signature,detector_version,checked_at) VALUES(?,?,?,CURRENT_TIMESTAMP)", (path, full_signature, SUBTITLE_DETECTOR_VERSION))

def _is_final_version(path: str) -> bool:
    """Final-version media remains indexed but skips advisory re-analysis."""
    with connection() as db:
        media = db.execute("SELECT kind,library_key,show_title FROM plex_media WHERE path=?", (str(path),)).fetchone()
        if not media:
            return False
        from app.v86 import final_version_entity_for_path
        entity = final_version_entity_for_path(path)
        row = db.execute("SELECT final_version FROM media_notes WHERE entity_type=? AND entity_key=?", entity).fetchone() if entity else None
        if not (row and row["final_version"]) and media["kind"] == "episode":
            parent = ("tv", f"{media['library_key']}:{media['show_title'] or 'Unknown show'}")
            row = db.execute("SELECT final_version FROM media_notes WHERE entity_type=? AND entity_key=?", parent).fetchone()
    return bool(row and row["final_version"])


def subtitle_index_with_sidecars(item: dict) -> None:
    path = str(item["path"])
    if _is_final_version(path):
        logger.info("subtitle_index event=skipped_final_version file=%s", path.replace("\n", "\\n"))
        return
    text_cache = item.get("_subtitle_text_cache") if item.get("_subtitle_cache_only") else {}
    if text_cache is None:
        text_cache = {}
    indexed_item = dict(item)
    indexed_item["_subtitle_text_cache"] = text_cache
    _legacy_processors["subtitles"](indexed_item)
    persist_external_sidecars("subtitles", path)
    scope = item.get("detection_scope") or {}
    if not scope.get("skip_detection"):
        inspect_portuguese_language(path, scope, text_cache, cache_only=bool(item.get("_subtitle_cache_only")))


indexes.processors["subtitles"] = subtitle_index_with_sidecars



def indexed_effective_target_paths(paths: list[str], request: SeasonStreamBulkEdit, targets: dict[str, list[str]]) -> list[str]:
    """Reduce tag-only bulk edits using indexed default/forced state."""
    changed = set(request.changed_fields)
    candidates = [path for path in paths if targets.get(path)]
    if not candidates or not changed or not changed.issubset({"default", "forced"}):
        return candidates
    by_path: dict[str, list[dict]] = {path: [] for path in candidates}
    with connection() as db:
        for start in range(0, len(candidates), 800):
            group = candidates[start:start + 800]
            rows = db.execute(
                f"SELECT path,stream_type,type_index,is_default,is_forced FROM media_stream_index WHERE path IN ({', '.join('?' for _ in group)})",
                group,
            ).fetchall()
            for row in rows:
                by_path[str(row["path"])].append(dict(row))
    stream_type = request.filters.stream_type if request.filters.stream_type in {"audio", "subtitle"} else None
    if stream_type is None:
        return candidates
    effective: list[str] = []
    for path, rows in by_path.items():
        keys = set(targets[path])
        typed = [row for row in rows if not stream_type or str(row.get("stream_type")) == stream_type]
        matching = [row for row in typed if f"embedded:{row.get('stream_type')}:{row.get('type_index')}" in keys]
        matching.sort(key=lambda row: int(row.get("type_index") or 0))
        if not matching:
            continue
        needs_change = False
        for field, action in (("default", request.default_action), ("forced", request.forced_action)):
            if field not in changed or action == "unchanged":
                continue
            if action == 'clear':
                if any(str(row.get('is_'+field)).lower() in {'1','true','t','yes'} for row in matching):
                    needs_change = True
                    break
                continue
            desired = None if action == "clear" else f"embedded:{matching[-1].get('stream_type')}:{matching[-1].get('type_index')}"
            current = {f"embedded:{row.get('stream_type')}:{row.get('type_index')}" for row in typed if str(row.get("is_default" if field == "default" else "is_forced")).lower() in {"1", "true", "t", "yes"}}
            if current != {desired}:
                needs_change = True
                break
        if needs_change:
            effective.append(path)
    return effective
def selected_episode_rows(paths: list[str]) -> list[dict]:
    unique = list(dict.fromkeys(paths))
    found = []
    with connection() as db:
        for start in range(0, len(unique), 800):
            group = unique[start:start + 800]
            placeholders = ",".join("?" for _ in group)
            rows = db.execute(
                f"SELECT path,modified,size,title FROM plex_media WHERE kind='episode' AND path IN ({placeholders})",
                group,
            ).fetchall()
            found.extend(dict(row) for row in rows)
    if {item["path"] for item in found} != set(unique):
        raise HTTPException(409, "The selected season changed. Refresh TV Shows and try again")
    # This is a read-only path used to populate filter options.  Do not run
    # per-episode Final-version editability checks here: they add several DB
    # queries per episode and unnecessarily delay opening filters.  Edit and
    # queue endpoints enforce the Final-version lock before mutating media.
    return found


def _queue_recoverable_stale_indexes(paths: set[str]) -> set[str]:
    """Queue accessible stale files without racing a just-finished index.

    TV status/filter refreshes can run while a core worker is committing its
    read model.  During that small visibility window the file looks stale and
    a second reconciliation request used to be created.  Suppress only that
    short race window; if the filesystem mtime is newer than the successful
    index, the request remains eligible immediately.
    """
    if not paths:
        return set()
    from app.v80 import enqueue
    placeholders = ",".join("?" for _ in paths)
    ordered_paths = list(paths)
    with connection() as db:
        active = {row["path"] for row in db.execute("SELECT path FROM index_task_queue WHERE job=\'core\' AND status IN (\'pending\',\'running\')").fetchall()}
        failed = {row["path"] for row in db.execute("SELECT path FROM index_task_queue WHERE job=\'core\' AND status=\'failed\'").fetchall()}
        recent_rows = db.execute(
            f"SELECT path,finished_at FROM index_task_queue WHERE job='core' AND status='succeeded' AND path IN ({placeholders}) AND finished_at IS NOT NULL ORDER BY id DESC",
            ordered_paths,
        ).fetchall()
    recent_success: dict[str, float] = {}
    for row in recent_rows:
        if row["path"] in recent_success:
            continue
        try:
            recent_success[row["path"]] = datetime.fromisoformat(str(row["finished_at"])).timestamp()
        except (TypeError, ValueError, OSError):
            continue
    queued = set()
    suppressed = 0
    for path in paths:
        if path in active or path in failed or not Path(path).is_file():
            continue
        finished = recent_success.get(path)
        if finished is not None:
            try:
                # Only suppress a reconciliation caused by the same file
                # state. A write after the successful index is always allowed
                # through, even if it happened inside the cooldown window.
                if Path(path).stat().st_mtime <= finished + 1.0:
                    suppressed += 1
                    continue
            except OSError:
                continue
        if enqueue("core", path, "Automatic stale fingerprint reconciliation"):
            queued.add(path)
    if suppressed:
        logger.info("index_queue=core event=stale_reconciliation_coalesced items=%d", suppressed)
    return queued

@app.post("/api/v79/tv/show-status")
def tv_show_status(request: TvShowStatusRequest) -> dict:
    paths = list(dict.fromkeys(request.paths))
    placeholders = ",".join("?" for _ in paths)
    change_paths: set[str] = set()
    index_paths: set[str] = set()
    with connection() as db:
        change_paths = {row["path"] for row in db.execute(
            f"SELECT marker.path FROM media_change_request marker JOIN task_queue task ON task.id=marker.task_id WHERE marker.path IN ({placeholders}) AND task.status IN ('pending','running') AND task.task_type NOT IN ('audio_language_detection')", paths
        ).fetchall()}
        # Failed historical rows are diagnostics, not currently blocking work.
        # Only pending/running rows can make cached filter values unsafe.
        index_paths = {row["path"] for row in db.execute(
            f"SELECT DISTINCT path FROM index_task_queue WHERE path IN ({placeholders}) AND status IN ('pending','running') AND job <> 'subtitles'", paths
        ).fetchall()}
        indexed = {row["path"]: (row["modified_ns"], row["size"]) for row in db.execute(
            f"SELECT path,modified_ns,size FROM media_stream_index_state WHERE path IN ({placeholders})", paths
        ).fetchall()}
    stale_paths = set()
    for path in paths:
        try:
            stat = Path(path).stat()
            saved = indexed.get(path)
            if not saved or int(saved[0] or 0) != int(stat.st_mtime_ns) or int(saved[1] or 0) != int(stat.st_size):
                stale_paths.add(path)
        except OSError:
            stale_paths.add(path)
    auto_queued = _queue_recoverable_stale_indexes(stale_paths)
    if auto_queued:
        # Re-read the durable queue: a short reconciliation may finish before
        # this request returns, which must not leave the UI falsely locked.
        with connection() as db:
            index_paths = {row["path"] for row in db.execute(
                f"SELECT DISTINCT path FROM index_task_queue WHERE path IN ({placeholders}) AND status IN ('pending','running') AND job <> 'subtitles'", paths
            ).fetchall()}
    # Queued media edits are safe to leave in the background: LUW/signature
    # protection isolates them and users may continue working on other media.
    # Index freshness is the only blocking condition because TV filters depend
    # on it to select the correct episodes.
    reasons = []
    if index_paths: reasons.append("index update queued or processing")
    # Stale files are reconciled in the background.  They remain visible in
    # diagnostics, but do not lock editing once no index work is active.
    if stale_paths and not index_paths: reasons.append("index refresh requested")
    return {"active": bool(index_paths), "reasons": reasons, "changes": len(change_paths), "indexing": len(index_paths), "stale": len(stale_paths)}


@app.post("/api/v79/tv/season-stream-values")
def season_stream_values(request: SeasonStreamRequest) -> dict:
    items = selected_episode_rows(request.paths)
    pending = []
    # Use the unified index fingerprint (filesystem mtime/size).  The legacy
    # TV table stores Plex catalog timestamps, which do not change when stream
    # metadata is edited and caused every season expansion to re-scan its files.
    with connection() as db:
        cached = {
            row["path"]: row for start in range(0, len(items), 800)
            for row in db.execute(
                f"SELECT path,modified_ns,size FROM media_stream_index_state WHERE path IN ({','.join('?' for _ in items[start:start + 800])})",
                [item["path"] for item in items[start:start + 800]],
            ).fetchall()
        }
    for item in items:
        old = cached.get(item["path"])
        try:
            stat = Path(item["path"]).stat()
            current = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            current = None
        if not old or current is None or (int(old["modified_ns"]), int(old["size"])) != current:
            pending.append(item)
    # Listing and filter expansion must remain read-only and responsive. The
    # old implementation indexed stale episodes synchronously here, which
    # caused long waits, request timeouts and competing writes while the user
    # was merely opening a season. Queue stale snapshots and let the next
    # refresh publish them atomically.
    queued = 0
    if pending:
        from app.v80 import enqueue_many
        queued = enqueue_many("core", pending, "Season filter requested a stale core index")
        logger.info(
            "tv_season_index event=stale_queued episodes=%d queued=%d",
            len(pending), queued,
        )
    errors = []
    paths = [item["path"] for item in items]
    values = []
    with connection() as db:
        for start in range(0, len(paths), 800):
            group = paths[start:start + 800]
            rows = db.execute(
                f"SELECT path,source,stream_type,type_index,external_path,language,region,track_name,is_default,is_forced,filename_tags FROM media_stream_index WHERE path IN ({','.join('?' for _ in group)})",
                group,
            ).fetchall()
            for row in rows:
                value = dict(row)
                value["language"] = comparable_language(value["language"])
                try:
                    value["filename_tags"] = json.loads(value["filename_tags"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    value["filename_tags"] = []
                values.append(value)
    logger.info(
        "tv_season_index event=ready episodes=%d inspected=0 queued=%d values=%d errors=0",
        len(items), queued, len(values),
    )
    return {
        "values": values,
        "episodes": len(items),
        "inspected": 0,
        "queued": queued,
        "pending": len(pending),
        "errors": errors,
    }


def comparable_language(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return plex_language_pair(normalized, "")[0]


def region_matches(stream: dict, expected: str | None) -> bool:
    if expected is None:
        return True
    actual = str(stream.get("region") or "").strip().upper()
    wanted = str(expected).strip().upper()
    return actual == wanted


def filter_matches(stream: dict, filters: SeasonStreamFilter) -> bool:
    language_region = f"{comparable_language(stream.get('language'))}|{str(stream.get('region') or '').strip().upper()}"
    selected_pairs = filters.language_regions
    pair_match = not selected_pairs or language_region in {
        f"{comparable_language(value.split('|', 1)[0])}|{value.split('|', 1)[1].strip().upper()}"
        for value in selected_pairs if '|' in value
    }
    return (
        ((filters.stream_types is not None and stream.get("codec_type") in filters.stream_types) or (filters.stream_types is None and (filters.stream_type is None or stream.get("codec_type") == filters.stream_type)))
        and pair_match
        and (filters.language_regions is not None or (filters.languages is not None and comparable_language(stream.get("language")) in {comparable_language(value) for value in filters.languages}) or (filters.languages is None and (filters.language is None or comparable_language(stream.get("language")) == comparable_language(filters.language))))
        and (filters.language_regions is not None or region_matches(stream, filters.region))
        and (filters.track_name is None or str(stream.get("title") or "").strip() == filters.track_name)
        and (filters.filename_tag is None or filters.filename_tag in (stream.get("filename_tags") or []))
    )


def episode_bulk_edit(path: str, request: SeasonStreamBulkEdit) -> tuple[dict, int]:
    details = media_details_with_ietf(path)
    tracks, external_changes, order, remove = [], [], [], []
    # Preserve tag state unless this request actually changes it. This lets
    # bulk preflight discard media that is already compliant.
    tags = {"default_audio": "__preserve__", "forced_audio": "__preserve__", "default_subtitle": "__preserve__", "forced_subtitle": "__preserve__"}
    current_tags = {"default_audio": None, "forced_audio": None, "default_subtitle": None, "forced_subtitle": None}
    matches = 0
    matched_keys: list[str] = []
    changed = set(request.changed_fields)
    targets = set(request.target_keys) if request.target_keys is not None else None
    for stream in details["streams"]:
        stream_type = stream["codec_type"]
        type_index = int(stream["type_index"])
        key = f"embedded:{stream_type}:{type_index}"
        order.append({"source": "embedded", "codec_type": stream_type, "type_index": type_index})
        if stream.get("default"):
            current_tags[f"default_{stream_type}"] = key
        if stream.get("forced"):
            current_tags[f"forced_{stream_type}"] = key
        if targets is not None and key not in targets:
            continue
        if targets is None and not filter_matches(stream, request.filters):
            continue
        matched_keys.append(key)
        if request.remove:
            remove.append(key)
            matches += 1
            continue
        update = {"codec_type": stream_type, "type_index": type_index}
        if "language" in changed or "region" in changed:
            update["language"] = request.language if "language" in changed else str(stream.get("language") or "")
            update["region"] = request.region if "region" in changed else str(stream.get("region") or "")
        if "track_name" in changed:
            update["title"] = request.track_name
        for flag, action in (("default", request.default_action), ("forced", request.forced_action)):
            if action == 'clear' and stream.get(flag):
                update[flag] = False
        if any(flag in update for flag in ('default','forced')) or any(update.get(field) != str(stream.get(source) or "") for field, source in (("language", "language"), ("region", "region"), ("title", "title")) if field in update):
            tracks.append(update)
        matches += 1
    for stream in details.get("external_subtitles", []):
        external_path = str(stream["path"])
        key = f"external:{external_path}"
        order.append({"source": "external", "codec_type": "subtitle", "path": external_path})
        comparable = {**stream, "codec_type": "external"}
        if targets is not None and key not in targets:
            continue
        if targets is None and not filter_matches(comparable, request.filters):
            continue
        if request.remove:
            remove.append(key)
            external_changes.append({
                "path": external_path, "embed": False,
                "language": str(stream.get("language") or "und"),
                "region": str(stream.get("region") or ""),
                "title": str(stream.get("title") or ""),
                "forced": bool(stream.get("forced")),
            })
        elif request.integrate:
            external_changes.append({
                "path": external_path, "embed": True,
                "language": request.language if "language" in changed else str(stream.get("language") or "und"),
                "region": request.region if "region" in changed else str(stream.get("region") or ""),
                "title": request.track_name if "track_name" in changed else str(stream.get("title") or ""),
                "forced": bool(stream.get("forced")),
            })
        matches += 1
    # For set/clear requests, the last matching stream of each type wins,
    # matching the stream-edit ordering rule.
    if request.default_action == "set":
        for stream_type in ("audio", "subtitle"):
            matches_for_type = [key for key in matched_keys if key.startswith(f"embedded:{stream_type}:")]
            if not matches_for_type: continue
            desired = f"embedded:{stream_type}:{matches_for_type[-1].rsplit(":", 1)[-1]}" if request.default_action == "set" and matches_for_type else None
            if any(bool(s.get('default')) != (f"embedded:{stream_type}:{s['type_index']}" == desired) for s in details['streams'] if s['codec_type']==stream_type):
                tags[f"default_{stream_type}"] = desired
        if request.filters.stream_type in {"audio", "subtitle"}:
            tags[f"default_{"subtitle" if request.filters.stream_type == "audio" else "audio"}"] = "__preserve__"
    if request.forced_action == "set":
        for stream_type in ("audio", "subtitle"):
            matches_for_type = [key for key in matched_keys if key.startswith(f"embedded:{stream_type}:")]
            if not matches_for_type: continue
            desired = f"embedded:{stream_type}:{matches_for_type[-1].rsplit(":", 1)[-1]}" if request.forced_action == "set" and matches_for_type else None
            if any(bool(s.get('forced')) != (f"embedded:{stream_type}:{s['type_index']}" == desired) for s in details['streams'] if s['codec_type']==stream_type):
                tags[f"forced_{stream_type}"] = desired
        if request.filters.stream_type in {"audio", "subtitle"}:
            tags[f"forced_{"subtitle" if request.filters.stream_type == "audio" else "audio"}"] = "__preserve__"
    return {"path": path, "tracks": tracks, "external_subtitles": external_changes, "order": order, **tags, "remove": remove}, matches


def edit_has_effective_changes(edit: dict) -> bool:
    return bool(edit.get("tracks") or edit.get("external_subtitles") or edit.get("remove") or edit.get('subtitle_color') or any(edit.get(name) != "__preserve__" for name in ("default_audio", "forced_audio", "default_subtitle", "forced_subtitle")))


def process_tv_filtered_stream_edit(task_id: int, payload: dict) -> dict:
    path = str(payload["path"])
    request_data = dict(payload["request"])
    # Preserve the user's selected execution mode before normalizing the
    # per-media request model.  Immediate edits must publish their refreshed
    # canonical stream snapshot before the task is reported complete; queued
    # edits continue to use the background index dependency.
    requested_mode = str(request_data.get("mode") or "queue")
    request_data["paths"] = [path]
    request_data["mode"] = "now"
    request = SeasonStreamBulkEdit.model_validate(request_data)
    episode = (re.search(r"(?:^|[^A-Za-z])(S\d{1,2}E\d{1,2})(?:[^A-Za-z]|$)", path, re.IGNORECASE) or [None, ""])[1].upper()
    prefix = f"Processing episode {episode} · " if episode else "Processing media · "
    tasks.update_progress(task_id, 0, 2, prefix + "Checking current streams")
    edit, matched = episode_bulk_edit(path, request)
    if not matched:
        tasks.update_progress(task_id, 2, 2, prefix + "No matching streams remain; no change needed")
        return {"path": path, "streams": 0, "skipped": True, "reason": "No current stream matches the queued filter"}
    if not edit_has_effective_changes(edit):
        tasks.update_progress(task_id, 2, 2, prefix + "Already compliant; no change needed")
        return {"path": path, "streams": matched, "skipped": True, "reason": "Media already has the requested values"}
    tasks.update_progress(task_id, 1, 2, prefix + f"Applying changes to {matched} matching stream(s)")
    result = optimized_media_edit(ReorderEditRequest.model_validate(edit))
    if requested_mode == "now":
        # Publish the affected episode's stream snapshot synchronously.  This
        # is intentionally limited to the edited media; language detection and
        # subtitle inspection remain scheduled/dependent work.  It prevents a
        # completed Apply-now operation from immediately reappearing as the
        # old filter value on the next bulk edit.
        try:
            stat = Path(path).stat()
            indexes.processors["core"]({"path": path, "modified": int(stat.st_mtime), "size": int(stat.st_size)})
            # optimized_media_edit may have registered a core dependency before
            # this synchronous publish. Retire only pending duplicates for this
            # media; a running worker is left untouched and remains idempotent.
            with connection() as db:
                db.execute("UPDATE index_task_queue SET status='cancelled', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP, error='Published synchronously by Apply now' WHERE job='core' AND path=? AND status='pending'", (path,))
            _queue_index_dependents(path, "core", "Immediate TV filtered stream edit completed")
        except Exception as exc:
            logger.warning("tv_filtered_edit event=immediate_index_refresh_failed path=%s error=%s", path.replace("\n", " ")[-300:], str(exc).replace("\n", " ")[-300:])
            raise
    else:
        from app.v80 import request_media_indexes
        request_media_indexes(path, ["core"], "Queued TV filtered stream edit completed")
    tasks.update_progress(task_id, 2, 2, prefix + "Stream changes applied and indexed")
    return {**result, "path": path, "streams": matched}


def indexed_target_keys(paths: list[str], filters: SeasonStreamFilter) -> dict[str, list[str]]:
    result = {path: [] for path in paths}
    with connection() as db:
        for start in range(0, len(paths), 800):
            group = paths[start:start + 800]
            rows = db.execute(
                f"SELECT path,source,stream_type,type_index,external_path,language,region,track_name,filename_tags FROM media_stream_index WHERE path IN ({', '.join('?' for _ in group)})",
                group,
            ).fetchall()
            for row in rows:
                value = dict(row)
                try:
                    value["filename_tags"] = json.loads(value["filename_tags"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    value["filename_tags"] = []
                stream = {"codec_type": value["stream_type"], "language": value["language"], "region": value["region"], "title": value["track_name"], "filename_tags": value["filename_tags"]}
                if not filter_matches(stream, filters):
                    continue
                key = f"external:{value['external_path']}" if value["source"] == "external" else f"embedded:{value['stream_type']}:{value['type_index']}"
                result[value["path"]].append(key)
    return result


def enqueue_tv_filtered_edits(paths: list[str], request: SeasonStreamBulkEdit) -> tuple[int, list[int]]:
    template = request.model_dump(exclude={"paths", "mode"})
    targets = indexed_target_keys(paths, request.filters)
    now = tasks.utc_now()
    task_ids: list[int] = []
    with connection() as db:
        for path in paths:
            if not targets[path]:
                continue
            per_media = {**template, "target_keys": targets[path]}
            task_type = 'tv_filtered_stream_edit_now' if request.mode == 'now' else 'tv_filtered_stream_edit'
            payload_data = tasks.attach_media_signature(task_type, {"path": path, "request": per_media})
            payload = json.dumps(payload_data, ensure_ascii=False, separators=(",", ":"))
            cursor = db.execute(
                "INSERT INTO task_queue(task_type,label,payload_json,status,progress_message,created_at,updated_at) VALUES(?,?,?,'pending','Waiting',?,?)",
                (task_type, f"TV filtered stream edit · {Path(path).name}", payload, now, now),
            )
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (cursor.lastrowid, path, now))
            task_ids.append(int(cursor.lastrowid))
    tasks.wake_queue()
    logger.info("tv_stream_bulk_edit event=batch_queued media=%d first_id=%s last_id=%s", len(task_ids), task_ids[0] if task_ids else "none", task_ids[-1] if task_ids else "none")
    return len(task_ids), task_ids


def preflight_tv_bulk_item(payload: dict, fingerprint: dict) -> dict:
    path = str(payload.get("path") or "")
    expected = payload.get("_preflight_fingerprint") or {}
    if not fingerprint.get("exists"):
        return {"decision": "invalid", "reason": "Media file is not accessible", "path": path}
    if expected and (int(expected.get("size", -1)) != int(fingerprint.get("size", -2)) or int(expected.get("mtime_ns", -1)) != int(fingerprint.get("mtime_ns", -2))):
        return {"decision": "stale", "reason": "Media changed after bulk request", "path": path}
    request_data = dict(payload.get("request") or {})
    request_data["paths"] = [path]
    request_data["mode"] = "now"
    try:
        request = SeasonStreamBulkEdit.model_validate(request_data)
        # The bulk endpoint already performed the indexed candidate pass.  Reuse
        # those keys during preflight instead of issuing one more database query
        # per media item.  The fallback keeps individual/legacy callers safe.
        targets = request_data.get("target_keys")
        if targets is None:
            targets = indexed_target_keys([path], request.filters).get(path, [])
        if not targets:
            return {"decision": "skipped", "reason": "No current stream matches the selected filter", "path": path}
        per_media = {**request.model_dump(exclude={"paths", "mode"}), "target_keys": targets}
        preview, matched = episode_bulk_edit(path, SeasonStreamBulkEdit.model_validate({**per_media, "paths": [path], "mode": "now"}))
        if not matched or not edit_has_effective_changes(preview):
            return {"decision": "skipped", "reason": "Media already has the requested values", "path": path}
        return {"decision": "approved", "path": path, "matched": matched, "request": per_media}
    except Exception as exc:
        return {"decision": "invalid", "reason": str(exc), "path": path}


def approve_tv_bulk(payload: dict, result: dict) -> dict:
    task_ids = []
    batch_items = []
    for item in payload.get("_bulk_items") or []:
        plan = dict((item.get("_preflight_result") or {}).get("request") or {})
        if not plan:
            continue
        batch_items.append({"path": str(item.get("path") or ""), "request": plan, "_media_signature": item.get("_preflight_fingerprint") or {}})
    if not batch_items:
        return {"task_ids": [], "queued": 0, "task_type": "tv_filtered_stream_edit_batch"}
    mode = str(((payload.get("_bulk_items") or [{}])[0].get("request") or {}).get("mode") or "queue")
    batch = tasks.enqueue("tv_filtered_stream_edit_batch", {"items": batch_items, "mode": mode}, f"TV filtered stream edit batch · {len(batch_items)} media", deduplicate=True)
    task_ids.append(batch["id"])
    return {"task_ids": task_ids, "queued": len(batch_items), "task_type": "tv_filtered_stream_edit_batch", "batch_task_id": batch["id"]}


def _batch_signature_matches(path: str, expected: dict) -> bool:
    try:
        stat = Path(path).stat()
    except OSError:
        return False
    return (not expected or expected.get("size") is None or int(expected["size"]) == stat.st_size) and (not expected or expected.get("mtime_ns") is None or int(expected["mtime_ns"]) == stat.st_mtime_ns)


def process_tv_filtered_stream_edit_batch(task_id: int, payload: dict) -> dict:
    """Run metadata-only edits under one scheduler task with per-media LUWs."""
    items = list(payload.get("items") or [])
    total = len(items)
    results = []
    for index, item in enumerate(items, 1):
        path = str(item.get("path") or "")
        try:
            tasks.update_progress(task_id, index - 1, total, f"Editing media {index}/{total}")
            expected = item.get("_media_signature") or {}
            if not _batch_signature_matches(path, expected):
                results.append({"path": path, "status": "stale", "reason": "Media changed between validation and execution"})
                continue
            raw_request = dict(item.get("request") or {})
            direct_edit = raw_request.get("direct_edit")
            if direct_edit:
                optimized_media_edit(ReorderEditRequest.model_validate(direct_edit))
                try:
                    from app.v80 import request_media_indexes
                    request_media_indexes(path, ["core", "subtitles"], "TV-show edit session committed")
                except Exception:
                    logger.exception("tv_edit_session event=index_request_failed path=%s", path)
                results.append({"path": path, "status": "succeeded"})
                continue
            request_data = raw_request
            request_data.update(paths=[path], mode="now")
            request = SeasonStreamBulkEdit.model_validate(request_data)
            edit, matched = episode_bulk_edit(path, request)
            if not matched or not edit_has_effective_changes(edit):
                results.append({"path": path, "status": "skipped", "streams": matched, "reason": "Already compliant or no matching stream"})
                continue
            luw_mode = "immediate" if str(payload.get("mode") or "queue") == "now" else "queued"
            luw_id = tasks.create_luw(resource_key=path, operation_type="tv_filtered_stream_edit_batch", mode=luw_mode, payload={"task_id": task_id, "path": path, "edit": edit, "batch_item": index}, input_signature=expected, idempotency_key=f"task:{task_id}:media:{index}", priority_class="user")
            try:
                if luw_id:
                    tasks.transition_luw(luw_id, "preflighted", "preflight", "Batch media signature accepted")
                    while not tasks.acquire_luw_lock(luw_id, path):
                        time.sleep(0.25)
                    tasks.transition_luw(luw_id, "applying", "apply", "Applying metadata-only edit")
                applied = optimized_media_edit(ReorderEditRequest.model_validate(edit))
                if luw_id:
                    tasks.transition_luw(luw_id, "verifying", "verify", "Metadata edit completed")
                    tasks.commit_luw(luw_id, tasks.media_configuration_signature(path))
                results.append({"path": path, "status": "succeeded", "streams": matched, "result": applied})
            except Exception:
                if luw_id:
                    try:
                        tasks.release_luw_lock(luw_id, path)
                        tasks.transition_luw(luw_id, "failed", "error", "Batch media edit failed")
                    except Exception:
                        logger.exception("batch_luw event=failure_persist_failed path=%s", path)
                raise
        except Exception as exc:
            results.append({"path": path, "status": "failed", "reason": str(exc)})
            logger.exception("tv_stream_bulk_batch event=media_failed path=%s", path)
    tasks.update_progress(task_id, total, total, f"Completed {total}/{total} media edits")
    return {"batch": True, "requested": total, "succeeded": sum(item["status"] == "succeeded" for item in results), "skipped": sum(item["status"] == "skipped" for item in results), "stale": sum(item["status"] == "stale" for item in results), "failed": sum(item["status"] == "failed" for item in results), "items": results}


def consolidated_tv_edit(path: str, journal: list[dict]) -> tuple[dict, int]:
    """Turn a virtual episode journal into one edit against its original stream IDs."""
    details = media_details_with_ietf(path)
    streams: dict[str, dict] = {}
    order: list[str] = []
    original_tags = {name: None for name in ("default_audio", "forced_audio", "default_subtitle", "forced_subtitle")}
    for stream in details["streams"]:
        kind, index = str(stream["codec_type"]), int(stream["type_index"])
        key = f"embedded:{kind}:{index}"
        streams[key] = {"codec_type": kind, "type_index": index, "language": str(stream.get("language") or ""), "region": str(stream.get("region") or ""), "title": str(stream.get("title") or ""), "source": "embedded"}
        streams[key].update({flag: bool(stream.get(flag)) for flag in ('default','forced')})
        order.append(key)
        for tag in ("default", "forced"):
            if stream.get(tag): original_tags[f"{tag}_{kind}"] = key
    for stream in details.get("external_subtitles", []):
        external_path = str(stream["path"])
        key = f"external:{external_path}"
        streams[key] = {"codec_type": "external", "path": external_path, "language": str(stream.get("language") or ""), "region": str(stream.get("region") or ""), "title": str(stream.get("title") or ""), "forced": bool(stream.get("forced")), "embed": False, "source": "external"}
        order.append(key)
    original = {key: value.copy() for key, value in streams.items()}
    original_order = order[:]
    tags = original_tags.copy()
    removed: set[str] = set()
    final_version = None
    subtitle_color = None
    audio_compatibility = []
    matched_total = 0
    for operation in journal:
        if operation.get('audio_compatibility'):
            audio_compatibility.extend(operation['audio_compatibility'])
            matched_total += len(operation['audio_compatibility'])
            continue
        direct = operation.get("direct_edit")
        if direct:
            if direct.get('subtitle_color'):
                subtitle_color = direct['subtitle_color']
            for change in direct.get("tracks") or []:
                key = f"embedded:{change.get('codec_type')}:{change.get('type_index')}"
                if key in streams:
                    for field in ("language", "region", "title"):
                        if field in change: streams[key][field] = str(change[field] or "")
            for change in direct.get("external_subtitles") or []:
                key = f"external:{change.get('path')}"
                if key in streams:
                    for field in ("language", "region", "title"):
                        if field in change: streams[key][field] = str(change[field] or "")
                    if "embed" in change: streams[key]["embed"] = bool(change["embed"])
            requested_order = [f"external:{item.get('path')}" if item.get("source") == "external" else f"embedded:{item.get('codec_type')}:{item.get('type_index')}" for item in direct.get("order") or []]
            if requested_order: order = list(dict.fromkeys([key for key in requested_order if key in streams] + [key for key in order if key not in requested_order]))
            removed = {str(key) for key in direct.get("remove") or [] if str(key) in streams}
            for name in tags:
                if name in direct and direct[name] != "__preserve__":
                    tags[name] = direct[name]
                    flag, kind = name.split('_', 1)
                    for key, stream in streams.items():
                        if stream['source']=='embedded' and stream['codec_type']==kind:
                            stream[flag] = key == direct[name]
            if direct.get("final_version") is not None: final_version = bool(direct["final_version"])
            matched_total += len(direct.get("tracks") or []) + len(direct.get("external_subtitles") or [])
            continue
        request = SeasonStreamBulkEdit.model_validate({**operation, "paths": [path], "mode": "now"})
        filters = request.filters
        targets = set(request.target_keys) if request.target_keys is not None else None
        matches = []
        for key, stream in streams.items():
            if key in removed: continue
            comparable = {**stream, "codec_type": stream["codec_type"], "filename_tags": []}
            if (targets is not None and key in targets) or (targets is None and filter_matches(comparable, filters)):
                matches.append(key)
        matched_total += len(matches)
        for key in matches:
            if request.remove:
                removed.add(key)
                continue
            stream = streams[key]
            if "language" in request.changed_fields: stream["language"] = request.language
            if "region" in request.changed_fields: stream["region"] = request.region
            if "track_name" in request.changed_fields: stream["title"] = request.track_name
            if stream["source"] == "external" and request.integrate: stream["embed"] = True
        for action, tag in ((request.default_action, "default"), (request.forced_action, "forced")):
            if action == "unchanged": continue
            for kind in ("audio", "subtitle"):
                eligible = [key for key in matches if key.startswith(f"embedded:{kind}:")]
                if not eligible: continue
                if action == 'set':
                    for key, stream in streams.items():
                        if stream['source']=='embedded' and stream['codec_type']==kind:
                            stream[tag] = key == eligible[-1]
                else:
                    for key in eligible: streams[key][tag] = False
    track_changes = [{field: stream[field] for field in ("codec_type", "type_index", "language", "region", "title")} for key, stream in streams.items() if stream["source"] == "embedded" and key not in removed and any(stream[field] != original[key][field] for field in ("language", "region", "title"))]
    for key, stream in streams.items():
        if stream['source']!='embedded' or key in removed: continue
        flags = {flag:stream[flag] for flag in ('default','forced') if stream[flag]!=original[key][flag] or tags[f'{flag}_{stream["codec_type"]}']!=original_tags[f'{flag}_{stream["codec_type"]}']}
        if not flags: continue
        change = next((item for item in track_changes if item['codec_type']==stream['codec_type'] and item['type_index']==stream['type_index']), None)
        if change is None:
            change = {'codec_type':stream['codec_type'],'type_index':stream['type_index']}
            track_changes.append(change)
        change.update(flags)
    external_changes = [{field: stream[field] for field in ("path", "embed", "language", "region", "title", "forced")} for key, stream in streams.items() if stream["source"] == "external" and (key in removed or any(stream[field] != original[key][field] for field in ("embed", "language", "region", "title")))]
    ordered = [{"source": streams[key]["source"], "codec_type": "subtitle" if streams[key]["source"] == "external" else streams[key]["codec_type"], **({"path": streams[key]["path"]} if streams[key]["source"] == "external" else {"type_index": streams[key]["type_index"]})} for key in order]
    changed_tags = {name: tags[name] if tags[name] != original_tags[name] else "__preserve__" for name in tags}
    edit = {"path": path, "tracks": track_changes, "external_subtitles": external_changes, "order": ordered, **changed_tags, "remove": sorted(removed), "final_version": final_version, "subtitle_color": subtitle_color}
    if audio_compatibility:
        edit['audio_compatibility'] = audio_compatibility
    structural = removed or order != original_order or any(item["embed"] for item in external_changes)
    if not (audio_compatibility or track_changes or external_changes or structural or any(value != "__preserve__" for value in changed_tags.values()) or final_version is not None or subtitle_color):
        return {}, matched_total
    return edit, matched_total


def _commit_tv_note_changes(show_id: str, path: str, changes: dict) -> None:
    """Apply journaled notes only after that episode's media edit succeeds."""
    show_key = f"@show:{show_id}"
    is_show = path == show_key
    if not is_show and not Path(path).is_file():
        raise FileNotFoundError(path)
    with connection() as db:
        if is_show:
            library_key, show_title = show_id.split(":", 1)
            episodes = db.execute(
                "SELECT path FROM plex_media WHERE kind='episode' AND library_key=? AND show_title=?",
                (library_key, show_title),
            ).fetchall()
            if not episodes:
                raise ValueError("TV show no longer has indexed episodes")
        else:
            media = db.execute("SELECT library_key,show_title,kind FROM plex_media WHERE path=?", (path,)).fetchone()
            if not media or media["kind"] != "episode" or f"{media['library_key']}:{media['show_title'] or 'Unknown show'}" != show_id:
                raise ValueError("Episode no longer belongs to this TV show")
        entity_key = show_id if is_show else f"episode:{path}"
        current = db.execute(
            "SELECT note,reviewed,plex_sync_change,final_version FROM media_notes WHERE entity_type='tv' AND entity_key=?",
            (entity_key,),
        ).fetchone()
        note = str(changes.get("note", current["note"] if current else "") or "").strip()
        reviewed = bool(changes.get("reviewed", current["reviewed"] if current else False))
        plex_change = bool(current["plex_sync_change"]) if current else False
        final = bool(changes.get("final_version", current["final_version"] if current else False))
        reviewed = reviewed or final
        db.execute(
            "INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) "
            "VALUES('tv',?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(entity_type,entity_key) "
            "DO UPDATE SET note=excluded.note,reviewed=excluded.reviewed,plex_sync_change=excluded.plex_sync_change,"
            "final_version=excluded.final_version,updated_at=CURRENT_TIMESTAMP",
            (entity_key, note, int(reviewed), int(plex_change), int(final)),
        )
        if is_show and "final_version" in changes:
            for episode in episodes:
                episode_key = f"episode:{episode['path']}"
                db.execute(
                    "INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) "
                    "VALUES('tv',?,'',?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(entity_type,entity_key) "
                    "DO UPDATE SET reviewed=CASE WHEN excluded.final_version=1 THEN 1 ELSE media_notes.reviewed END,"
                    "final_version=excluded.final_version,updated_at=CURRENT_TIMESTAMP",
                    (episode_key, int(final), 0, int(final)),
                )
        elif not is_show and "final_version" in changes:
            total = int(db.execute("SELECT count(*) AS n FROM plex_media WHERE kind='episode' AND library_key=? AND show_title=?", (media["library_key"], media["show_title"])).fetchone()["n"] or 0)
            final_count = int(db.execute(
                "SELECT count(*) AS n FROM media_notes n JOIN plex_media p ON p.path=substr(n.entity_key,9) "
                "WHERE n.entity_type='tv' AND n.final_version=1 AND p.kind='episode' AND p.library_key=? AND p.show_title=?",
                (media["library_key"], media["show_title"]),
            ).fetchone()["n"] or 0)
            parent_final = total > 0 and final_count >= total
            db.execute(
                "INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at) "
                "VALUES('tv',?,'',?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(entity_type,entity_key) "
                "DO UPDATE SET reviewed=CASE WHEN excluded.final_version=1 THEN 1 ELSE media_notes.reviewed END,"
                "final_version=excluded.final_version,updated_at=CURRENT_TIMESTAMP",
                (show_id, int(parent_final), 0, int(parent_final)),
            )
        if final:
            from app.detection_policy import retire_final_detection
            retire_final_detection(db)
    if changes.get("final_version") is True:
        from app.subtitle_cache import prioritize_final_media
        prioritize_final_media(
            [str(episode["path"]) for episode in episodes] if is_show else [path]
        )


def process_tv_edit_session_commit(task_id: int, payload: dict) -> dict:
    _ensure_tv_edit_sessions()
    session_id = str(payload.get('session_id') or '')
    token = tv_commit_owner.set(session_id)
    try:
        with connection() as db:
            db.execute("UPDATE tv_edit_sessions SET status='committing',commit_task_id=?,updated_at=? WHERE session_id=?", (task_id,_tv_edit_now(),session_id))
        return _process_tv_edit_session_commit(task_id,payload)
    except Exception as exc:
        with connection() as db:
            db.execute("UPDATE tv_edit_sessions SET status='open',updated_at=?,error=? WHERE session_id=?", (_tv_edit_now(),str(exc)[:2000],session_id))
        raise
    finally:
        tv_commit_owner.reset(token)


def _process_tv_edit_session_commit(task_id: int, payload: dict) -> dict:
    """Commit a TV-show draft as consolidated, per-episode LUWs."""
    session_id = str(payload.get("session_id") or "")
    tasks.update_progress(task_id, 0, 0, 'Loading saved TV-show draft; preparing episode changes')
    items = load_tv_commit_journal(session_id) if payload.get('journal_reference') else list(payload.get("items") or [])
    show_id = str(payload.get("show_id") or "")
    show_final_changes = [
        operation["note_edit"]["final_version"]
        for entry in items if entry.get("path") == f"@show:{show_id}"
        for operation in entry.get("journal") or []
        if isinstance(operation.get("note_edit"), dict) and "final_version" in operation["note_edit"]
    ]
    show_unfreeze = bool(show_final_changes) and show_final_changes[-1] is False
    results = []
    total = len(items)
    for index, item in enumerate(items, 1):
        path = str(item.get("path") or "")
        try:
            tasks.update_progress(task_id, index - 1, total, f"Saving TV-show edit {index}/{total}")
            if path == f"@show:{show_id}" and any(item["status"] in {"failed", "stale"} for item in results):
                results.append({"path": path, "status": "stale", "reason": "Show status was not saved because an episode change failed"})
                continue
            expected = item.get("_media_signature") or {}
            if path != f"@show:{show_id}" and not _batch_signature_matches(path, expected):
                results.append({"path": path, "status": "stale", "reason": "Media changed while the TV-show draft was open"})
                continue
            journal = list(item.get("journal") or []) or [dict(item.get("request") or {})]
            note_changes = {}
            media_journal = []
            for operation in journal:
                if isinstance(operation.get("note_edit"), dict):
                    note_changes.update(operation["note_edit"])
                else:
                    media_journal.append(operation)
            if path == f"@show:{show_id}" and media_journal:
                raise ValueError("A TV-show status entry cannot edit a media file")
            edit, matched_total = consolidated_tv_edit(path, media_journal) if media_journal else ({}, 0)
            if edit:
                if show_unfreeze or note_changes.get("final_version") is False:
                    edit["final_version"] = False
                optimized_media_edit(ReorderEditRequest.model_validate(edit))
                try:
                    from app.v80 import request_media_indexes
                    request_media_indexes(path, ["core", "subtitles"] if edit["remove"] or edit["external_subtitles"] or any(order_item["source"] == "external" for order_item in edit["order"]) else ["core"], "TV-show edit session committed")
                except Exception:
                    logger.exception("tv_edit_session event=index_request_failed path=%s", path)
            if note_changes:
                _commit_tv_note_changes(show_id, path, note_changes)
            results.append({"path": path, "status": "succeeded" if edit or note_changes else "skipped", "streams": matched_total})
            if payload.get('journal_reference'):
                with connection() as db:
                    db.execute('DELETE FROM tv_edit_operations WHERE session_id=? AND path=?', (session_id,path))
        except Exception as exc:
            logger.exception("tv_edit_session event=media_failed path=%s", path)
            results.append({"path": path, "status": "failed", "reason": str(exc)})
    # A stale signature is not a successful commit: keep the draft journal so
    # the user can inspect the changed media and explicitly retry or discard
    # it.  Treating stale entries as committed used to unlock the show while
    # silently deleting the only recovery plan.
    failed = [item for item in results if item["status"] in {"failed", "stale"}]
    now = _tv_edit_now()
    with connection() as db:
        if failed:
            db.execute("UPDATE tv_edit_sessions SET status='open',updated_at=?,error=? WHERE session_id=?", (now, f"{len(failed)} media failed during save", session_id))
        else:
            db.execute("UPDATE tv_edit_sessions SET status='committed',committed_at=?,updated_at=?,error=NULL WHERE session_id=?", (now, now, session_id))
            db.execute("DELETE FROM tv_edit_operations WHERE session_id=?", (session_id,))
    tasks.update_progress(task_id, total, total, f"TV-show edit saved · {total - len(failed)}/{total} media complete")
    return {"session_id": session_id, "requested": total, "succeeded": sum(item["status"] == "succeeded" for item in results), "skipped": sum(item["status"] == "skipped" for item in results), "stale": sum(item["status"] == "stale" for item in results), "failed": len(failed), "items": results}


register_handler("tv_stream_edit_bulk", preflight_tv_bulk_item)
register_approval_handler("tv_stream_edit_bulk", approve_tv_bulk)

tasks.TASK_HANDLERS["tv_filtered_stream_edit"] = process_tv_filtered_stream_edit
tasks.TASK_HANDLERS["tv_filtered_stream_edit_now"] = process_tv_filtered_stream_edit
tasks.TASK_HANDLERS["tv_filtered_stream_edit_batch"] = process_tv_filtered_stream_edit_batch
tasks.TASK_HANDLERS["tv_edit_session_commit"] = process_tv_edit_session_commit

@app.post("/api/v79/tv/season-stream-bulk-edit")
def season_stream_bulk_edit(request: SeasonStreamBulkEdit) -> dict:
    request.paths = list(dict.fromkeys(request.paths))
    selected_episode_rows(request.paths)
    changed = set(request.changed_fields)
    if request.filters.stream_type == "external":
        if request.integrate and request.remove:
            raise HTTPException(400, "External subtitles cannot be integrated and removed in the same operation")
        if changed.intersection({"language", "region", "track_name"}) and not request.integrate:
            raise HTTPException(400, "Integrate must be selected to save properties on external subtitles")
        if not request.integrate and not request.remove:
            raise HTTPException(400, "Select Integrate or Remove for matching external subtitles")
    elif request.integrate:
        raise HTTPException(400, "Integrate is available only for external subtitles")
    if request.remove and changed.intersection({"language", "region", "track_name"}):
        raise HTTPException(400, "Remove cannot be combined with metadata changes")
    if "track_name" in changed and request.filters.language is None and not request.filters.language_regions:
        raise HTTPException(400, "Select a language and region before changing track names in bulk")
    # Use the current stream index as a cheap candidate pass before queuing the
    # detailed preflight. The preflight still re-reads the media and verifies
    # its signature, but it no longer probes every episode in a whole show when
    # only a small subset can match the selected filters.
    indexed_targets = indexed_target_keys(request.paths, request.filters)
    candidate_paths = indexed_effective_target_paths(request.paths, request, indexed_targets)
    if not candidate_paths:
        reason = "No indexed streams match the selected filter"
        if any(indexed_targets.values()):
            reason = "All matching streams already have the requested values"
        logger.info(
            "tv_stream_bulk_edit event=noop requested_media=%d indexed_candidates=%d reason=%s",
            len(request.paths), sum(bool(value) for value in indexed_targets.values()), reason,
        )
        return {"mode": request.mode, "queued": 0, "candidates": 0, "requested_media": len(request.paths), "preflight": False, "preflight_id": None, "task_ids": [], "applied": 0, "streams": 0, "skipped": [{"reason": reason, "media": len(request.paths)}], "failed": []}
    request_payload = request.model_dump(exclude={"paths"})
    items = [
        {
            "path": path,
            "request": {**request_payload, "target_keys": indexed_targets.get(path, [])},
        }
        for path in candidate_paths
    ]
    preflight = enqueue_bulk_preflight("tv_stream_edit_bulk", items, mode="immediate" if request.mode == "now" else "queued", priority=80, deduplicate=True)
    return {"mode": request.mode, "queued": len(items), "candidates": len(candidate_paths), "requested_media": len(request.paths), "preflight": True, "preflight_id": preflight["id"], "task_ids": [], "applied": 0, "streams": 0, "skipped": [], "failed": []}
