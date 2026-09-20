from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from uuid import uuid4
from pathlib import Path

from fastapi import HTTPException

from pydantic import BaseModel, Field

from app.v11 import connection
from app.v22 import _normalize_clone_state, app, normalized_clone_state

logger = logging.getLogger("videostreamedit")


class CloneHistoryInspectRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=250)
    templates: list[dict] = Field(min_length=1, max_length=10)


class TemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    scope: str = Field(default="media", max_length=32)
    before: dict
    after: dict
    changes: list[str] = Field(default_factory=list, max_length=64)


class BulkTemplateQueueItem(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    edit: dict


class BulkTemplateQueueRequest(BaseModel):
    items: list[BulkTemplateQueueItem] = Field(min_length=1, max_length=300)
    template_id: str | None = Field(default=None, max_length=64)


class TemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = None


@app.on_event("startup")
def initialize_durable_templates() -> None:
    """Create the durable template store; browser history remains a fallback during rollout."""
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS stream_change_templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'media',
                schema_version INTEGER NOT NULL DEFAULT 1,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                changes_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                use_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_stream_change_templates_enabled ON stream_change_templates(enabled, updated_at)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS template_bulk_runs (
                id TEXT PRIMARY KEY,
                template_id TEXT,
                status TEXT NOT NULL DEFAULT 'preparing',
                total INTEGER NOT NULL DEFAULT 0,
                queued INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                conflicts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_template_bulk_runs_status ON template_bulk_runs(status, updated_at)")


def _template_row(row: dict) -> dict:
    result = dict(row)
    for key, fallback in (("before_json", {}), ("after_json", {}), ("changes_json", [])):
        raw = result.pop(key, None)
        try:
            result[key.removesuffix("_json")] = json.loads(raw) if raw else fallback
        except (TypeError, ValueError):
            result[key.removesuffix("_json")] = fallback
    result["enabled"] = bool(result.get("enabled", 1))
    return result


@app.get("/api/v25/templates")
def list_durable_templates(include_disabled: bool = False) -> dict:
    with connection() as db:
        if include_disabled:
            rows = db.execute("SELECT * FROM stream_change_templates ORDER BY use_count DESC, updated_at DESC LIMIT 100").fetchall()
        else:
            rows = db.execute("SELECT * FROM stream_change_templates WHERE enabled=1 ORDER BY use_count DESC, updated_at DESC LIMIT 100").fetchall()
    return {"templates": [_template_row(row) for row in rows]}


@app.post("/api/v25/templates")
def create_durable_template(request: TemplateCreateRequest) -> dict:
    template_id = uuid4().hex
    with connection() as db:
        db.execute("""
            INSERT INTO stream_change_templates
              (id,name,description,scope,before_json,after_json,changes_json)
            VALUES (?,?,?,?,?,?,?)
        """, (template_id, request.name.strip(), request.description.strip(), request.scope.strip() or "media",
              json.dumps(request.before, ensure_ascii=False, separators=(",", ":")),
              json.dumps(request.after, ensure_ascii=False, separators=(",", ":")),
              json.dumps(request.changes, ensure_ascii=False)))
        row = db.execute("SELECT * FROM stream_change_templates WHERE id=?", (template_id,)).fetchone()
    return {"template": _template_row(row)}


@app.put("/api/v25/templates/{template_id}")
def update_durable_template(template_id: str, request: TemplateUpdateRequest) -> dict:
    fields, values = [], []
    if request.name is not None:
        fields.append("name=?"); values.append(request.name.strip())
    if request.description is not None:
        fields.append("description=?"); values.append(request.description.strip())
    if request.enabled is not None:
        fields.append("enabled=?"); values.append(1 if request.enabled else 0)
    if not fields:
        raise HTTPException(status_code=400, detail="No template fields supplied")
    fields.append("updated_at=CURRENT_TIMESTAMP")
    values.append(template_id)
    with connection() as db:
        db.execute(f"UPDATE stream_change_templates SET {', '.join(fields)} WHERE id=?", tuple(values))
        row = db.execute("SELECT * FROM stream_change_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"template": _template_row(row)}


@app.delete("/api/v25/templates")
def delete_all_durable_templates() -> dict:
    with connection() as db:
        result = db.execute("DELETE FROM stream_change_templates")
    return {"deleted": result.rowcount or 0}


@app.delete("/api/v25/templates/{template_id}")
def delete_durable_template(template_id: str) -> dict:
    with connection() as db:
        result = db.execute("DELETE FROM stream_change_templates WHERE id=?", (template_id,))
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"deleted": template_id}


@app.post("/api/v25/templates/bulk-queue")
def queue_durable_template_bulk(request: BulkTemplateQueueRequest) -> dict:
    """Queue one signature-checked media edit per compatible media item."""
    from app import v65 as tasks

    run_id = uuid4().hex
    items = list(request.items)
    with connection() as db:
        db.execute("INSERT INTO template_bulk_runs(id,template_id,total) VALUES(?,?,?)", (run_id, request.template_id, len(items)))
    queued: list[int] = []
    skipped: list[dict] = []
    conflicts: list[dict] = []
    for item in items:
        if not Path(item.path).is_file():
            skipped.append({"path": item.path, "reason": "Media file is not accessible"})
            continue
        with connection() as db:
            active = db.execute("""
                SELECT task_queue.id, task_queue.task_type, task_queue.label
                FROM media_change_request
                JOIN task_queue ON task_queue.id=media_change_request.task_id
                WHERE media_change_request.path=? AND task_queue.status IN ('pending','running')
                ORDER BY task_queue.id
                LIMIT 5
            """, (item.path,)).fetchall()
        if active:
            conflicts.append({"path": item.path, "tasks": [dict(row) for row in active], "reason": "Another media change is already queued or running"})
            continue
        edit = dict(item.edit)
        edit["path"] = item.path
        if not any(value not in (None, "", [], {}) for key, value in edit.items() if key != "path"):
            skipped.append({"path": item.path, "reason": "No effective stream changes"})
            continue
        try:
            result = tasks.enqueue(
                "media_edit",
                {"edit": edit, "mode": "queue", "template_id": request.template_id, "_template_run_id": run_id},
                label=f"Template change · {Path(item.path).name}",
            )
            queued.append(int(result["id"]))
        except Exception as exc:
            logger.warning("template_bulk event=item_skipped path=%s error=%s", item.path, str(exc).replace("\n", " ")[-300:])
            skipped.append({"path": item.path, "reason": str(exc)})
    status = "queued" if queued and not (skipped or conflicts) else ("partial" if queued else "blocked")
    with connection() as db:
        db.execute("UPDATE template_bulk_runs SET status=?,queued=?,skipped=?,conflicts=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, len(queued), len(skipped), len(conflicts), run_id))
        if request.template_id and queued:
            db.execute("UPDATE stream_change_templates SET use_count=use_count+1,last_used_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (request.template_id,))
    logger.info("template_bulk event=queued run=%s queued=%d skipped=%d conflicts=%d", run_id, len(queued), len(skipped), len(conflicts))
    return {"run_id": run_id, "status": status, "queued": len(queued), "skipped": skipped, "conflicts": conflicts, "task_ids": queued}


@app.get("/api/v25/template-runs/{run_id}")
def template_bulk_run(run_id: str) -> dict:
    with connection() as db:
        row = db.execute("SELECT * FROM template_bulk_runs WHERE id=?", (run_id,)).fetchone()
        tasks = db.execute("SELECT id,status,task_type,label,progress_message,error,started_at,finished_at FROM task_queue WHERE payload_json LIKE ? ORDER BY id", (f'%"_template_run_id":"{run_id}"%',)).fetchall()
    if not row:
        raise HTTPException(status_code=404, detail="Template run not found")
    task_rows = [dict(item) for item in tasks]
    terminal = {"completed", "failed", "cancelled"}
    if task_rows and all(item.get("status") in terminal for item in task_rows):
        live_status = "completed" if all(item.get("status") == "completed" for item in task_rows) else "completed_with_errors"
        with connection() as db:
            db.execute("UPDATE template_bulk_runs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (live_status, run_id))
        row = {**dict(row), "status": live_status}
    return {"run": dict(row), "tasks": task_rows}


@app.post("/api/v25/templates/{template_id}/use")
def use_durable_template(template_id: str) -> dict:
    with connection() as db:
        db.execute("UPDATE stream_change_templates SET use_count=use_count+1, last_used_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?", (template_id,))
        row = db.execute("SELECT * FROM stream_change_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"template": _template_row(row)}


def inspect_history_path(path: str) -> tuple[str, dict | None]:
    try:
        return path, normalized_clone_state(path)
    except Exception:
        return path, None


@app.post("/api/v25/tv/clone/history/inspect")
def inspect_clone_history(request: CloneHistoryInspectRequest) -> dict:
    with ThreadPoolExecutor(max_workers=min(4, len(request.paths))) as executor:
        states = list(executor.map(inspect_history_path, request.paths))
    matches = []
    for index, template in enumerate(request.templates):
        expected = template.get("before")
        normalized_expected = _normalize_clone_state(expected)
        candidates = [path for path, state in states if state is not None and _normalize_clone_state(state) == normalized_expected]
        if candidates:
            matches.append({"template_index": index, "count": len(candidates), "candidates": candidates})
    return {"checked": len(states), "matches": matches}
