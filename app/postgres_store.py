"""PostgreSQL bootstrap and durable-configuration migration helpers.

This module is intentionally isolated from the legacy SQLite connection until
the application schema cut-over is complete. It provides the new database
contract without mutating the existing database during development.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.environ.get("DATABASE_URL", "")


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as db:
        yield db


def initialize_schema() -> None:
    """Create the new workflow-oriented PostgreSQL schema."""
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS workflow_groups (
                group_id UUID PRIMARY KEY,
                kind TEXT NOT NULL,
                resource_key TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending','running','waiting','succeeded','failed','cancelled','blocked')),
                current_stage INTEGER NOT NULL DEFAULT 0,
                definition JSONB NOT NULL DEFAULT '{}'::jsonb,
                input_signature JSONB,
                output_signature JSONB,
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS workflow_groups_runnable
                ON workflow_groups(status, updated_at);
            CREATE TABLE IF NOT EXISTS workflow_stages (
                stage_id BIGSERIAL PRIMARY KEY,
                group_id UUID NOT NULL REFERENCES workflow_groups(group_id) ON DELETE CASCADE,
                stage_number INTEGER NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','cancelled','blocked')),
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                input_signature JSONB,
                output_signature JSONB,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE(group_id, stage_number)
            );
            CREATE INDEX IF NOT EXISTS workflow_stages_runnable
                ON workflow_stages(status, task_type, stage_number);
            CREATE TABLE IF NOT EXISTS workflow_locks (
                resource_key TEXT PRIMARY KEY,
                group_id UUID NOT NULL REFERENCES workflow_groups(group_id) ON DELETE CASCADE,
                stage_id BIGINT REFERENCES workflow_stages(stage_id) ON DELETE CASCADE,
                lease_until TIMESTAMPTZ NOT NULL,
                heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS workflow_artifacts (
                artifact_id BIGSERIAL PRIMARY KEY,
                group_id UUID NOT NULL REFERENCES workflow_groups(group_id) ON DELETE CASCADE,
                stage_id BIGINT REFERENCES workflow_stages(stage_id) ON DELETE CASCADE,
                original_path TEXT,
                artifact_path TEXT NOT NULL,
                kind TEXT NOT NULL,
                checksum TEXT,
                status TEXT NOT NULL DEFAULT 'owned',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS workflow_artifacts_group
                ON workflow_artifacts(group_id, stage_id);
        """)


# --- staged execution bridge -------------------------------------------------
# The legacy task tables remain the UI-facing queue during migration. These
# helpers make every new PostgreSQL-backed task participate in the staged
# workflow immediately, without duplicating the existing task handlers.
import json
import uuid
from datetime import timedelta


def _workflow_id(group_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(group_id))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f"videostreamedit:group:{group_id}")


def register_task_stage(group_id: str | None, task_type: str, path: str, payload: dict, task_id: int | None = None) -> None:
    if not group_id or not DATABASE_URL:
        return
    workflow_id = _workflow_id(group_id)
    with connection() as db:
        db.execute(
            """INSERT INTO workflow_groups(group_id, kind, resource_key, status, definition, input_signature)
               VALUES (%s, %s, %s, 'pending', %s::jsonb, %s::jsonb)
               ON CONFLICT (group_id) DO NOTHING""",
            (workflow_id, task_type, path or None, json.dumps({"task_type": task_type}), json.dumps(payload.get("_media_signature") or {})),
        )
        row = db.execute("SELECT COALESCE(MAX(stage_number), -1) + 1 AS next_stage FROM workflow_stages WHERE group_id=%s", (workflow_id,)).fetchone()
        stage_number = int(row["next_stage"])
        db.execute(
            """INSERT INTO workflow_stages(group_id, stage_number, task_type, status, payload, input_signature)
               VALUES (%s, %s, %s, 'pending', %s::jsonb, %s::jsonb)""",
            (workflow_id, stage_number, task_type, json.dumps({"task_id": task_id, "path": path, "payload": payload}), json.dumps(payload.get("_media_signature") or {})),
        )


