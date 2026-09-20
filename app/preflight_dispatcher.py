"""Durable, lightweight preflight dispatcher.

The dispatcher validates and plans requests before expensive queue work is
created.  Phase 1 provides durable requests, leases, signature checks, a
small fingerprint cache, and a worker/API; operation-specific handlers are
registered by later phases.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.v11 import connection
from app.v15 import app

logger = logging.getLogger("uvicorn.error")
_dispatcher_shutdown = threading.Event()
_dispatcher_condition = threading.Condition()
_dispatcher_thread: threading.Thread | None = None
_handlers: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {}
_approval_handlers: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {}
LEASE_SECONDS = 120
CACHE_TTL_SECONDS = 300


class PreflightRequest(BaseModel):
    operation_type: str = Field(min_length=1, max_length=100)
    media_path: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    mode: str = Field(default="queued", pattern="^(immediate|queued|maintenance)$")
    priority: int = Field(default=50, ge=0, le=1000)
    deduplicate: bool = True


def _now() -> str:
    # Database timestamps are intentionally stored as UTC ISO text for the
    # existing PostgreSQL/compatibility layer.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fingerprint(path: str) -> dict[str, Any]:
    value = {"path": path, "exists": False}
    try:
        stat = Path(path).stat()
    except (FileNotFoundError, OSError):
        return value
    value.update({"exists": True, "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns), "inode": int(getattr(stat, "st_ino", 0))})
    return value


def _dedupe_key(operation_type: str, media_path: str, payload: dict[str, Any]) -> str:
    stable = {key: value for key, value in payload.items() if key not in {"_request_id", "_created_at"}}
    encoded = json.dumps([operation_type, media_path, stable], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def ensure_tables() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS preflight_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                media_path TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'queued',
                priority INTEGER NOT NULL DEFAULT 50,
                status TEXT NOT NULL DEFAULT 'pending',
                dedupe_key TEXT NOT NULL UNIQUE,
                fingerprint_json TEXT,
                result_json TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_until TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS preflight_requests_runnable ON preflight_requests(status, priority, id);
            CREATE INDEX IF NOT EXISTS preflight_requests_media ON preflight_requests(media_path, status);
            CREATE TABLE IF NOT EXISTS preflight_cache (
                cache_key TEXT PRIMARY KEY,
                media_path TEXT NOT NULL,
                fingerprint_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS preflight_cache_expiry ON preflight_cache(expires_at);
            CREATE TABLE IF NOT EXISTS preflight_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO preflight_settings(key,value) VALUES('cleanup_enabled','0');
            INSERT OR IGNORE INTO preflight_settings(key,value) VALUES('retention_days','30');
            INSERT OR IGNORE INTO preflight_settings(key,value) VALUES('cleanup_interval_hours','24');
            INSERT OR IGNORE INTO preflight_settings(key,value) VALUES('last_cleanup_at','');
        """)
        # Recover requests whose worker disappeared.
        db.execute("UPDATE preflight_requests SET status='pending', lease_until=NULL, updated_at=? WHERE status='running' AND lease_until < ?", (_now(), _now()))
        db.execute("DELETE FROM preflight_cache WHERE expires_at < ?", (_now(),))


def enqueue_preflight(operation_type: str, media_path: str = "", payload: dict[str, Any] | None = None, *, mode: str = "queued", priority: int = 50, deduplicate: bool = True) -> dict[str, Any]:
    payload = dict(payload or {})
    media_path = str(media_path or payload.get("path") or "")
    key = _dedupe_key(operation_type, media_path, payload)
    now = _now()
    with connection() as db:
        if deduplicate:
            existing = db.execute("SELECT * FROM preflight_requests WHERE dedupe_key=? AND status IN ('pending','running') ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if existing:
                return _row(existing)
        cursor = db.execute(
            "INSERT INTO preflight_requests(operation_type,media_path,payload_json,mode,priority,status,dedupe_key,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
            (operation_type, media_path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), mode, priority, key, now, now),
        )
        request_id = cursor.lastrowid
    with _dispatcher_condition:
        _dispatcher_condition.notify_all()
    logger.info("preflight event=queued id=%s operation=%s path=%s", request_id, operation_type, media_path.replace("\n", "\\n"))
    return get_request(int(request_id))


