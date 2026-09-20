from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v7 as media_editor
import app.v54 as indexes
from app.postgres_store import (
    acquire_luw_lock,
    append_luw_journal,
    begin_task_stage,
    cleanup_succeeded_workflow_group_artifacts,
    commit_luw,
    commit_task_artifacts,
    create_luw,
    fail_task_stage,
    finish_task_stage,
    mark_read_models_fresh,
    prepare_task_artifact,
    register_task_stage,
    release_luw_lock,
    reset_task_stage_for_retry,
    task_stage_exists,
    transition_luw,
    workflow_details,
)
from app.postgres_store import (
    cleanup_succeeded_workflow_artifacts as cleanup_succeeded_workflow_artifacts_startup,
    cleanup_orphaned_workflow_artifacts as cleanup_orphaned_workflow_artifacts_startup,
)
from app.v5 import external_subtitles
from app.v7 import ReorderEditRequest
from app.v11 import column_exists, connection
from app.v37 import MediaRenameRequest, rename_media
from app.v43 import optimized_media_edit
from app.v64 import app

logger = logging.getLogger("uvicorn.error")
queue_condition = threading.Condition()
queue_thread: threading.Thread | None = None
queue_light_thread: threading.Thread | None = None
queue_shutdown = threading.Event()
LIGHT_TASK_TYPES = ("index_check_prepare", "index_rebuild_prepare")
# These jobs are protected maintenance work. User ordering still controls
# user-requested operations, while old maintenance work receives an aging
# allowance so it cannot be starved indefinitely.
SYSTEM_TASK_TYPES = frozenset({
    "plex_sync", "audio_language_detection", "media_reindex",
    "index_check_prepare", "index_rebuild_prepare",
})
SYSTEM_AGING_SECONDS = 600


class QueueRequest(BaseModel):
    task_type: Literal["media_edit", "movie_import"]
    payload: dict[str, Any]
    label: str = ""


class QueueAction(BaseModel):
    action: Literal["pause", "resume"]


class QueueStatusRequest(BaseModel):
    task_ids: list[int] = Field(min_length=1, max_length=30000)


class QueueExpediteRequest(BaseModel):
    minutes: int = Field(default=60, ge=5, le=1440)


class QueueExpediteMatchingRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    minutes: int = Field(default=60, ge=5, le=1440)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scheduler_system_cutoff() -> str:
    return datetime.fromtimestamp(time.time() - SYSTEM_AGING_SECONDS, tz=timezone.utc).isoformat(timespec="seconds")


def queue_paused() -> bool:
    with connection() as db:
        row = db.execute("SELECT value FROM task_queue_settings WHERE key='paused'").fetchone()
    return bool(row and row[0] == "1")


def task_row(task_id: int) -> dict:
    with connection() as db:
        row = db.execute("SELECT * FROM task_queue WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Queue item not found")
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def wake_queue() -> None:
    with queue_condition:
        queue_condition.notify_all()


def clear_expired_expedites(db) -> None:
    """Remove temporary boosts so an occasional priority decision cannot become permanent."""
    db.execute("DELETE FROM task_queue_expedite WHERE expires_at <= ?", (utc_now(),))