def begin_task_stage(group_id: str | None, task_type: str, path: str, task_id: int | None = None) -> bool:
    if not group_id or not DATABASE_URL:
        return True
    workflow_id = _workflow_id(group_id)
    resource = path or f"task:{workflow_id}"
    with connection() as db:
        stage = db.execute(
            """SELECT stage_id FROM workflow_stages current
               WHERE current.group_id=%s AND current.task_type=%s AND current.status='pending'
                 AND current.payload->>'task_id'=%s
                 AND NOT EXISTS (
                     SELECT 1 FROM workflow_stages earlier
                      WHERE earlier.group_id=current.group_id
                        AND earlier.stage_number < current.stage_number
                        AND earlier.status NOT IN ('succeeded','cancelled')
                 )
               ORDER BY current.stage_number LIMIT 1""",
            (workflow_id, task_type, str(task_id)),
        ).fetchone()
        if not stage:
            return False
        lock = db.execute(
            """INSERT INTO workflow_locks(resource_key, group_id, stage_id, lease_until)
               VALUES (%s, %s, %s, now() + interval '30 minutes')
               ON CONFLICT (resource_key) DO UPDATE
               SET group_id=EXCLUDED.group_id, stage_id=EXCLUDED.stage_id,
                   lease_until=EXCLUDED.lease_until, heartbeat_at=now()
               WHERE workflow_locks.lease_until < now()
                  OR workflow_locks.group_id=EXCLUDED.group_id
               RETURNING resource_key""",
            (resource, workflow_id, stage["stage_id"]),
        ).fetchone()
        if not lock:
            return False
        db.execute("UPDATE workflow_stages SET status='running', started_at=now(), attempts=attempts+1, updated_at=now() WHERE stage_id=%s", (stage["stage_id"],))
        db.execute("UPDATE workflow_groups SET status='running', current_stage=(SELECT stage_number FROM workflow_stages WHERE stage_id=%s), started_at=COALESCE(started_at, now()), updated_at=now() WHERE group_id=%s", (stage["stage_id"], workflow_id))
    return True


def finish_task_stage(group_id: str | None, task_type: str, path: str, result: dict, task_id: int | None = None) -> None:
    if not group_id or not DATABASE_URL:
        return
    workflow_id = _workflow_id(group_id)
    resource = path or f"task:{workflow_id}"
    with connection() as db:
        stage = db.execute("SELECT stage_id FROM workflow_stages WHERE group_id=%s AND task_type=%s AND status='running' AND payload->>'task_id'=%s ORDER BY stage_number LIMIT 1", (workflow_id, task_type, str(task_id))).fetchone()
        if not stage:
            return
        db.execute("UPDATE workflow_stages SET status='succeeded', output_signature=%s::jsonb, finished_at=now(), updated_at=now() WHERE stage_id=%s", (json.dumps(result or {}), stage["stage_id"]))
        db.execute("DELETE FROM workflow_locks WHERE resource_key=%s AND group_id=%s", (resource, workflow_id))
        pending = db.execute("SELECT 1 FROM workflow_stages WHERE group_id=%s AND status IN ('pending','running') LIMIT 1", (workflow_id,)).fetchone()
        db.execute("UPDATE workflow_groups SET status=%s, finished_at=CASE WHEN %s THEN now() ELSE finished_at END, updated_at=now() WHERE group_id=%s", ('running' if pending else 'succeeded', not pending, workflow_id))


def fail_task_stage(group_id: str | None, task_type: str, path: str, error: str, task_id: int | None = None) -> None:
    if not group_id or not DATABASE_URL:
        return
    workflow_id = _workflow_id(group_id)
    resource = path or f"task:{workflow_id}"
    with connection() as db:
        stage = db.execute("SELECT stage_id FROM workflow_stages WHERE group_id=%s AND task_type=%s AND status='running' AND payload->>'task_id'=%s ORDER BY stage_number LIMIT 1", (workflow_id, task_type, str(task_id))).fetchone()
        if not stage:
            return
        db.execute("UPDATE workflow_stages SET status='failed', error=%s, finished_at=now(), updated_at=now() WHERE stage_id=%s", (error[-4000:], stage["stage_id"]))
        db.execute("DELETE FROM workflow_locks WHERE resource_key=%s AND group_id=%s", (resource, workflow_id))
        db.execute("UPDATE workflow_groups SET status='failed', error=%s, updated_at=now() WHERE group_id=%s", (error[-4000:], workflow_id))