def enqueue_bulk_preflight(operation_type: str, items: list[dict[str, Any]], *, mode: str = "queued", priority: int = 50, deduplicate: bool = True) -> dict[str, Any]:
    """Submit one durable preflight request for a bulk selection."""
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        path = str(item.get("path") or "").strip()
        key = (path, str(item.get("source") or "embedded"), int(item.get("type_index", -1)), str(item.get("external_path") or ""))
        item_copy = dict(item)
        item_copy["_preflight_fingerprint"] = _fingerprint(path)
        unique[json.dumps(key, ensure_ascii=False)] = item_copy
    payload = {"_bulk_items": list(unique.values()), "_bulk_count": len(unique)}
    return enqueue_preflight(operation_type, "", payload, mode=mode, priority=priority, deduplicate=deduplicate)


def _row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in ("payload_json", "fingerprint_json", "result_json"):
        raw = result.pop(key, None)
        result[key.removesuffix("_json")] = json.loads(raw) if raw else None
    return result


def get_request(request_id: int) -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT * FROM preflight_requests WHERE id=?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Preflight request not found")
    return _row(row)


def _cache_key(operation_type: str, media_path: str, fingerprint: dict[str, Any], payload: dict[str, Any]) -> str:
    return _dedupe_key(operation_type, media_path, {"fingerprint": fingerprint, "payload": payload})