def _active_expedite_expiry(db, group_id: str | None = None, path: str | None = None) -> str | None:
    """Return the latest active expedite inherited by a group or media path."""
    clear_expired_expedites(db)
    candidates = db.execute("SELECT group_id,expires_at,task_id FROM task_queue_expedite WHERE expires_at > ?", (utc_now(),)).fetchall()
    best = None
    for row in candidates:
        if group_id and row["group_id"] and str(row["group_id"]) == str(group_id):
            best = max(best or str(row["expires_at"]), str(row["expires_at"]))
            continue
        if path:
            task = db.execute("SELECT task_type,payload_json FROM task_queue WHERE id=?", (row["task_id"],)).fetchone()
            try:
                if task and affected_media_path(str(task["task_type"]), json.loads(task["payload_json"])) == path:
                    best = max(best or str(row["expires_at"]), str(row["expires_at"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return best


def affected_media_path(task_type: str, payload: dict) -> str:
    if task_type == "media_edit":
        return str((payload.get("edit") or payload).get("path") or "")
    if task_type == "subtitle_html_cleanup":
        return str(payload.get("path") or "")
    if task_type in {"tv_filtered_stream_edit", "tv_filtered_stream_edit_now", "filtered_stream_edit", "filtered_stream_edit_now", "plex_import_refresh"}:
        return str(payload.get("path") or "")
    if task_type == "movie_import":
        return str(payload.get("source") or "")
    if task_type == "media_reindex":
        return str(payload.get("path") or "")
    if task_type == "audio_language_detection":
        return str(payload.get("path") or "")
    return ""


SIGNATURE_EXCLUDED_TASKS = {"audio_language_detection", "media_reindex", "index_check_prepare", "index_rebuild_prepare", "plex_sync", "plex_import_refresh"}

def media_configuration_signature(path: str) -> dict:
    media = Path(path).resolve()
    if not media.is_file():
        return {"path": str(media), "missing": True, "digest": ""}
    stat = media.stat()
    try:
        probe = media_editor.probe(media)
        streams = []
        for stream in probe.get("streams", []):
            streams.append({
                "index": stream.get("index"), "codec_type": stream.get("codec_type"),
                "codec_name": stream.get("codec_name"), "codec_long_name": stream.get("codec_long_name"),
                "tags": stream.get("tags") or {}, "disposition": stream.get("disposition") or {},
                "width": stream.get("width"), "height": stream.get("height"),
            })
        sidecars = []
        for item in external_subtitles(media):
            sidecar = Path(str(item.get("path") or item.get("external_path") or "")).resolve()
            try:
                side_stat = sidecar.stat()
                sidecars.append({"path": str(sidecar), "size": side_stat.st_size, "mtime_ns": side_stat.st_mtime_ns, "metadata": item})
            except OSError:
                sidecars.append({"path": str(sidecar), "missing": True, "metadata": item})
        canonical = json.dumps({"stat": [stat.st_size, stat.st_mtime_ns], "format": probe.get("format") or {}, "streams": streams, "sidecars": sidecars}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return {"path": str(media), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "digest": digest}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not capture media signature: {exc}") from exc

def attach_media_signature(task_type: str, payload: dict, group_id: str | None = None) -> dict:
    if task_type in SIGNATURE_EXCLUDED_TASKS or payload.get("_media_signature"):
        return payload
    path = affected_media_path(task_type, payload)
    if not path:
        return payload
    signed = dict(payload)
    signed["_queue_group"] = group_id or str(payload.get("_queue_group") or uuid.uuid4().hex)
    expected = None
    if signed["_queue_group"]:
        with connection() as db:
            row = db.execute("SELECT signature_json FROM task_queue_group WHERE group_id=?", (signed["_queue_group"],)).fetchone()
        if row and row["signature_json"]:
            expected = json.loads(row["signature_json"])
    signed["_media_signature"] = expected or media_configuration_signature(path)
    return signed

def validate_media_signature(task_type: str, payload: dict) -> None:
    expected = payload.get("_media_signature")
    group_id = payload.get("_queue_group")
    if group_id:
        with connection() as db:
            row = db.execute("SELECT signature_json FROM task_queue_group WHERE group_id=?", (group_id,)).fetchone()
        if row and row["signature_json"]:
            expected = json.loads(row["signature_json"])
    if not expected or task_type in SIGNATURE_EXCLUDED_TASKS:
        return
    path = str(expected.get("path") or affected_media_path(task_type, payload) or "")
    current = media_configuration_signature(path)
    if current.get("digest") != expected.get("digest") or bool(current.get("missing")) != bool(expected.get("missing")):
        raise RuntimeError("Media changed between queueing and execution; job refused for safety")

def _create_media_luw(task_id: int, task_type: str, payload: dict, path: str) -> str | None:
    destructive_types = {"media_edit", "filtered_stream_edit", "filtered_stream_edit_now", "tv_filtered_stream_edit", "tv_filtered_stream_edit_now", "movie_import", "subtitle_html_cleanup", "image_subtitle_convert", "ocr_rollback", "plex_import_refresh"}
    if os.getenv("DATABASE_BACKEND", "sqlite").lower() != "postgres" or task_type not in destructive_types or not path:
        return None
    existing = payload.get("_luw_id")
    if existing:
        return str(existing)
    mode = "immediate" if str(task_type).endswith("_now") or payload.get("mode") == "now" else "queued"
    resource_key = path
    if task_type == "movie_import":
        resource_key = f"{path} -> {payload.get('destination') or ''}/{payload.get('filename') or ''}".rstrip("/")
    edit = payload.get("edit") or payload
    if task_type == "movie_import":
        rollback_strategy = "atomic-copy-target-and-retain-source"
    elif task_type in {"image_subtitle_convert", "ocr_rollback", "subtitle_html_cleanup"}:
        rollback_strategy = "staged-original-until-review-or-commit"
    elif edit.get("remove") or edit.get("order") or edit.get("external_subtitles"):
        rollback_strategy = "full-original-until-verified"
    else:
        rollback_strategy = "metadata-journal-plus-original-until-verified"
    payload["_luw_strategy"] = rollback_strategy
    luw_id = create_luw(
        resource_key=resource_key,
        operation_type=task_type,
        mode=mode,
        payload={"task_id": task_id, "source": payload.get("source"), "destination": payload.get("destination"), "filename": payload.get("filename"), "edit": payload.get("edit") or {}, "rollback_strategy": rollback_strategy, "queue_group": payload.get("_queue_group")},
        input_signature=payload.get("_media_signature") or {},
        group_id=payload.get("_queue_group"),
        idempotency_key=f"task:{task_id}",
        priority_class="user",
    )
    if luw_id:
        payload["_luw_id"] = luw_id
        append_luw_journal(
            luw_id,
            "operation_plan",
            path,
            {"media_signature": payload.get("_media_signature") or {}, "rollback_strategy": rollback_strategy},
            {"edit": payload.get("edit") or {}, "destination": payload.get("destination"), "filename": payload.get("filename"), "rollback_strategy": rollback_strategy},
        )
    return luw_id

def advance_media_signature(payload: dict) -> None:
    group_id = payload.get("_queue_group")
    if not group_id:
        return
    with connection() as db:
        row = db.execute("SELECT path FROM task_queue_group WHERE group_id=?", (group_id,)).fetchone()
    path = str(row["path"] if row and row["path"] else "")
    if not path:
        path = affected_media_path("media_edit", payload) or affected_media_path("subtitle_html_cleanup", payload) or str((payload.get("_media_signature") or {}).get("path") or "")
    if not path:
        return
    signature = media_configuration_signature(path)
    with connection() as db:
        db.execute("UPDATE task_queue_group SET signature_json=?,updated_at=? WHERE group_id=?", (json.dumps(signature, ensure_ascii=False), utc_now(), group_id))

def enqueue(task_type: str, payload: dict, label: str = "", *, deduplicate: bool = False) -> dict:
    affected = affected_media_path(task_type, payload)
    group_id = str(payload.get("_queue_group") or "")
    if affected and not group_id and task_type not in SIGNATURE_EXCLUDED_TASKS:
        with connection() as lookup:
            row = lookup.execute("SELECT task_queue.group_id FROM media_change_request JOIN task_queue ON task_queue.id=media_change_request.task_id WHERE media_change_request.path=? AND task_queue.group_id IS NOT NULL AND task_queue.status IN ('pending','running') ORDER BY task_queue.id LIMIT 1", (affected,)).fetchone()
        group_id = str(row[0]) if row else uuid.uuid4().hex
    if not group_id:
        group_id = uuid.uuid4().hex
    payload = dict(payload)
    payload["_queue_group"] = group_id
    payload = attach_media_signature(task_type, payload, group_id)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with connection() as db:
        if deduplicate:
            row = db.execute(
                "SELECT id FROM task_queue WHERE task_type=? AND payload_json=? AND status IN ('pending','running') ORDER BY id LIMIT 1",
                (task_type, encoded),
            ).fetchone()
            if row:
                return task_row(row[0])
        cursor = db.execute(
            "INSERT INTO task_queue(task_type,label,payload_json,group_id,status,progress_message,created_at,updated_at) VALUES(?,?,?,?, 'pending','Waiting',?,?)",
            (task_type, label.strip() or task_type.replace("_", " ").title(), encoded, payload.get("_queue_group"), utc_now(), utc_now()),
        )
        task_id = cursor.lastrowid
        if payload.get("_queue_group") and affected:
            db.execute("INSERT OR IGNORE INTO task_queue_group(group_id,path,signature_json,updated_at) VALUES(?,?,?,?)", (payload["_queue_group"], affected, json.dumps(payload.get("_media_signature") or {}, ensure_ascii=False), utc_now()))
        affected = affected_media_path(task_type, payload)
        if affected:
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (task_id, affected, utc_now()))
        # An expedited parent/workflow carries its temporary boost to any child
        # task created later for the same group or media.
        inherited = _active_expedite_expiry(db, group_id, affected)
        if inherited:
            db.execute("INSERT OR REPLACE INTO task_queue_expedite(task_id,group_id,requested_at,expires_at) VALUES(?,?,?,?)", (task_id, group_id, utc_now(), inherited))
            logger.info("task_queue event=expedite_inherited id=%d group=%s file=%s expires=%s", task_id, group_id, affected or "", inherited)
    try:
        register_task_stage(payload.get("_queue_group"), task_type, affected, payload, task_id)
    except Exception as exc:
        logger.exception("workflow event=stage_registration_failed task=%s error=%s", task_type, str(exc))
        raise
    # Detection invalidation is performed by the executing operation with a
    # stream-aware scope. Queue insertion itself must not erase valid results
    # for unrelated streams while a task waits behind other work.
    logger.info("task_queue event=added id=%d type=%s label=%s", task_id, task_type, (label or "").replace("\n", "\\n"))
    wake_queue()
    return task_row(task_id)


def update_progress(task_id: int, completed: int, total: int, message: str) -> None:
    with connection() as db:
        db.execute(
            "UPDATE task_queue SET progress_current=?,progress_total=?,progress_message=?,updated_at=? WHERE id=?",
            (completed, total, message, utc_now(), task_id),
        )


def process_media_reindex(task_id: int, payload: dict) -> dict:
    requested = str(payload.get("path") or "")
    path = Path(requested)
    if not path.is_file():
        raise RuntimeError(f"Media file is not accessible: {requested}")
    stat = path.stat()
    with connection() as db:
        row = db.execute("SELECT path,title,kind FROM plex_media WHERE path=?", (requested,)).fetchone()
        if not row:
            raise RuntimeError("Media is not in the synchronized Plex catalog")
        db.execute("UPDATE plex_media SET modified=?,size=? WHERE path=?", (int(stat.st_mtime), stat.st_size, requested))
    item = {"path": requested, "title": row["title"], "modified": int(stat.st_mtime), "size": stat.st_size}
    requested_indexes = payload.get("indexes") or ("core", "subtitles", "previews")
    names = tuple(name for name in ("core", "subtitles", "previews") if name in requested_indexes)
    if not names:
        raise RuntimeError("No valid media indexes were requested")
    for number, name in enumerate(names, 1):
        update_progress(task_id, number - 1, len(names), f"Updating {name} index")
        indexes.processors[name](item)
        family = {"core": "common", "subtitles": "subtitles", "previews": None}.get(name)
        if family:
            mark_read_models_fresh(requested, family, {"modified": int(stat.st_mtime), "size": stat.st_size})
        logger.info("task_queue event=item_progress id=%d type=media_reindex step=%d total=%d index=%s file=%s", task_id, number, len(names), name, requested.replace("\n", "\\n"))
    update_progress(task_id, len(names), len(names), "All media indexes updated")
    return {"path": requested, "indexes": list(names)}


def process_media_edit(task_id: int, payload: dict) -> dict:
    edit_payload = payload.get("edit") or payload
    update_progress(task_id, 0, 2, "Applying stream changes")
    result = optimized_media_edit(ReorderEditRequest.model_validate(edit_payload))
    final_path = result.get("edited") or edit_payload["path"]
    filename = str(payload.get("filename") or "").strip()
    renamed = False
    if filename and filename != Path(final_path).name:
        update_progress(task_id, 1, 2, "Renaming media")
        final_path = rename_media(MediaRenameRequest(path=final_path, filename=filename))["path"]
        renamed = True
    update_progress(task_id, 2, 2, "Media changes applied")
    # optimized_media_edit already schedules the exact detector/index families
    # for the edit. Only follow up for operations performed outside that editor:
    # HTML cleanup is applied by the wrapper before editing, and a rename changes
    # the catalog identity without changing stream content.
    from app.v80 import detection_scope_for_operation, request_media_indexes
    if payload.get("html_cleanups"):
        from app.v68 import register_internal_change_scope
        register_internal_change_scope(final_path, detection_scope_for_operation("subtitle_content"), "Queued HTML cleanup completed")
        request_media_indexes(
            final_path,
            ["subtitles"],
            "Queued HTML cleanup completed",
            detection_scope=detection_scope_for_operation("subtitle_content"),
        )
    elif renamed:
        request_media_indexes(final_path, ["core"], "Queued media rename completed")
    return {**result, "path": final_path}


def _audio_language_code(value: str) -> str:
    key = str(value or '').strip().casefold().replace('_', '-')
    aliases = {'por': 'pt', 'pt-br': 'pt', 'pt-pt': 'pt', 'eng': 'en', 'jpn': 'ja', 'spa': 'es', 'fre': 'fr', 'fra': 'fr', 'ger': 'de', 'deu': 'de', 'ita': 'it', 'rus': 'ru', 'zho': 'zh', 'chi': 'zh', 'kor': 'ko'}
    return aliases.get(key, key.split('-', 1)[0])


def _language_service_request(wav: str, service_url: str | None = None) -> dict:
    boundary = '----vse-language-' + uuid.uuid4().hex
    with open(wav, 'rb') as handle:
        content = handle.read()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="sample.wav"\r\n'
            'Content-Type: audio/wav\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
    url = (service_url or os.environ.get('LANGUAGE_ID_URL', 'http://language-id:9000')).rstrip('/') + '/detect-language'
    request = urllib.request.Request(url, data=body, method='POST', headers={'Content-Type': f'multipart/form-data; boundary={boundary}', 'Content-Length': str(len(body))})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            body = exc.read(500).decode('utf-8', errors='replace') if hasattr(exc, "read") else ""
            if exc.code >= 500 and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("audio_language_detection event=language_service_sample_failed status=%s attempt=%d detail=%s", exc.code, attempt + 1, body.replace("\n", " ")[-300:])
            return {"language_code": "", "confidence": 0.0, "error": f"language-id HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("audio_language_detection event=language_service_sample_failed attempt=%d detail=%s", attempt + 1, str(exc).replace("\n", " ")[-300:])
            return {"language_code": "", "confidence": 0.0, "error": "language-id unavailable"}
    return {"language_code": "", "confidence": 0.0, "error": "language-id unavailable"}


def extract_audio_sample(path: str, stream_index: int, start: float, seconds: int, wav: str) -> None:
    """Extract a sample with one fast seek and one accurate-seek retry."""
    base = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error']
    commands = [
        base + ['-ss', f'{start:.3f}', '-i', path, '-map', f'0:a:{stream_index}', '-t', str(seconds), '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-f', 'wav', wav],
        base + ['-i', path, '-ss', f'{start:.3f}', '-map', f'0:a:{stream_index}', '-t', str(seconds), '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-f', 'wav', wav],
    ]
    errors = []
    for command in commands:
        Path(wav).unlink(missing_ok=True)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc)); continue
        if result.returncode == 0 and Path(wav).is_file() and Path(wav).stat().st_size > 44:
            return
        detail = (result.stderr or result.stdout or f'exit status {result.returncode}').strip().replace('\n', ' ')
        errors.append(detail[-500:])
    raise RuntimeError(f'FFmpeg audio sample extraction failed for audio {stream_index + 1} at {start:.1f}s: {errors[-1] if errors else "unknown error"}')


def process_audio_language_detection(task_id: int, payload: dict) -> dict:
    path = str(payload.get('path') or '')
    # Final-version media is intentionally view-only. Core reconciliation still
    # runs so Plex/file replacements can clear the lock, but advisory voice
    # detection must not rewrite its state while the lock is active.
    try:
        from app.v86 import final_version_entity_for_path
        entity = final_version_entity_for_path(path)
        if entity:
            with connection() as db:
                final = db.execute("SELECT final_version FROM media_notes WHERE entity_type=? AND entity_key=?", entity).fetchone()
            if final and bool(final["final_version"]):
                logger.info("audio_language_detection event=skipped_final_version file=%s", path.replace('\n', '\\n'))
                return {'path': path, 'streams': 0, 'mismatches': 0, 'skipped': 'final_version'}
    except Exception as exc:
        logger.debug("audio_language_detection final-version check unavailable: %s", exc)
    if not path or not Path(path).is_file():
        raise RuntimeError(f'Media file is not accessible: {path}')
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type:stream_tags=language', '-of', 'json', path, '-select_streams', 'a'], text=True))
    all_audio_streams = [stream for stream in probe.get('streams', []) if stream.get('codec_type') == 'audio']
    requested_indices = payload.get('stream_indices')
    if requested_indices and requested_indices != 'all':
        wanted = {int(item) for item in requested_indices}
        streams = [(index, stream) for index, stream in enumerate(all_audio_streams) if index in wanted]
    else:
        streams = list(enumerate(all_audio_streams))
    duration = float((probe.get('format') or {}).get('duration') or 0)
    service_url = os.environ.get('LANGUAGE_ID_URL', 'http://language-id:9000')
    sample_seconds = 30
    sample_positions = (0.10, 0.50, 0.90)
    try:
        with connection() as db:
            config = {str(row['key']): str(row['value']) for row in db.execute("SELECT key,value FROM language_detection_settings WHERE key LIKE 'voice_detection_%'").fetchall()}
        service_url = config.get('voice_detection_url', service_url)
        sample_seconds = max(10, min(120, int(config.get('voice_detection_sample_seconds', '30'))))
        parsed_positions = json.loads(config.get('voice_detection_positions', '[0.1,0.5,0.9]'))
        sample_positions = tuple(max(0.0, min(1.0, float(value))) for value in parsed_positions) or sample_positions
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    if not streams:
        return {'path': path, 'streams': 0, 'mismatches': 0}
    results = []
    with tempfile.TemporaryDirectory(prefix='vse-audio-language-') as work:
        for index, stream in streams:
            metadata = str((stream.get('tags') or {}).get('language') or '').strip()
            samples = []
            for number, fraction in enumerate(sample_positions, 1):
                start = max(0.0, min(max(0.0, duration - float(sample_seconds)), duration * fraction))
                wav = str(Path(work) / f'audio-{index}-{number}.wav')
                extract_audio_sample(path, index, start, sample_seconds, wav)
                detected = _language_service_request(wav, service_url)
                samples.append({'language': str(detected.get('language_code') or '').strip().lower(), 'confidence': float(detected.get('confidence') or 0), 'position': round(start, 2)})
                update_progress(task_id, (index * 3) + number, len(streams) * len(sample_positions), f'Detecting audio {index + 1}/{len(streams)} sample {number}/{len(sample_positions)}')
            votes = {}
            for sample in samples:
                bucket = _audio_language_code(sample['language'])
                if bucket:
                    votes.setdefault(bucket, []).append(sample)
            detected_code, winning = max(votes.items(), key=lambda item: (len(item[1]), sum(x['confidence'] for x in item[1]))) if votes else ('', [])
            confidence = sum(x['confidence'] for x in winning) / len(winning) if winning else 0.0
            metadata_code = _audio_language_code(metadata)
            mismatch = bool(detected_code and (not metadata or metadata_code in {'', 'und', 'unknown'} or detected_code != metadata_code))
            results.append((path, index, metadata, '', detected_code, confidence, json.dumps(samples, ensure_ascii=False), 1 if mismatch else 0))
    with connection() as db:
        if requested_indices and requested_indices != 'all':
            wanted = {int(item) for item in requested_indices}
            marks = ','.join('?' for _ in wanted)
            db.execute(f'DELETE FROM audio_language_detection WHERE path=? AND type_index IN ({marks})', [path, *wanted])
        else:
            db.execute('DELETE FROM audio_language_detection WHERE path=?', (path,))
        db.executemany("""INSERT INTO audio_language_detection(path,type_index,metadata_language,metadata_region,detected_language,confidence,samples_json,mismatch,checked_at)
            VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""", results)
    update_progress(task_id, len(streams) * len(sample_positions), len(streams) * len(sample_positions), 'Audio language detection completed')
    mark_read_models_fresh(path, "voice", {"modified": int(Path(path).stat().st_mtime), "size": Path(path).stat().st_size})
    mismatches = sum(row[-1] for row in results)
    logger.info('audio_language_detection event=completed path=%s streams=%d mismatches=%d', path.replace('\n', '\\n'), len(results), mismatches)
    return {'path': path, 'streams': len(results), 'mismatches': mismatches, 'results': [{'type_index': row[1], 'metadata_language': row[2], 'detected_language': row[4], 'confidence': row[5], 'mismatch': bool(row[7])} for row in results]}


TASK_HANDLERS = {"media_edit": process_media_edit, "media_reindex": process_media_reindex, "audio_language_detection": process_audio_language_detection}


DEFAULT_TASK_PRIORITIES = [
    "media_edit", "tv_filtered_stream_edit_now", "filtered_stream_edit_now",
    "tv_filtered_stream_edit", "filtered_stream_edit", "movie_import",
    "audio_language_detection", "subtitle_html_cleanup", "image_subtitle_convert",
    "media_reindex", "plex_sync", "plex_import_refresh", "index_check_prepare", "index_rebuild_prepare",
]

def task_priority_order() -> list[str]:
    with connection() as db:
        row = db.execute("SELECT value FROM task_queue_settings WHERE key='priority_order'").fetchone()
        seen = []
        try: seen = [str(value) for value in json.loads(row["value"] or "[]")] if row else []
        except (TypeError, ValueError, json.JSONDecodeError): seen = []
        existing = [str(item["task_type"]) for item in db.execute("SELECT DISTINCT task_type FROM task_queue").fetchall()]
    return list(dict.fromkeys([item for item in seen + DEFAULT_TASK_PRIORITIES + existing if item]))


@app.get("/api/v65/queue/priorities")
def get_queue_priorities() -> dict:
    return {"priorities": task_priority_order()}


@app.get("/api/v65/queue/policy")
def get_queue_policy() -> dict:
    return {
        "user_priority": [item for item in task_priority_order() if item not in SYSTEM_TASK_TYPES],
        "protected_system_tasks": sorted(SYSTEM_TASK_TYPES),
        "system_aging_seconds": SYSTEM_AGING_SECONDS,
        "run_sooner": {"inherited_by_group": True, "max_minutes": 1440, "fairness": "aging prevents maintenance starvation"},
    }


class QueuePrioritiesRequest(BaseModel):
    priorities: list[str] = Field(min_length=1, max_length=100)


@app.put("/api/v65/queue/priorities")
def save_queue_priorities(request: QueuePrioritiesRequest) -> dict:
    allowed = set(DEFAULT_TASK_PRIORITIES)
    with connection() as db:
        allowed.update(str(item["task_type"]) for item in db.execute("SELECT DISTINCT task_type FROM task_queue").fetchall())
        values = list(dict.fromkeys(str(value).strip() for value in request.priorities if str(value).strip() in allowed))
        if not values: raise HTTPException(400, "At least one valid queue task type is required")
        db.execute("INSERT OR REPLACE INTO task_queue_settings(key,value) VALUES('priority_order',?)", (json.dumps(values),))
    logger.info("task_queue event=priorities_saved count=%d order=%s", len(values), ",".join(values))
    wake_queue()
    return {"priorities": task_priority_order()}


def run_queue(lane: str = "main") -> None:
    light_lane = lane == "light"
    logger.info("task_queue event=worker_started lane=%s", lane)
    while not queue_shutdown.is_set():
        if queue_paused():
            with queue_condition:
                queue_condition.wait(timeout=5)
            continue
        priorities = task_priority_order()
        system_marks = ",".join("?" for _ in SYSTEM_TASK_TYPES)
        priority_case = "CASE task_type " + " ".join(f"WHEN ? THEN {index}" for index, _ in enumerate(priorities)) + " ELSE 999 END"
        # A grouped task can be pending only because an earlier stage owns the
        # workflow boundary. Keep those rows eligible, but always let runnable
        # tasks (including the predecessor stage) go first. Without this guard,
        # a high-priority blocked task can be claimed repeatedly and starve its
        # own predecessor indefinitely.
        workflow_wait_case = "CASE WHEN progress_message='Waiting for workflow resource' THEN 1 ELSE 0 END"
        if os.getenv('DATABASE_BACKEND', 'sqlite').lower() == 'postgres':
            # Prefer the earliest runnable stage of an active group. This lets
            # a low-priority predecessor unblock its higher-priority siblings.
            workflow_stage_case = "CASE WHEN EXISTS (SELECT 1 FROM workflow_stages current_stage WHERE current_stage.group_id=task_queue.group_id::uuid AND current_stage.status='pending' AND current_stage.payload->>'task_id'=task_queue.id::text AND NOT EXISTS (SELECT 1 FROM workflow_stages earlier_stage WHERE earlier_stage.group_id=current_stage.group_id AND earlier_stage.stage_number<current_stage.stage_number AND earlier_stage.status NOT IN ('succeeded','cancelled'))) THEN 0 ELSE 1 END"
        else:
            workflow_stage_case = '1'
        with connection() as db:
            clear_expired_expedites(db)
            # Immediate bulk edits are submitted as individual tasks so the
            # UI can report per-media progress. Run them ahead of unrelated
            # backlog items; otherwise an immediate operation could remain at
            # 0/N behind hundreds of older maintenance tasks.
            if light_lane:
                row = db.execute(f"""SELECT * FROM task_queue
                    WHERE status='pending' AND task_type IN ('index_check_prepare','index_rebuild_prepare')
                    ORDER BY {priority_case}, id LIMIT 1""", priorities).fetchone()
            else:
                row = db.execute(f"""SELECT * FROM task_queue
                    WHERE status='pending' AND task_type NOT IN ('media_reindex','index_check_prepare','index_rebuild_prepare')
                    ORDER BY CASE WHEN task_type IN ('tv_filtered_stream_edit_now','filtered_stream_edit_now') THEN 0 WHEN task_type='audio_language_detection' THEN 2 ELSE ({workflow_stage_case}) END, CASE WHEN EXISTS (SELECT 1 FROM task_queue_expedite boost WHERE boost.task_id=task_queue.id AND boost.expires_at > ?) THEN 0 ELSE 1 END, CASE WHEN task_type IN ({system_marks}) AND created_at < ? THEN 0 ELSE 1 END, {workflow_wait_case}, {priority_case}, CASE WHEN task_type IN ('tv_filtered_stream_edit_now','filtered_stream_edit_now') THEN 0
                                  WHEN task_type IN ('tv_filtered_stream_edit','filtered_stream_edit') THEN 1 ELSE 2 END,
                             CASE WHEN task_type IN ('tv_filtered_stream_edit_now','filtered_stream_edit_now','tv_filtered_stream_edit','filtered_stream_edit') THEN id END DESC,
                             id LIMIT 1""", [utc_now(), *SYSTEM_TASK_TYPES, scheduler_system_cutoff(), *priorities]).fetchone()
            if row:
                claimed = db.execute(
                    "UPDATE task_queue SET status='running',started_at=?,updated_at=?,attempts=attempts+1,progress_message='Starting' WHERE id=? AND status='pending'",
                    (utc_now(), utc_now(), row["id"]),
                ).rowcount
            else:
                claimed = 0
        if not row or not claimed:
            with queue_condition:
                queue_condition.wait(timeout=5)
            continue
        task_id, task_type = row["id"], row["task_type"]
        logger.info("task_queue event=task_started lane=%s id=%d type=%s attempt=%d", lane, task_id, task_type, row["attempts"] + 1)
        started = time.monotonic()
        stage_started = False
        task_payload = {}
        luw_id = None
        luw_path = ""
        try:
            task_payload = json.loads(row["payload_json"])
            validate_media_signature(task_type, task_payload)
            stage_path = affected_media_path(task_type, task_payload)
            luw_path = stage_path
            if not task_stage_exists(task_payload.get("_queue_group"), task_type, task_id):
                register_task_stage(task_payload.get("_queue_group"), task_type, stage_path, task_payload, task_id)
            luw_id = _create_media_luw(task_id, task_type, task_payload, stage_path)
            if luw_id:
                transition_luw(luw_id, "preflighted", "preflight", "Media signature accepted")
                if not acquire_luw_lock(luw_id, stage_path):
                    transition_luw(luw_id, "waiting", "lock", "Waiting for media LUW lock")
                    with connection() as db:
                        db.execute("UPDATE task_queue SET status='pending',progress_message='Waiting for media LUW lock',attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END,started_at=NULL,updated_at=? WHERE id=?", (utc_now(), task_id))
                    # A sibling task may own the media LUW. Back off instead
                    # of spinning and inflating attempts while waiting for it.
                    time.sleep(0.5)
                    continue
                transition_luw(luw_id, "applying", "apply", "Applying media edit")
            if not begin_task_stage(task_payload.get("_queue_group"), task_type, stage_path, task_id):
                with connection() as db:
                    db.execute("UPDATE task_queue SET status='pending',progress_message='Waiting for workflow resource',attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END,started_at=NULL,updated_at=? WHERE id=?", (utc_now(), task_id))
                if luw_id:
                    release_luw_lock(luw_id, luw_path)
                    transition_luw(luw_id, "waiting", "workflow", "Waiting for workflow stage")
                logger.info("workflow event=stage_waiting id=%d type=%s resource=%s", task_id, task_type, stage_path)
                # Do not reclaim a blocked stage in a tight loop. This gives
                # its predecessor time to advance and protects the worker
                # from malformed/orphaned workflow groups.
                time.sleep(0.5)
                continue
            stage_started = True
            # Private queue metadata is persisted for validation but must not
            # be passed to strict Pydantic task request models.
            handler_payload = {key: value for key, value in task_payload.items() if not str(key).startswith("_")}
            prepare_task_artifact(task_payload.get("_queue_group"), task_id, task_type, stage_path, task_payload)
            result = TASK_HANDLERS[task_type](task_id, handler_payload)
            if luw_id:
                transition_luw(luw_id, "verifying", "verify", "Media edit completed; verifying result")
            with connection() as db:
                db.execute(
                    "UPDATE task_queue SET status='succeeded',result_json=?,progress_message='Completed',finished_at=?,updated_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), utc_now(), utc_now(), task_id),
                )
                db.execute("DELETE FROM media_change_request WHERE task_id=?", (task_id,))
            advance_media_signature(task_payload)
            if luw_id:
                committed_path = str(result.get("target") or luw_path)
                commit_luw(luw_id, media_configuration_signature(committed_path))
            commit_task_artifacts(task_payload.get("_queue_group"), task_id)
            finish_task_stage(task_payload.get("_queue_group"), task_type, affected_media_path(task_type, task_payload), result, task_id)
            removed_snapshots = cleanup_succeeded_workflow_group_artifacts(task_payload.get("_queue_group"))
            if removed_snapshots:
                logger.info("workflow event=staging_cleaned group=%s files=%d", task_payload.get("_queue_group"), removed_snapshots)
            logger.info("task_queue event=task_completed lane=%s id=%d type=%s seconds=%.2f", lane, task_id, task_type, time.monotonic() - started)
        except Exception as exc:
            message = str(getattr(exc, "detail", exc))
            if luw_id:
                try:
                    release_luw_lock(luw_id, luw_path)
                    transition_luw(luw_id, "failed", "error", message)
                except Exception:
                    logger.exception("luw event=failure_persist_failed task=%d", task_id)
            # Detection is advisory background work. If the media changed
            # since it was queued, discard this obsolete sample instead of
            # creating a visible failure or blocking user operations.
            if task_type == "audio_language_detection" and "Media changed between queueing and execution" in message:
                with connection() as db:
                    db.execute("UPDATE task_queue SET status='cancelled',error=?,progress_message='Discarded: media changed',finished_at=?,updated_at=? WHERE id=?", (message, utc_now(), utc_now(), task_id))
                logger.info("audio_language_detection event=discarded_obsolete task=%d", task_id)
                continue
            if stage_started:
                try:
                    fail_task_stage(task_payload.get("_queue_group"), task_type, affected_media_path(task_type, task_payload), message, task_id)
                except Exception as workflow_exc:
                    logger.exception("workflow event=stage_failure_persist_failed id=%d error=%s", task_id, workflow_exc)
            with connection() as db:
                db.execute(
                    "UPDATE task_queue SET status='failed',error=?,progress_message='Failed',finished_at=?,updated_at=? WHERE id=?",
                    (message[-4000:], utc_now(), utc_now(), task_id),
                )
            failed_path = affected_media_path(task_type, json.loads(row["payload_json"]))
            if failed_path:
                try:
                    from app.v80 import request_media_indexes
                    request_media_indexes(failed_path, ["core"], "Queued media change failed; refresh subtitle detection")
                except Exception as index_exc:
                    logger.warning("subtitle_detection event=failed_task_refresh_error task=%d error=%s", task_id, str(index_exc).replace("\n", " ")[-300:])
            logger.exception("task_queue event=task_failed lane=%s id=%d type=%s seconds=%.2f error=%s", lane, task_id, task_type, time.monotonic() - started, message.replace("\n", " ")[-500:])


@app.on_event("startup")
def initialize_task_queue() -> None:
    global queue_thread
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS task_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL, label TEXT NOT NULL, payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','cancelled')),
                attempts INTEGER NOT NULL DEFAULT 0,
                progress_current INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0,
                progress_message TEXT NOT NULL DEFAULT '', result_json TEXT, error TEXT,
                group_id TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_queue_group (
                group_id TEXT PRIMARY KEY, path TEXT NOT NULL, signature_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_queue_expedite (
                task_id INTEGER PRIMARY KEY, group_id TEXT, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_queue_status_id ON task_queue(status,id);
            CREATE TABLE IF NOT EXISTS media_change_request (
                task_id INTEGER PRIMARY KEY, path TEXT NOT NULL, requested_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS media_change_request_path ON media_change_request(path);
            CREATE TABLE IF NOT EXISTS task_queue_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO task_queue_settings(key,value) VALUES('paused','0');
            INSERT OR IGNORE INTO task_queue_settings(key,value) VALUES('priority_order','[]');
            UPDATE task_queue SET status='pending',progress_message='Recovered after restart',started_at=NULL WHERE status='running';
        """)
        if not column_exists(db, "task_queue", "group_id"):
            db.execute("ALTER TABLE task_queue ADD COLUMN IF NOT EXISTS group_id TEXT") if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres" else db.execute("ALTER TABLE task_queue ADD COLUMN group_id TEXT")
        if not column_exists(db, "task_queue_expedite", "group_id"):
            db.execute("ALTER TABLE task_queue_expedite ADD COLUMN IF NOT EXISTS group_id TEXT") if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres" else db.execute("ALTER TABLE task_queue_expedite ADD COLUMN group_id TEXT")
        if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres":
            # Older deployments stored expedite timestamps as TEXT. Convert
            # once so all priority comparisons are type-safe in PostgreSQL.
            column_type = db.execute("SELECT data_type FROM information_schema.columns WHERE table_name='task_queue_expedite' AND column_name='expires_at'").fetchone()
            if column_type and str(column_type[0]).lower() in {"text", "character varying"}:
                db.execute("ALTER TABLE task_queue_expedite ALTER COLUMN expires_at TYPE TIMESTAMPTZ USING NULLIF(expires_at,'')::timestamptz")
                db.execute("ALTER TABLE task_queue_expedite ALTER COLUMN requested_at TYPE TIMESTAMPTZ USING NULLIF(requested_at,'')::timestamptz")
        if os.getenv("DATABASE_BACKEND", "sqlite").lower() == "postgres":
            db.execute("""UPDATE workflow_stages stage SET status='pending', started_at=NULL, updated_at=now()
                          WHERE stage.status='running' AND stage.payload->>'task_id' IN
                            (SELECT id::text FROM task_queue WHERE status <> 'running')""")
            db.execute("""UPDATE workflow_groups group_row SET status='pending', updated_at=now()
                          WHERE group_row.status='running' AND NOT EXISTS
                            (SELECT 1 FROM workflow_stages stage WHERE stage.group_id=group_row.group_id AND stage.status='running')""")
        db.execute("DELETE FROM media_change_request WHERE task_id NOT IN (SELECT id FROM task_queue WHERE status IN ('pending','running','failed'))")
        # Keep active boosts attached to completed parents until expiry so
        # follow-up child/index work created after a restart can inherit them.
        db.execute("DELETE FROM task_queue_expedite WHERE task_id NOT IN (SELECT id FROM task_queue) OR expires_at <= ?", (utc_now(),))
        active = db.execute("SELECT id,task_type,payload_json,created_at FROM task_queue WHERE status IN ('pending','running','failed')").fetchall()
        for row in active:
            try:
                affected = affected_media_path(row["task_type"], json.loads(row["payload_json"]))
            except (json.JSONDecodeError, TypeError):
                affected = ""
            if affected:
                db.execute("INSERT OR IGNORE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (row["id"], affected, row["created_at"]))
    try:
        removed_orphans = cleanup_orphaned_workflow_artifacts_startup()
        if removed_orphans:
            logger.info("workflow event=startup_orphan_staging_cleaned files=%d", removed_orphans)
        removed_snapshots = cleanup_succeeded_workflow_artifacts_startup()
        if removed_snapshots:
            logger.info("workflow event=startup_staging_cleaned files=%d", removed_snapshots)
    except Exception as exc:
        logger.warning("workflow event=startup_staging_cleanup_failed error=%s", str(exc).replace("\n", " ")[-300:])


def start_task_queue_worker() -> None:
    global queue_thread, queue_light_thread
    queue_shutdown.clear()
    if not queue_thread or not queue_thread.is_alive():
        queue_thread = threading.Thread(target=run_queue, args=("main",), name="vse-task-queue", daemon=True)
        queue_thread.start()
    if not queue_light_thread or not queue_light_thread.is_alive():
        queue_light_thread = threading.Thread(target=run_queue, args=("light",), name="vse-task-queue-light", daemon=True)
        queue_light_thread.start()
    wake_queue()


@app.post("/api/v65/queue")
@app.on_event("shutdown")
def shutdown_task_queue() -> None:
    queue_shutdown.set()
    with queue_condition:
        queue_condition.notify_all()
    workers = [thread for thread in (queue_thread, queue_light_thread) if thread]
    for thread in workers:
        thread.join(timeout=30)
    logger.info("task_queue event=worker_shutdown workers=%d alive=%d", len(workers), sum(thread.is_alive() for thread in workers))


def add_queue_item(request: QueueRequest) -> dict:
    if request.task_type == "media_edit" and not (request.payload.get("edit") or request.payload).get("path"):
        raise HTTPException(400, "An edit media path is required")
    if request.task_type == "movie_import" and not request.payload.get("source"):
        raise HTTPException(400, "An import source path is required")
    return enqueue(request.task_type, request.payload, request.label)


@app.get("/api/v65/queue")
def list_queue(limit: int = 200, status: str | None = None, task_type: str | None = None, grouped: bool = False, q: str | None = None) -> dict:
    limit = max(1, min(limit, 500))
    if status not in {None, "running", "pending", "failed", "succeeded", "cancelled"}:
        raise HTTPException(400, "Unsupported queue status")
    if task_type and not task_type.strip():
        task_type = None
    q = q.strip() if q else None
    clauses = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if task_type:
        clauses.append("task_type=?")
        params.append(task_type)
    if q:
        clauses.append("(label LIKE ? OR payload_json LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    groups = []
    with connection() as db:
        rows = db.execute(f"SELECT task_queue.*, CASE WHEN task_queue_expedite.task_id IS NOT NULL AND task_queue_expedite.expires_at > ? THEN 1 ELSE 0 END AS expedited, task_queue_expedite.expires_at AS expedite_expires_at FROM task_queue LEFT JOIN task_queue_expedite ON task_queue_expedite.task_id=task_queue.id{where} ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END,id DESC LIMIT ?", [utc_now(), *params]).fetchall()
        counts = {row["status"]: row["amount"] for row in db.execute("SELECT status,count(*) amount FROM task_queue GROUP BY status")}
        type_status = status if status in {"pending", "failed", "succeeded", "cancelled"} else None
        pending_rows = db.execute("SELECT task_type,count(*) amount FROM task_queue WHERE status=? GROUP BY task_type", (type_status,)).fetchall() if type_status else []
    priority = {value: index for index, value in enumerate(task_priority_order())}
    pending_types = sorted(({"task_type": str(row["task_type"]), "count": row["amount"]} for row in pending_rows), key=lambda row: (priority.get(row["task_type"], 999999), row["task_type"]))
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        raw_result = item.pop("result_json", None)
        item["result"] = json.loads(raw_result) if raw_result else None
        items.append(item)
    if grouped:
        with connection() as db:
            group_clauses = list(clauses) + ["group_id IS NOT NULL"]
            group_where = " WHERE " + " AND ".join(group_clauses)
            group_params = list(params[:-1]) if params else []
            group_rows = db.execute(
                f"SELECT group_id,status,created_at,updated_at FROM task_queue{group_where} ORDER BY updated_at DESC LIMIT ?",
                [*group_params, min(limit, 50)],
            ).fetchall()
            grouped_rows = {}
            for item in group_rows:
                gid = str(item["group_id"])
                group = grouped_rows.setdefault(gid, {"group_id": gid, "task_count": 0, "created_at": item["created_at"], "updated_at": item["updated_at"], "running": 0, "pending": 0, "failed": 0, "succeeded": 0, "cancelled": 0})
                group["task_count"] += 1
                group["created_at"] = min(group["created_at"], item["created_at"])
                group["updated_at"] = max(group["updated_at"], item["updated_at"])
                group[str(item["status"])] = group.get(str(item["status"]), 0) + 1
            group_rows = list(grouped_rows.values())[:limit]
            for row in group_rows:
                group = dict(row)
                gid = str(group["group_id"])
                group["tasks"] = [dict(item) for item in db.execute(
                    "SELECT id,label,task_type,status,progress_current,progress_total,progress_message,error,created_at,updated_at "
                    "FROM task_queue WHERE group_id=? ORDER BY id", (gid,)
                ).fetchall()]
                try:
                    group["workflow"] = workflow_details(gid) or None
                except Exception:
                    group["workflow"] = None
                groups.append(group)
    return {"paused": queue_paused(), "counts": counts, "type_status": type_status, "pending_types": pending_types, "items": items, "grouped": bool(grouped), "groups": groups}


@app.post("/api/v65/queue/expedite-matching")
def expedite_matching_queue(request: QueueExpediteMatchingRequest) -> dict:
    """Boost pending tasks whose label or payload identifies a requested movie/show."""
    query = request.query.strip()
    now = utc_now()
    expires = datetime.fromtimestamp(time.time() + request.minutes * 60, timezone.utc).isoformat(timespec="seconds")
    with connection() as db:
        clear_expired_expedites(db)
        rows = db.execute("SELECT id,group_id FROM task_queue WHERE status='pending' AND (label LIKE ? OR payload_json LIKE ?)", (f"%{query}%", f"%{query}%")).fetchall()
        if not rows:
            raise HTTPException(404, "No pending tasks matched that movie or show")
        for row in rows:
            db.execute("INSERT OR REPLACE INTO task_queue_expedite(task_id,group_id,requested_at,expires_at) VALUES(?,?,?,?)", (row["id"], row["group_id"], now, expires))
    logger.info("task_queue event=matching_expedited query=%s tasks=%d expires=%s", query.replace("\n", " ")[:120], len(rows), expires)
    wake_queue()
    return {"query": query, "tasks": len(rows), "expedited": True, "expires_at": expires}


@app.post("/api/v65/queue/{task_id}/expedite")
def expedite_queue_item(task_id: int, request: QueueExpediteRequest) -> dict:
    """Temporarily prioritize one pending media task without bypassing workflow stages."""
    now = utc_now()
    expires = datetime.fromtimestamp(time.time() + request.minutes * 60, timezone.utc).isoformat(timespec="seconds")
    with connection() as db:
        row = db.execute("SELECT id,status,group_id FROM task_queue WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Queue item not found")
        if row["status"] != "pending":
            raise HTTPException(409, "Only pending work can be expedited; running work is never interrupted")
        clear_expired_expedites(db)
        db.execute("INSERT OR REPLACE INTO task_queue_expedite(task_id,group_id,requested_at,expires_at) VALUES(?,?,?,?)", (task_id, row["group_id"], now, expires))
    logger.info("task_queue event=expedited id=%d expires=%s", task_id, expires)
    wake_queue()
    return {"task_id": task_id, "expedited": True, "expires_at": expires}


@app.delete("/api/v65/queue/{task_id}/expedite")
def cancel_expedite_queue_item(task_id: int) -> dict:
    with connection() as db:
        db.execute("DELETE FROM task_queue_expedite WHERE task_id=?", (task_id,))
    logger.info("task_queue event=expedite_cancelled id=%d", task_id)
    wake_queue()
    return {"task_id": task_id, "expedited": False}


@app.post("/api/v65/queue/group/{group_id}/expedite")
def expedite_queue_group(group_id: str, request: QueueExpediteRequest) -> dict:
    """Temporarily prioritize pending stages in one workflow group."""
    now = utc_now()
    expires = datetime.fromtimestamp(time.time() + request.minutes * 60, timezone.utc).isoformat(timespec="seconds")
    with connection() as db:
        rows = db.execute("SELECT id FROM task_queue WHERE group_id=? AND status='pending'", (group_id,)).fetchall()
        if not rows:
            raise HTTPException(409, "This workflow has no pending stage to expedite")
        clear_expired_expedites(db)
        for row in rows:
            db.execute("INSERT OR REPLACE INTO task_queue_expedite(task_id,group_id,requested_at,expires_at) VALUES(?,?,?,?)", (row["id"], group_id, now, expires))
    logger.info("task_queue event=workflow_expedited group=%s tasks=%d expires=%s", group_id, len(rows), expires)
    wake_queue()
    return {"group_id": group_id, "tasks": len(rows), "expedited": True, "expires_at": expires}


@app.delete("/api/v65/queue/group/{group_id}/expedite")
def cancel_expedite_queue_group(group_id: str) -> dict:
    with connection() as db:
        changed = db.execute("DELETE FROM task_queue_expedite WHERE group_id=?", (group_id,)).rowcount
    logger.info("task_queue event=workflow_expedite_cancelled group=%s tasks=%d", group_id, changed)
    wake_queue()
    return {"group_id": group_id, "expedited": False, "tasks": changed}


@app.post("/api/v65/queue/status")
def queue_status(request: QueueStatusRequest) -> dict:
    items = []
    with connection() as db:
        for start in range(0, len(request.task_ids), 800):
            group = request.task_ids[start:start + 800]
            rows = db.execute(
                f"SELECT id,label,status,progress_current,progress_total,progress_message,error,result_json FROM task_queue WHERE id IN ({','.join('?' for _ in group)})",
                group,
            ).fetchall()
            items.extend(dict(row) for row in rows)
    return {"items": items}


@app.put("/api/v65/queue/control")
def control_queue(request: QueueAction) -> dict:
    paused = request.action == "pause"
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO task_queue_settings(key,value) VALUES('paused',?)", ("1" if paused else "0",))
    logger.info("task_queue event=%s", "paused" if paused else "resumed")
    wake_queue()
    return {"paused": paused}


def _group_has_unfinished_tasks(db, group_id: str | None) -> bool:
    """Completed tasks remain until every sibling task in their group is done."""
    if not group_id:
        return False
    row = db.execute(
        "SELECT 1 FROM task_queue WHERE group_id=? AND status IN ('pending','running') LIMIT 1",
        (group_id,),
    ).fetchone()
    return bool(row)


@app.post("/api/v65/queue/bulk")
def bulk_queue_action(request: dict) -> dict:
    action = str(request.get("action") or "").strip().lower()
    status = str(request.get("status") or "").strip().lower()
    task_type = str(request.get("task_type") or "").strip()
    if action not in {"delete", "retry"} or status not in {"pending", "failed", "succeeded", "cancelled"}:
        raise HTTPException(400, "Bulk action requires a finished/pending status filter")
    if action == "retry" and status != "failed":
        raise HTTPException(400, "Only failed tasks can be retried")
    clauses = ["status=?"]
    params: list[Any] = [status]
    if task_type:
        clauses.append("task_type=?")
        params.append(task_type)
    where = " AND ".join(clauses)
    with connection() as db:
        if action == "retry":
            changed = db.execute(f"UPDATE task_queue SET status='pending',error=NULL,finished_at=NULL,progress_current=0,progress_total=0,progress_message='Waiting',updated_at=? WHERE {where}", [utc_now(), *params]).rowcount
        else:
            candidates = db.execute(f"SELECT id,group_id FROM task_queue WHERE {where}", params).fetchall()
            ids, skipped = [], 0
            for candidate in candidates:
                if _group_has_unfinished_tasks(db, candidate["group_id"]):
                    skipped += 1
                else:
                    ids.append(int(candidate["id"]))
            if ids:
                placeholders=','.join('?' for _ in ids)
                changed = db.execute(f"DELETE FROM task_queue WHERE id IN ({placeholders})", ids).rowcount
                db.executemany("DELETE FROM media_change_request WHERE task_id=?", [(task_id,) for task_id in ids])
            else:
                changed = 0
    logger.info("task_queue event=bulk_%s status=%s task_type=%s count=%d skipped_group_partial=%d", action, status, task_type or "all", changed, skipped if action == 'delete' else 0)
    if action == "retry":
        wake_queue()
    return {"action": action, "status": status, "task_type": task_type, "count": changed, "skipped": skipped if action == 'delete' else 0}

@app.post("/api/v65/queue/{task_id}/retry")
def retry_queue_item(task_id: int) -> dict:
    with connection() as db:
        changed = db.execute("UPDATE task_queue SET status='pending',error=NULL,finished_at=NULL,progress_current=0,progress_total=0,progress_message='Waiting',updated_at=? WHERE id=? AND status='failed'", (utc_now(), task_id)).rowcount
    if not changed:
        raise HTTPException(409, "Only failed queue items can be retried")
    try:
        row = task_row(task_id)
        reset_task_stage_for_retry(row.get("group_id"), task_id)
    except Exception as exc:
        logger.warning("workflow event=retry_reset_failed id=%d error=%s", task_id, str(exc).replace("\n", " ")[-300:])
        raise HTTPException(500, "Queue item reset succeeded but staged workflow could not be reopened") from exc
    logger.info("task_queue event=retry_requested id=%d", task_id)
    wake_queue()
    return task_row(task_id)


@app.post("/api/v65/queue/{task_id}/cancel")
def cancel_queue_item(task_id: int) -> dict:
    with connection() as db:
        changed = db.execute("UPDATE task_queue SET status='cancelled',progress_message='Cancelled',finished_at=?,updated_at=? WHERE id=? AND status='pending'", (utc_now(), utc_now(), task_id)).rowcount
    if not changed:
        raise HTTPException(409, "Only pending queue items can be cancelled")
    with connection() as db:
        db.execute("DELETE FROM media_change_request WHERE task_id=?", (task_id,))
    logger.info("task_queue event=task_cancelled id=%d", task_id)
    return task_row(task_id)


@app.delete("/api/v65/queue/{task_id}")
def delete_queue_item(task_id: int) -> dict:
    with connection() as db:
        row = db.execute("SELECT status,group_id FROM task_queue WHERE id=?", (task_id,)).fetchone()
        if not row or row["status"] not in {'succeeded','failed','cancelled'}:
            raise HTTPException(409, "Only finished queue items can be deleted")
        if _group_has_unfinished_tasks(db, row["group_id"]):
            raise HTTPException(409, "This task belongs to a workflow group that is still in progress; delete it after the whole group completes")
        changed = db.execute("DELETE FROM task_queue WHERE id=?", (task_id,)).rowcount
        db.execute("DELETE FROM media_change_request WHERE task_id=?", (task_id,))
    if not changed:
        raise HTTPException(409, "Task was already removed")
    logger.info("task_queue event=task_deleted id=%d", task_id)
    return {"deleted": True, "id": task_id}