def reset_task_stage_for_retry(group_id: str | None, task_id: int | None = None) -> None:
    """Reopen the exact failed stage when a queue item is explicitly retried."""
    if not group_id or not DATABASE_URL:
        return
    workflow_id = _workflow_id(group_id)
    with connection() as db:
        db.execute(
            """UPDATE workflow_stages
               SET status='pending', error=NULL, started_at=NULL, finished_at=NULL, updated_at=now()
             WHERE group_id=%s AND payload->>'task_id'=%s AND status IN ('failed','succeeded')""",
            (workflow_id, str(task_id)),
        )
        db.execute(
            """UPDATE workflow_groups
               SET status='pending', error=NULL, finished_at=NULL, updated_at=now()
             WHERE group_id=%s AND status IN ('failed','succeeded')""",
            (workflow_id,),
        )


def task_stage_exists(group_id: str | None, task_type: str, task_id: int | None = None) -> bool:
    if not group_id or not DATABASE_URL:
        return False
    workflow_id = _workflow_id(group_id)
    with connection() as db:
        row = db.execute(
            "SELECT 1 FROM workflow_stages WHERE group_id=%s AND task_type=%s AND payload->>'task_id'=%s LIMIT 1",
            (workflow_id, task_type, str(task_id)),
        ).fetchone()
    return bool(row)

# --- artifact snapshots and rollback ----------------------------------------
import hashlib
import shutil
from pathlib import Path

MUTATING_TASK_TYPES = frozenset({
    "media_edit", "movie_import", "filtered_stream_edit", "filtered_stream_edit_now",
    "tv_filtered_stream_edit", "tv_filtered_stream_edit_now", "subtitle_html_cleanup",
    "image_subtitle_convert", "ocr_rollback", "plex_import_refresh",
})