def register_handler(operation_type: str, handler: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> None:
    """Register a lightweight operation-specific validator."""
    _handlers[str(operation_type)] = handler


def register_approval_handler(operation_type: str, handler: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> None:
    """Register the callback that creates execution work after approval."""
    _approval_handlers[str(operation_type)] = handler


def _default_validate(payload: dict[str, Any], fingerprint: dict[str, Any]) -> dict[str, Any]:
    path = str(payload.get("path") or payload.get("media_path") or fingerprint.get("path") or "")
    if path and not fingerprint.get("exists"):
        return {"decision": "invalid", "reason": "Media file is not accessible", "path": path}
    expected = payload.get("_media_signature") or {}
    if expected and path and fingerprint.get("exists"):
        expected_size = expected.get("size")
        expected_mtime = expected.get("mtime_ns")
        if expected_size is not None and int(expected_size) != int(fingerprint.get("size", -1)):
            return {"decision": "stale", "reason": "Media changed after request was created", "path": path}
        if expected_mtime is not None and int(expected_mtime) not in {int(fingerprint.get("mtime_ns", -1)), int(fingerprint.get("mtime", -1))}:
            return {"decision": "stale", "reason": "Media changed after request was created", "path": path}
    return {"decision": "approved", "path": path, "fingerprint": fingerprint}


def _process_bulk(request_id: int, row: Any, payload: dict[str, Any], operation: str) -> None:
    items = list(payload.get("_bulk_items") or [])
    handler = _handlers.get(operation)
    outcomes: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    for item in items:
        item_payload = dict(item)
        path = str(item_payload.get("path") or "")
        fingerprint = _fingerprint(path)
        fingerprints.append(fingerprint)
        try:
            result = handler(item_payload, fingerprint) if handler else _default_validate(item_payload, fingerprint)
        except Exception as exc:
            result = {"decision": "invalid", "reason": str(exc)}
        decision = str(result.get("decision") or "failed")
        outcome = {"path": path, "source": item_payload.get("source", "embedded"), "type_index": item_payload.get("type_index"), "external_path": item_payload.get("external_path"), "decision": decision, "reason": result.get("reason", "")}
        outcomes.append(outcome)
        if decision == "approved":
            item_payload["_preflight_result"] = result
            approved.append(item_payload)
    result: dict[str, Any] = {
        "decision": "approved" if approved else ("skipped" if outcomes and all(item["decision"] == "skipped" for item in outcomes) else "invalid"),
        "bulk": True, "requested": len(items), "approved": len(approved),
        "skipped": sum(item["decision"] == "skipped" for item in outcomes),
        "invalid": sum(item["decision"] not in {"approved", "skipped"} for item in outcomes),
        "items": outcomes, "fingerprints": fingerprints,
    }
    if approved and operation in _approval_handlers:
        approval = _approval_handlers[operation]({"_bulk_items": approved}, result) or {}
        result["execution"] = approval
    status = "approved" if approved else ("skipped" if result["decision"] == "skipped" else "failed")
    error = None if status in {"approved", "skipped"} else "No bulk items passed preflight"
    now = _now()
    with connection() as db:
        db.execute("UPDATE preflight_requests SET status=?,fingerprint_json=?,result_json=?,error=?,finished_at=?,lease_until=NULL,updated_at=? WHERE id=?", (status, json.dumps(fingerprints), json.dumps(result, ensure_ascii=False), error, now, now, request_id))
    logger.info("preflight event=bulk_completed id=%d operation=%s requested=%d approved=%d skipped=%d invalid=%d", request_id, operation, len(items), len(approved), result["skipped"], result["invalid"])


def _process(request_id: int) -> None:
    with connection() as db:
        row = db.execute("SELECT * FROM preflight_requests WHERE id=?", (request_id,)).fetchone()
    if not row:
        return
    payload = json.loads(row["payload_json"] or "{}")
    operation = str(row["operation_type"])
    if payload.get("_bulk_items") is not None:
        _process_bulk(request_id, row, payload, operation)
        return
    media_path = str(row["media_path"] or payload.get("path") or "")
    fingerprint = _fingerprint(media_path) if media_path else {"path": "", "exists": True}
    key = _cache_key(operation, media_path, fingerprint, payload)
    result: dict[str, Any] | None = None
    with connection() as db:
        cached = db.execute("SELECT result_json FROM preflight_cache WHERE cache_key=? AND expires_at>=?", (key, _now())).fetchone()
    if cached:
        result = json.loads(cached[0])
        result["cache_hit"] = True
    else:
        handler = _handlers.get(operation)
        result = handler(payload, fingerprint) if handler else _default_validate(payload, fingerprint)
        with connection() as db:
            db.execute("INSERT OR REPLACE INTO preflight_cache(cache_key,media_path,fingerprint_json,result_json,created_at,expires_at) VALUES(?,?,?,?,?,?)", (key, media_path, json.dumps(fingerprint), json.dumps(result, ensure_ascii=False), _now(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + CACHE_TTL_SECONDS))))
    decision = str(result.get("decision") or "failed")
    status = {"approved": "approved", "skip": "skipped", "skipped": "skipped"}.get(decision, "failed")
    if status == "approved" and operation in _approval_handlers:
        # Execution creation is deliberately after validation and persisted as
        # part of the result. The callback must remain lightweight and create
        # only the minimal child task(s).
        approval = _approval_handlers[operation](payload, result) or {}
        result["execution"] = approval
    error = None if status in {"approved", "skipped"} else str(result.get("reason") or "Preflight validation failed")
    with connection() as db:
        db.execute("UPDATE preflight_requests SET status=?,fingerprint_json=?,result_json=?,error=?,finished_at=?,lease_until=NULL,updated_at=? WHERE id=?", (status, json.dumps(fingerprint), json.dumps(result, ensure_ascii=False), error, _now(), _now(), request_id))
    logger.info("preflight event=completed id=%d operation=%s status=%s cache=%s", request_id, operation, status, bool(result.get("cache_hit")))


def _cleanup_if_due() -> None:
    with connection() as db:
        values = {str(row["key"]): str(row["value"]) for row in db.execute("SELECT key,value FROM preflight_settings").fetchall()}
    if values.get("cleanup_enabled") != "1":
        return
    try:
        interval = max(1, int(values.get("cleanup_interval_hours", "24")))
    except ValueError:
        interval = 24
    last = values.get("last_cleanup_at") or ""
    due = True
    if last:
        try:
            due = time.time() - calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ")) >= interval * 3600
        except ValueError:
            due = True
    if not due:
        return
    try:
        days = max(1, int(values.get("retention_days", "30")))
    except ValueError:
        days = 30
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    with connection() as db:
        deleted = int(db.execute("DELETE FROM preflight_requests WHERE status IN ('approved','skipped','failed','cancelled') AND finished_at IS NOT NULL AND finished_at < ?", (cutoff,)).rowcount or 0)
        db.execute("INSERT OR REPLACE INTO preflight_settings(key,value) VALUES('last_cleanup_at',?)", (_now(),))
    logger.info("preflight event=scheduled_retention_cleanup deleted=%d retention_days=%d", deleted, days)


def _worker() -> None:
    logger.info("preflight event=worker_started")
    while not _dispatcher_shutdown.is_set():
        request_id = None
        _cleanup_if_due()
        now = _now()
        lease = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + LEASE_SECONDS))
        with connection() as db:
            row = db.execute("SELECT id FROM preflight_requests WHERE status='pending' ORDER BY priority DESC,id LIMIT 1").fetchone()
            if row:
                changed = db.execute("UPDATE preflight_requests SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,?),lease_until=?,updated_at=? WHERE id=? AND status='pending'", (now, lease, now, row["id"])).rowcount
                if changed:
                    request_id = int(row["id"])
        if request_id is None:
            with _dispatcher_condition:
                _dispatcher_condition.wait(timeout=1.0)
            continue
        try:
            _process(request_id)
        except Exception as exc:
            with connection() as db:
                db.execute("UPDATE preflight_requests SET status='failed',error=?,finished_at=?,lease_until=NULL,updated_at=? WHERE id=?", (str(exc)[-4000:], _now(), _now(), request_id))
            logger.exception("preflight event=failed id=%d", request_id)


