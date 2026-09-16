from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from pydantic import BaseModel, Field

import app.v54 as indexes
import app.v7 as media_editor
from app.v7 import ReorderEditRequest
from app.v11 import connection
from app.v37 import MediaRenameRequest, rename_media
from app.v43 import optimized_media_edit
from app.v64 import app
from app.v5 import external_subtitles


logger = logging.getLogger("uvicorn.error")
queue_condition = threading.Condition()
queue_thread: threading.Thread | None = None
queue_light_thread: threading.Thread | None = None
LIGHT_TASK_TYPES = ("index_check_prepare", "index_rebuild_prepare")


class QueueRequest(BaseModel):
    task_type: Literal["media_edit", "movie_import"]
    payload: dict[str, Any]
    label: str = ""


class QueueAction(BaseModel):
    action: Literal["pause", "resume"]


class QueueStatusRequest(BaseModel):
    task_ids: list[int] = Field(min_length=1, max_length=30000)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def affected_media_path(task_type: str, payload: dict) -> str:
    if task_type == "media_edit":
        return str((payload.get("edit") or payload).get("path") or "")
    if task_type == "subtitle_html_cleanup":
        return str(payload.get("path") or "")
    if task_type in {"tv_filtered_stream_edit", "tv_filtered_stream_edit_now", "filtered_stream_edit", "filtered_stream_edit_now", "plex_import_refresh"}:
        return str(payload.get("path") or "")
    if task_type == "movie_import":
        return str(payload.get("source") or "")
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

def attach_media_signature(task_type: str, payload: dict) -> dict:
    if task_type in SIGNATURE_EXCLUDED_TASKS or payload.get("_media_signature"):
        return payload
    path = affected_media_path(task_type, payload)
    if not path:
        return payload
    signed = dict(payload)
    signed["_media_signature"] = media_configuration_signature(path)
    return signed

def validate_media_signature(task_type: str, payload: dict) -> None:
    expected = payload.get("_media_signature")
    if not expected or task_type in SIGNATURE_EXCLUDED_TASKS:
        return
    path = str(expected.get("path") or affected_media_path(task_type, payload) or "")
    current = media_configuration_signature(path)
    if current.get("digest") != expected.get("digest") or bool(current.get("missing")) != bool(expected.get("missing")):
        raise RuntimeError("Media changed between queueing and execution; job refused for safety")

def enqueue(task_type: str, payload: dict, label: str = "", *, deduplicate: bool = False) -> dict:
    payload = attach_media_signature(task_type, payload)
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
            "INSERT INTO task_queue(task_type,label,payload_json,status,progress_message,created_at,updated_at) VALUES(?,?,?,'pending','Waiting',?,?)",
            (task_type, label.strip() or task_type.replace("_", " ").title(), encoded, utc_now(), utc_now()),
        )
        task_id = cursor.lastrowid
        affected = affected_media_path(task_type, payload)
        if affected:
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (task_id, affected, utc_now()))
    if affected:
        try:
            from app.v80 import invalidate_language_detection
            invalidate_language_detection(affected)
        except Exception as exc:
            logger.warning("subtitle_detection event=invalidation_failed task=%d error=%s", task_id, str(exc).replace("\n", " ")[-300:])
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
    if not renamed:
        from app.v80 import media_indexes_for_edit, request_media_indexes
        names = payload.get("reindex_indexes") or media_indexes_for_edit(edit_payload, payload.get("html_cleanups"), result.get("operation") == "single_remux")
        request_media_indexes(final_path, names, "Queued media edit completed")
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