def _stage_root() -> Path:
    root = Path(os.environ.get("WORKFLOW_STAGE_ROOT", "/data/workflow-staging"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_task_artifact(group_id: str | None, task_id: int, task_type: str, path: str, payload: dict) -> None:
    """Snapshot a mutating input before its handler can touch the media file."""
    if not group_id or task_type not in MUTATING_TASK_TYPES or not path or not DATABASE_URL:
        return
    source = Path(path)
    if not source.is_file():
        return
    workflow_id = _workflow_id(group_id)
    with connection() as db:
        stage = db.execute(
            "SELECT stage_id FROM workflow_stages WHERE group_id=%s AND payload->>'task_id'=%s AND status='running' LIMIT 1",
            (workflow_id, str(task_id)),
        ).fetchone()
        if not stage:
            return
        existing = db.execute("SELECT artifact_id FROM workflow_artifacts WHERE group_id=%s AND stage_id=%s AND original_path=%s LIMIT 1", (workflow_id, stage["stage_id"], str(source))).fetchone()
        if existing:
            return
    destination_dir = _stage_root() / str(workflow_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{task_id}-{source.name}"
    shutil.copy2(source, destination)
    checksum = _file_digest(destination)
    with connection() as db:
        db.execute(
            """INSERT INTO workflow_artifacts(group_id, stage_id, original_path, artifact_path, kind, checksum, status)
               VALUES (%s,%s,%s,%s,'media-original',%s,'owned')
               ON CONFLICT DO NOTHING""",
            (workflow_id, stage["stage_id"], str(source), str(destination), checksum),
        )


def commit_task_artifacts(group_id: str | None, task_id: int) -> None:
    if not group_id or not DATABASE_URL:
        return
    workflow_id = _workflow_id(group_id)
    with connection() as db:
        db.execute("UPDATE workflow_artifacts SET status='committed' WHERE group_id=%s AND stage_id IN (SELECT stage_id FROM workflow_stages WHERE group_id=%s AND payload->>'task_id'=%s)", (workflow_id, workflow_id, str(task_id)))


def cleanup_succeeded_workflow_group_artifacts(group_id: str | None) -> int:
    """Delete transient snapshots only after the complete workflow succeeds."""
    if not group_id or not DATABASE_URL:
        return 0
    workflow_id = _workflow_id(group_id)
    with connection() as db:
        group = db.execute("SELECT status FROM workflow_groups WHERE group_id=%s", (workflow_id,)).fetchone()
        if not group or group["status"] != "succeeded":
            return 0
        artifacts = db.execute("SELECT artifact_id,artifact_path FROM workflow_artifacts WHERE group_id=%s", (workflow_id,)).fetchall()
    removed = 0
    for artifact in artifacts:
        source = Path(str(artifact["artifact_path"]))
        try:
            if source.is_file():
                source.unlink()
                removed += 1
        except OSError:
            continue
    with connection() as db:
        db.execute("DELETE FROM workflow_artifacts WHERE group_id=%s", (workflow_id,))
    stage_dir = _stage_root() / str(workflow_id)
    try:
        if stage_dir.is_dir() and not any(stage_dir.iterdir()):
            stage_dir.rmdir()
    except OSError:
        pass
    return removed


def cleanup_succeeded_workflow_artifacts() -> int:
    """Startup maintenance for snapshots left behind by a prior clean success."""
    if not DATABASE_URL:
        return 0
    with connection() as db:
        groups = db.execute("SELECT group_id FROM workflow_groups WHERE status='succeeded' AND EXISTS (SELECT 1 FROM workflow_artifacts WHERE workflow_artifacts.group_id=workflow_groups.group_id)").fetchall()
    return sum(cleanup_succeeded_workflow_group_artifacts(str(row["group_id"])) for row in groups)


def rollback_workflow(group_id: str) -> dict:
    """Restore every preserved artifact for a workflow and mark it cancelled."""
    workflow_id = _workflow_id(group_id)
    restored = 0
    with connection() as db:
        artifacts = db.execute("SELECT artifact_id,original_path,artifact_path,status FROM workflow_artifacts WHERE group_id=%s ORDER BY artifact_id", (workflow_id,)).fetchall()
    for artifact in artifacts:
        source = Path(str(artifact["artifact_path"]))
        target = Path(str(artifact["original_path"]))
        if not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.rollback-{workflow_id.hex[:8]}")
        shutil.copy2(source, temporary)
        temporary.replace(target)
        restored += 1
        with connection() as db:
            db.execute("UPDATE workflow_artifacts SET status='rolled-back' WHERE artifact_id=%s", (artifact["artifact_id"],))
    with connection() as db:
        db.execute("UPDATE workflow_stages SET status='cancelled', finished_at=now(), updated_at=now() WHERE group_id=%s AND status IN ('pending','running','failed')", (workflow_id,))
        db.execute("UPDATE workflow_groups SET status='cancelled', finished_at=now(), updated_at=now(), error=NULL WHERE group_id=%s", (workflow_id,))
        db.execute("DELETE FROM workflow_locks WHERE group_id=%s", (workflow_id,))
    return {"group_id": str(workflow_id), "restored": restored}


def workflow_details(group_id: str) -> dict:
    """Return a compact, read-only view of a staged workflow for the UI."""
    workflow_id = _workflow_id(group_id)
    with connection() as db:
        group = db.execute("SELECT * FROM workflow_groups WHERE group_id=%s", (workflow_id,)).fetchone()
        if not group:
            return {}
        stages = db.execute(
            "SELECT stage_id,stage_number,task_type,status,attempts,error,created_at,started_at,finished_at,updated_at,payload,output_signature FROM workflow_stages WHERE group_id=%s ORDER BY stage_number",
            (workflow_id,),
        ).fetchall()
        artifacts = db.execute(
            "SELECT artifact_id,stage_id,original_path,artifact_path,kind,checksum,status,created_at FROM workflow_artifacts WHERE group_id=%s ORDER BY artifact_id",
            (workflow_id,),
        ).fetchall()
        locks = db.execute(
            "SELECT resource_key,stage_id,lease_until,heartbeat_at FROM workflow_locks WHERE group_id=%s ORDER BY resource_key",
            (workflow_id,),
        ).fetchall()
    return {
        "group": dict(group),
        "stages": [dict(row) for row in stages],
        "artifacts": [dict(row) for row in artifacts],
        "locks": [dict(row) for row in locks],
    }