def start_dispatcher() -> None:
    global _dispatcher_thread
    ensure_tables()
    _dispatcher_shutdown.clear()
    if not _dispatcher_thread or not _dispatcher_thread.is_alive():
        _dispatcher_thread = threading.Thread(target=_worker, name="vse-preflight-dispatcher", daemon=True)
        _dispatcher_thread.start()


def stop_dispatcher() -> None:
    _dispatcher_shutdown.set()
    with _dispatcher_condition:
        _dispatcher_condition.notify_all()
    if _dispatcher_thread:
        _dispatcher_thread.join(timeout=10)


@app.post("/api/v89/preflight")
def create_preflight(request: PreflightRequest) -> dict[str, Any]:
    return enqueue_preflight(request.operation_type, request.media_path, request.payload, mode=request.mode, priority=request.priority, deduplicate=request.deduplicate)


class PreflightCleanupRequest(BaseModel):
    older_than_days: int = Field(default=30, ge=1, le=3650)


@app.get("/api/v89/preflight/metrics")
def preflight_metrics() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute("SELECT status,COUNT(*) AS count,AVG(CASE WHEN started_at IS NOT NULL AND finished_at IS NOT NULL THEN EXTRACT(EPOCH FROM (finished_at::timestamptz-started_at::timestamptz)) END) AS avg_seconds FROM preflight_requests GROUP BY status ORDER BY status").fetchall()
        operation_rows = db.execute("SELECT operation_type,COUNT(*) AS count FROM preflight_requests GROUP BY operation_type ORDER BY count DESC,operation_type").fetchall()
        result_rows = db.execute("SELECT result_json FROM preflight_requests WHERE result_json IS NOT NULL").fetchall()
    child_ids: set[int] = set()
    for row in result_rows:
        try:
            execution = (json.loads(row["result_json"] or "{}").get("execution") or {})
            child_ids.update(int(value) for value in (execution.get("task_ids") or []) if str(value).isdigit())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    child_statuses: dict[str, int] = {}
    if child_ids:
        with connection() as db:
            for task_id in child_ids:
                task = db.execute("SELECT status FROM task_queue WHERE id=?", (task_id,)).fetchone()
                if task:
                    key = str(task["status"]); child_statuses[key] = child_statuses.get(key, 0) + 1
    statuses = {str(row["status"]): {"count": int(row["count"] or 0), "avg_seconds": round(float(row["avg_seconds"] or 0), 2)} for row in rows}
    total = sum(item["count"] for item in statuses.values())
    terminal = sum(statuses.get(name, {}).get("count", 0) for name in ("approved", "skipped", "failed", "cancelled"))
    approved = statuses.get("approved", {}).get("count", 0)
    return {"total": total, "terminal": terminal, "approval_rate": round(approved / terminal * 100, 1) if terminal else 0, "statuses": statuses, "operations": [{"operation_type": str(row["operation_type"]), "count": int(row["count"] or 0)} for row in operation_rows], "child_tasks": child_statuses, "child_total": sum(child_statuses.values())}