def process_audio_language_detection(task_id: int, payload: dict) -> dict:
    path = str(payload.get('path') or '')
    if not path or not Path(path).is_file():
        raise RuntimeError(f'Media file is not accessible: {path}')
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type:stream_tags=language', '-of', 'json', path, '-select_streams', 'a'], text=True))
    streams = [stream for stream in probe.get('streams', []) if stream.get('codec_type') == 'audio']
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
        for index, stream in enumerate(streams):
            metadata = str((stream.get('tags') or {}).get('language') or '').strip()
            samples = []
            for number, fraction in enumerate(sample_positions, 1):
                start = max(0.0, min(max(0.0, duration - float(sample_seconds)), duration * fraction))
                wav = str(Path(work) / f'audio-{index}-{number}.wav')
                subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-ss', f'{start:.3f}', '-i', path, '-map', f'0:a:{index}', '-t', str(sample_seconds), '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-f', 'wav', wav], check=True, timeout=180)
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
        db.execute('DELETE FROM audio_language_detection WHERE path=?', (path,))
        db.executemany("""INSERT INTO audio_language_detection(path,type_index,metadata_language,metadata_region,detected_language,confidence,samples_json,mismatch,checked_at)
            VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""", results)
    update_progress(task_id, len(streams) * len(sample_positions), len(streams) * len(sample_positions), 'Audio language detection completed')
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
    while True:
        if queue_paused():
            with queue_condition:
                queue_condition.wait(timeout=5)
            continue
        priorities = task_priority_order()
        priority_case = "CASE task_type " + " ".join(f"WHEN ? THEN {index}" for index, _ in enumerate(priorities)) + " ELSE 999 END"
        with connection() as db:
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
                    ORDER BY {priority_case}, CASE WHEN task_type IN ('tv_filtered_stream_edit_now','filtered_stream_edit_now') THEN 0
                                  WHEN task_type IN ('tv_filtered_stream_edit','filtered_stream_edit') THEN 1 ELSE 2 END,
                             CASE WHEN task_type IN ('tv_filtered_stream_edit_now','filtered_stream_edit_now','tv_filtered_stream_edit','filtered_stream_edit') THEN id END DESC,
                             id LIMIT 1""", priorities).fetchone()
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
        try:
            task_payload = json.loads(row["payload_json"])
            validate_media_signature(task_type, task_payload)
            # Private queue metadata is persisted for validation but must not
            # be passed to strict Pydantic task request models.
            handler_payload = {key: value for key, value in task_payload.items() if not str(key).startswith("_")}
            result = TASK_HANDLERS[task_type](task_id, handler_payload)
            with connection() as db:
                db.execute(
                    "UPDATE task_queue SET status='succeeded',result_json=?,progress_message='Completed',finished_at=?,updated_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), utc_now(), utc_now(), task_id),
                )
                db.execute("DELETE FROM media_change_request WHERE task_id=?", (task_id,))
            logger.info("task_queue event=task_completed lane=%s id=%d type=%s seconds=%.2f", lane, task_id, task_type, time.monotonic() - started)
        except Exception as exc:
            message = str(getattr(exc, "detail", exc))
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
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
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
        db.execute("DELETE FROM media_change_request WHERE task_id NOT IN (SELECT id FROM task_queue WHERE status IN ('pending','running','failed'))")
        active = db.execute("SELECT id,task_type,payload_json,created_at FROM task_queue WHERE status IN ('pending','running','failed')").fetchall()
        for row in active:
            try:
                affected = affected_media_path(row["task_type"], json.loads(row["payload_json"]))
            except (json.JSONDecodeError, TypeError):
                affected = ""
            if affected:
                db.execute("INSERT OR IGNORE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (row["id"], affected, row["created_at"]))
def start_task_queue_worker() -> None:
    global queue_thread, queue_light_thread
    if not queue_thread or not queue_thread.is_alive():
        queue_thread = threading.Thread(target=run_queue, args=("main",), name="vse-task-queue", daemon=True)
        queue_thread.start()
    if not queue_light_thread or not queue_light_thread.is_alive():
        queue_light_thread = threading.Thread(target=run_queue, args=("light",), name="vse-task-queue-light", daemon=True)
        queue_light_thread.start()
    wake_queue()


@app.post("/api/v65/queue")
def add_queue_item(request: QueueRequest) -> dict:
    if request.task_type == "media_edit" and not (request.payload.get("edit") or request.payload).get("path"):
        raise HTTPException(400, "An edit media path is required")
    if request.task_type == "movie_import" and not request.payload.get("source"):
        raise HTTPException(400, "An import source path is required")
    return enqueue(request.task_type, request.payload, request.label)


@app.get("/api/v65/queue")
def list_queue(limit: int = 200, status: str | None = None, task_type: str | None = None) -> dict:
    limit = max(1, min(limit, 500))
    if status not in {None, "running", "pending", "failed", "succeeded", "cancelled"}:
        raise HTTPException(400, "Unsupported queue status")
    if task_type and not task_type.strip():
        task_type = None
    clauses = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if task_type:
        clauses.append("task_type=?")
        params.append(task_type)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with connection() as db:
        rows = db.execute(f"SELECT * FROM task_queue{where} ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END,id DESC LIMIT ?", params).fetchall()
        counts = {row["status"]: row["amount"] for row in db.execute("SELECT status,count(*) amount FROM task_queue GROUP BY status")}
        type_status = status if status in {"pending", "failed", "succeeded", "cancelled"} else None
        pending_rows = db.execute("SELECT task_type,count(*) amount FROM task_queue WHERE status=? GROUP BY task_type", (type_status,)).fetchall() if type_status else []
    priority = {value: index for index, value in enumerate(task_priority_order())}
    pending_types = sorted(({"task_type": str(row["task_type"]), "count": row["amount"]} for row in pending_rows), key=lambda row: (priority.get(row["task_type"], 999999), row["task_type"]))
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item.pop("result_json", None)
        items.append(item)
    return {"paused": queue_paused(), "counts": counts, "type_status": type_status, "pending_types": pending_types, "items": items}


@app.post("/api/v65/queue/status")
def queue_status(request: QueueStatusRequest) -> dict:
    items = []
    with connection() as db:
        for start in range(0, len(request.task_ids), 800):
            group = request.task_ids[start:start + 800]
            rows = db.execute(
                f"SELECT id,label,status,progress_current,progress_total,progress_message,error FROM task_queue WHERE id IN ({','.join('?' for _ in group)})",
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
            ids = [int(row["id"]) for row in db.execute(f"SELECT id FROM task_queue WHERE {where}", params).fetchall()]
            changed = db.execute(f"DELETE FROM task_queue WHERE {where}", params).rowcount
            if ids:
                db.executemany("DELETE FROM media_change_request WHERE task_id=?", [(task_id,) for task_id in ids])
    logger.info("task_queue event=bulk_%s status=%s task_type=%s count=%d", action, status, task_type or "all", changed)
    if action == "retry":
        wake_queue()
    return {"action": action, "status": status, "task_type": task_type, "count": changed}

@app.post("/api/v65/queue/{task_id}/retry")
def retry_queue_item(task_id: int) -> dict:
    with connection() as db:
        changed = db.execute("UPDATE task_queue SET status='pending',error=NULL,finished_at=NULL,progress_current=0,progress_total=0,progress_message='Waiting',updated_at=? WHERE id=? AND status='failed'", (utc_now(), task_id)).rowcount
    if not changed:
        raise HTTPException(409, "Only failed queue items can be retried")
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
        changed = db.execute("DELETE FROM task_queue WHERE id=? AND status IN ('succeeded','failed','cancelled')", (task_id,)).rowcount
    if not changed:
        raise HTTPException(409, "Only finished queue items can be deleted")
    with connection() as db:
        db.execute("DELETE FROM media_change_request WHERE task_id=?", (task_id,))
    logger.info("task_queue event=task_deleted id=%d", task_id)
    return {"deleted": True, "id": task_id}
