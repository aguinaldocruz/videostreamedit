from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v54 as indexes
from app.v7 import ReorderEditRequest
from app.v11 import connection
from app.v37 import MediaRenameRequest, rename_media
from app.v43 import optimized_media_edit
from app.v64 import app


logger = logging.getLogger("uvicorn.error")
queue_condition = threading.Condition()
queue_thread: threading.Thread | None = None


class QueueRequest(BaseModel):
    task_type: Literal["media_edit", "movie_import"]
    payload: dict[str, Any]
    label: str = ""


class QueueAction(BaseModel):
    action: Literal["pause", "resume"]


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
    if task_type == "movie_import":
        return str(payload.get("source") or "")
    return ""


def enqueue(task_type: str, payload: dict, label: str = "", *, deduplicate: bool = False) -> dict:
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


TASK_HANDLERS = {"media_edit": process_media_edit, "media_reindex": process_media_reindex}


def run_queue() -> None:
    logger.info("task_queue event=worker_started")
    while True:
        if queue_paused():
            with queue_condition:
                queue_condition.wait(timeout=5)
            continue
        with connection() as db:
            row = db.execute("SELECT * FROM task_queue WHERE status='pending' AND task_type!='media_reindex' ORDER BY id LIMIT 1").fetchone()
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
        logger.info("task_queue event=task_started id=%d type=%s attempt=%d", task_id, task_type, row["attempts"] + 1)
        started = time.monotonic()
        try:
            result = TASK_HANDLERS[task_type](task_id, json.loads(row["payload_json"]))
            with connection() as db:
                db.execute(
                    "UPDATE task_queue SET status='succeeded',result_json=?,progress_message='Completed',finished_at=?,updated_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), utc_now(), utc_now(), task_id),
                )
                db.execute("DELETE FROM media_change_request WHERE task_id=?", (task_id,))
            logger.info("task_queue event=task_completed id=%d type=%s seconds=%.2f", task_id, task_type, time.monotonic() - started)
        except Exception as exc:
            message = str(getattr(exc, "detail", exc))
            with connection() as db:
                db.execute(
                    "UPDATE task_queue SET status='failed',error=?,progress_message='Failed',finished_at=?,updated_at=? WHERE id=?",
                    (message[-4000:], utc_now(), utc_now(), task_id),
                )
            logger.exception("task_queue event=task_failed id=%d type=%s seconds=%.2f error=%s", task_id, task_type, time.monotonic() - started, message.replace("\n", " ")[-500:])


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
    global queue_thread
    if not queue_thread or not queue_thread.is_alive():
        queue_thread = threading.Thread(target=run_queue, name="vse-task-queue", daemon=True)
        queue_thread.start()
    wake_queue()


@app.post("/api/v65/queue")
def add_queue_item(request: QueueRequest) -> dict:
    if request.task_type == "media_edit" and not (request.payload.get("edit") or request.payload).get("path"):
        raise HTTPException(400, "An edit media path is required")
    if request.task_type == "movie_import" and not request.payload.get("source"):
        raise HTTPException(400, "An import source path is required")
    return enqueue(request.task_type, request.payload, request.label)


@app.get("/api/v65/queue")
def list_queue(limit: int = 200) -> dict:
    limit = max(1, min(limit, 500))
    with connection() as db:
        rows = db.execute("SELECT * FROM task_queue ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END,id DESC LIMIT ?", (limit,)).fetchall()
        counts = {row["status"]: row["amount"] for row in db.execute("SELECT status,count(*) amount FROM task_queue GROUP BY status")}
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item.pop("result_json", None)
        items.append(item)
    return {"paused": queue_paused(), "counts": counts, "items": items}


@app.put("/api/v65/queue/control")
def control_queue(request: QueueAction) -> dict:
    paused = request.action == "pause"
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO task_queue_settings(key,value) VALUES('paused',?)", ("1" if paused else "0",))
    logger.info("task_queue event=%s", "paused" if paused else "resumed")
    wake_queue()
    return {"paused": paused}


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