@app.get("/api/v89/preflight/settings")
def preflight_settings() -> dict[str, Any]:
    ensure_tables()
    with connection() as db:
        values = {str(row["key"]): str(row["value"]) for row in db.execute("SELECT key,value FROM preflight_settings").fetchall()}
    return {"cleanup_enabled": values.get("cleanup_enabled") == "1", "retention_days": int(values.get("retention_days", "30")), "cleanup_interval_hours": int(values.get("cleanup_interval_hours", "24")), "last_cleanup_at": values.get("last_cleanup_at") or None}


class PreflightSettingsUpdate(BaseModel):
    cleanup_enabled: bool = False
    retention_days: int = Field(default=30, ge=1, le=3650)
    cleanup_interval_hours: int = Field(default=24, ge=1, le=720)


@app.put("/api/v89/preflight/settings")
def update_preflight_settings(request: PreflightSettingsUpdate) -> dict[str, Any]:
    ensure_tables()
    with connection() as db:
        for key, value in (("cleanup_enabled", "1" if request.cleanup_enabled else "0"), ("retention_days", str(request.retention_days)), ("cleanup_interval_hours", str(request.cleanup_interval_hours))):
            db.execute("INSERT OR REPLACE INTO preflight_settings(key,value) VALUES(?,?)", (key, value))
    return preflight_settings()


@app.post("/api/v89/preflight/cleanup")
def cleanup_preflight(request: PreflightCleanupRequest) -> dict[str, Any]:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - request.older_than_days * 86400))
    with connection() as db:
        result = db.execute("DELETE FROM preflight_requests WHERE status IN ('approved','skipped','failed','cancelled') AND finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
        deleted = int(result.rowcount or 0)
    logger.info("preflight event=retention_cleanup deleted=%d older_than_days=%d", deleted, request.older_than_days)
    return {"deleted": deleted, "older_than_days": request.older_than_days, "cutoff": cutoff}


@app.get("/api/v89/preflight/{request_id}")
def preflight_status(request_id: int) -> dict[str, Any]:
    return get_request(request_id)


@app.post("/api/v89/preflight/{request_id}/cancel")
def cancel_preflight(request_id: int) -> dict[str, Any]:
    now = _now()
    with connection() as db:
        row = db.execute("SELECT * FROM preflight_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Preflight request not found")
        if row["status"] != "pending":
            raise HTTPException(409, "Only queued preflight requests can be cancelled safely")
        db.execute("UPDATE preflight_requests SET status='cancelled',error=?,finished_at=?,lease_until=NULL,updated_at=? WHERE id=? AND status='pending'", ("Cancelled by user", now, now, request_id))
    logger.info("preflight event=cancelled id=%d", request_id)
    return get_request(request_id)


@app.post("/api/v89/preflight/{request_id}/retry")
def retry_preflight(request_id: int) -> dict[str, Any]:
    now = _now()
    with connection() as db:
        row = db.execute("SELECT * FROM preflight_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Preflight request not found")
        if row["status"] not in {"failed", "skipped", "cancelled"}:
            raise HTTPException(409, "Only failed, skipped, or cancelled preflight requests can be retried")
        db.execute("UPDATE preflight_requests SET status='pending',error=NULL,result_json=NULL,fingerprint_json=NULL,attempts=0,lease_until=NULL,started_at=NULL,finished_at=NULL,updated_at=? WHERE id=?", (now, request_id))
    with _dispatcher_condition:
        _dispatcher_condition.notify_all()
    logger.info("preflight event=retry_queued id=%d", request_id)
    return get_request(request_id)


@app.get("/api/v89/preflight")
def preflight_list(status: str | None = None, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    with connection() as db:
        if status:
            rows = db.execute("SELECT * FROM preflight_requests WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = db.execute("SELECT * FROM preflight_requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [_row(row) for row in rows]}


@app.on_event("startup")
def initialize_preflight_dispatcher() -> None:
    start_dispatcher()


@app.on_event("shutdown")
def shutdown_preflight_dispatcher() -> None:
    stop_dispatcher()
