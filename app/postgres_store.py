"""PostgreSQL bootstrap and durable-configuration migration helpers.

This module is intentionally isolated from the legacy SQLite connection until
the application schema cut-over is complete. It provides the new database
contract without mutating the existing database during development.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

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
        db.execute("""
            CREATE TABLE IF NOT EXISTS workflow_luws (
                luw_id UUID PRIMARY KEY,
                group_id UUID REFERENCES workflow_groups(group_id) ON DELETE SET NULL,
                resource_key TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('immediate','queued','maintenance')),
                status TEXT NOT NULL CHECK (status IN ('planned','preflighted','waiting','locked','applying','verifying','committed','rolled_back','failed','cancelled','obsolete')),
                idempotency_key TEXT NOT NULL UNIQUE,
                planned_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                input_signature JSONB,
                output_signature JSONB,
                rollback_plan JSONB,
                current_step TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                priority_class TEXT NOT NULL DEFAULT 'user',
                boost_until TIMESTAMPTZ,
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS workflow_luws_runnable
                ON workflow_luws(status, priority_class, boost_until, created_at);
            CREATE INDEX IF NOT EXISTS workflow_luws_resource
                ON workflow_luws(resource_key, status);
            CREATE TABLE IF NOT EXISTS workflow_luw_events (
                event_id BIGSERIAL PRIMARY KEY,
                luw_id UUID NOT NULL REFERENCES workflow_luws(luw_id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                step TEXT,
                message TEXT,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS workflow_luw_events_lookup
                ON workflow_luw_events(luw_id, event_id);
            CREATE TABLE IF NOT EXISTS workflow_luw_journal (
                journal_id BIGSERIAL PRIMARY KEY,
                luw_id UUID NOT NULL REFERENCES workflow_luws(luw_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                resource_path TEXT,
                before_value JSONB,
                after_value JSONB,
                checksum TEXT,
                committed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS workflow_luw_journal_lookup
                ON workflow_luw_journal(luw_id, journal_id);
            CREATE TABLE IF NOT EXISTS workflow_luw_locks (
                resource_key TEXT PRIMARY KEY,
                luw_id UUID NOT NULL REFERENCES workflow_luws(luw_id) ON DELETE CASCADE,
                lease_until TIMESTAMPTZ NOT NULL,
                heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS media_read_model_state (
                resource_key TEXT PRIMARY KEY,
                media_signature JSONB,
                common_index_version BIGINT NOT NULL DEFAULT 0,
                subtitle_index_version BIGINT NOT NULL DEFAULT 0,
                language_index_version BIGINT NOT NULL DEFAULT 0,
                voice_index_version BIGINT NOT NULL DEFAULT 0,
                plex_index_version BIGINT NOT NULL DEFAULT 0,
                common_index_stale BOOLEAN NOT NULL DEFAULT TRUE,
                subtitle_index_stale BOOLEAN NOT NULL DEFAULT TRUE,
                language_index_stale BOOLEAN NOT NULL DEFAULT TRUE,
                voice_index_stale BOOLEAN NOT NULL DEFAULT TRUE,
                plex_index_stale BOOLEAN NOT NULL DEFAULT TRUE,
                stale_reason TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS media_read_model_state_stale
                ON media_read_model_state(common_index_stale, subtitle_index_stale, language_index_stale, voice_index_stale, plex_index_stale);
        """)


# --- staged execution bridge -------------------------------------------------
# The legacy task tables remain the UI-facing queue during migration. These
# helpers make every new PostgreSQL-backed task participate in the staged
# workflow immediately, without duplicating the existing task handlers.
import json
import uuid


def _workflow_id(group_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(group_id))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f"videostreamedit:group:{group_id}")


def _lock_workflow_mutation(db) -> None:
    """Serialize tiny workflow metadata transactions across queue workers.

    Stage registration inserts a group and then a stage while stage completion
    updates the stage and its group. PostgreSQL FK checks can otherwise take
    those relation locks in opposite order when an enqueue races completion,
    producing a deadlock even though media processing itself is independent.
    """
    db.execute("SELECT pg_advisory_xact_lock(hashtext('videostreamedit:workflow-mutation-v2'))")


def register_task_stage(group_id: str | None, task_type: str, path: str, payload: dict, task_id: int | None = None) -> None:
    if not group_id or not DATABASE_URL:
        return
    workflow_id = _workflow_id(group_id)
    with connection() as db:
        _lock_workflow_mutation(db)
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
        _lock_workflow_mutation(db)
        # Reconcile pending stages with their exact queue task and group.
        # Cancelled/deleted/recovered tasks must not leave an orphaned
        # predecessor blocking the rest of a workflow forever.
        db.execute(
            """UPDATE workflow_stages stage
               SET status=CASE task.status
                              WHEN 'succeeded' THEN 'succeeded'
                              WHEN 'cancelled' THEN 'cancelled'
                              WHEN 'failed' THEN 'failed'
                              ELSE stage.status
                          END,
                   error=CASE WHEN task.status IN ('failed','cancelled')
                              THEN COALESCE(task.error, 'Queue task was not completed')
                              ELSE stage.error END,
                   finished_at=CASE WHEN task.status IN ('succeeded','failed','cancelled')
                                    THEN COALESCE(stage.finished_at, now())
                                    ELSE stage.finished_at END,
                   updated_at=now()
               FROM task_queue task
               WHERE stage.group_id=%s
                 AND stage.status='pending'
                 AND task.id::text=stage.payload->>'task_id'
                 AND task.group_id=%s
                 AND task.status IN ('succeeded','failed','cancelled')""",
            (workflow_id, group_id),
        )
        db.execute(
            """UPDATE workflow_stages stage
               SET status='cancelled',
                   error=COALESCE(stage.error, 'Orphaned workflow stage: queue task is missing from this group'),
                   finished_at=COALESCE(stage.finished_at, now()), updated_at=now()
               WHERE stage.group_id=%s AND stage.status='pending'
                 AND stage.payload->>'task_id' IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM task_queue task
                      WHERE task.id::text=stage.payload->>'task_id'
                        AND task.group_id=%s
                 )""",
            (workflow_id, group_id),
        )
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
        _lock_workflow_mutation(db)
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
        _lock_workflow_mutation(db)
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
        _lock_workflow_mutation(db)
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


# --- phase 1 LUW execution primitives ---------------------------------------
#
# These records deliberately sit beside the existing bridge while handlers are
# migrated. They provide one durable unit per media resource and do not require
# full-file staging for metadata-only operations.

def _luw_uuid(value: str | uuid.UUID | None = None) -> uuid.UUID:
    if value:
        try:
            return uuid.UUID(str(value))
        except (ValueError, AttributeError):
            return uuid.uuid5(uuid.NAMESPACE_URL, f"videostreamedit:luw:{value}")
    return uuid.uuid4()


def create_luw(
    resource_key: str,
    operation_type: str,
    mode: str,
    payload: dict | None = None,
    input_signature: dict | None = None,
    group_id: str | uuid.UUID | None = None,
    idempotency_key: str | None = None,
    priority_class: str = "user",
    boost_until=None,
) -> str | None:
    """Create or return an idempotent LUW for one media resource."""
    if not DATABASE_URL:
        return None
    payload = payload or {}
    key = idempotency_key or f"{mode}:{operation_type}:{resource_key}:{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
    luw_id = _luw_uuid()
    group_uuid = _workflow_id(str(group_id)) if group_id else None
    with connection() as db:
        row = db.execute(
            "SELECT luw_id FROM workflow_luws WHERE idempotency_key=%s",
            (key,),
        ).fetchone()
        if row:
            return str(row["luw_id"])
        db.execute(
            """INSERT INTO workflow_luws
               (luw_id, group_id, resource_key, operation_type, mode, status,
                idempotency_key, planned_payload, input_signature, priority_class, boost_until)
               VALUES (%s,%s,%s,%s,%s,'planned',%s,%s::jsonb,%s::jsonb,%s,%s)""",
            (
                luw_id, group_uuid, resource_key, operation_type, mode, key,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(input_signature or {}, ensure_ascii=False),
                priority_class, boost_until,
            ),
        )
        db.execute(
            """INSERT INTO workflow_luw_events(luw_id,state,step,message,details)
               VALUES (%s,'planned','create','LUW planned',%s::jsonb)""",
            (luw_id, json.dumps({"operation_type": operation_type, "resource_key": resource_key})),
        )
    return str(luw_id)


def record_luw_event(luw_id: str, state: str, step: str | None = None, message: str = "", details: dict | None = None) -> None:
    if not DATABASE_URL:
        return
    uid = _luw_uuid(luw_id)
    with connection() as db:
        db.execute(
            """INSERT INTO workflow_luw_events(luw_id,state,step,message,details)
               VALUES (%s,%s,%s,%s,%s::jsonb)""",
            (uid, state, step, message[:2000], json.dumps(details or {}, ensure_ascii=False)),
        )


def transition_luw(luw_id: str, state: str, step: str | None = None, message: str = "", error: str | None = None, output_signature: dict | None = None) -> bool:
    """Durably move an LUW state and append the matching event."""
    if not DATABASE_URL:
        return False
    uid = _luw_uuid(luw_id)
    terminal = state in {"committed", "rolled_back", "failed", "cancelled", "obsolete"}
    with connection() as db:
        changed = db.execute(
            """UPDATE workflow_luws
               SET status=%s, current_step=%s, error=%s,
                   output_signature=COALESCE(%s::jsonb, output_signature),
                   started_at=CASE WHEN %s IN ('locked','applying','verifying') THEN COALESCE(started_at,now()) ELSE started_at END,
                   finished_at=CASE WHEN %s THEN now() ELSE finished_at END,
                   updated_at=now()
             WHERE luw_id=%s AND status NOT IN ('committed','rolled_back','failed','cancelled','obsolete')""",
            (
                state, step, (error or "")[-4000:] or None,
                json.dumps(output_signature, ensure_ascii=False) if output_signature is not None else None,
                state, terminal, uid,
            ),
        ).rowcount
        if not changed:
            return False
        db.execute(
            """INSERT INTO workflow_luw_events(luw_id,state,step,message,details)
               VALUES (%s,%s,%s,%s,%s::jsonb)""",
            (uid, state, step, message[:2000], json.dumps({"error": error} if error else {}, ensure_ascii=False)),
        )
    return True


def acquire_luw_lock(luw_id: str, resource_key: str, lease_seconds: int = 1800) -> bool:
    if not DATABASE_URL:
        return False
    uid = _luw_uuid(luw_id)
    with connection() as db:
        row = db.execute(
            """INSERT INTO workflow_luw_locks(resource_key,luw_id,lease_until)
               VALUES (%s,%s,now() + (%s * interval '1 second'))
               ON CONFLICT(resource_key) DO UPDATE
               SET luw_id=EXCLUDED.luw_id, lease_until=EXCLUDED.lease_until,
                   heartbeat_at=now()
               WHERE workflow_luw_locks.lease_until < now()
                  OR workflow_luw_locks.luw_id=EXCLUDED.luw_id
               RETURNING resource_key""",
            (resource_key, uid, max(30, lease_seconds)),
        ).fetchone()
        if not row:
            return False
        db.execute(
            "UPDATE workflow_luws SET status='locked',updated_at=now() WHERE luw_id=%s AND status IN ('planned','preflighted','waiting','locked')",
            (uid,),
        )
    record_luw_event(str(uid), "locked", "lock", f"Locked {resource_key}")
    return True


def release_luw_lock(luw_id: str, resource_key: str) -> None:
    if not DATABASE_URL:
        return
    uid = _luw_uuid(luw_id)
    with connection() as db:
        db.execute("DELETE FROM workflow_luw_locks WHERE resource_key=%s AND luw_id=%s", (resource_key, uid))


def append_luw_journal(
    luw_id: str,
    kind: str,
    resource_path: str | None = None,
    before_value: dict | None = None,
    after_value: dict | None = None,
    checksum: str | None = None,
) -> int | None:
    if not DATABASE_URL:
        return None
    uid = _luw_uuid(luw_id)
    with connection() as db:
        row = db.execute(
            """INSERT INTO workflow_luw_journal
               (luw_id,kind,resource_path,before_value,after_value,checksum)
               VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s)
               RETURNING journal_id""",
            (
                uid, kind, resource_path,
                json.dumps(before_value or {}, ensure_ascii=False),
                json.dumps(after_value or {}, ensure_ascii=False),
                checksum,
            ),
        ).fetchone()
    return int(row["journal_id"]) if row else None


def mark_read_models_stale(resource_key: str, operation_type: str, signature: dict | None = None) -> None:
    """Mark only affected read-model families stale after a committed LUW."""
    if not DATABASE_URL or not resource_key:
        return
    operation = str(operation_type or "")
    all_indexes = operation in {"movie_import", "plex_import_refresh", "media_edit", "filtered_stream_edit", "filtered_stream_edit_now", "tv_filtered_stream_edit", "tv_filtered_stream_edit_now"}
    subtitle = all_indexes or "subtitle" in operation or "html" in operation or "image" in operation or "ocr" in operation
    language = all_indexes or "language" in operation or subtitle
    voice = all_indexes or "audio" in operation or "voice" in operation
    common = all_indexes or subtitle or language or voice or "reindex" in operation
    plex = all_indexes or operation in {"movie_import", "plex_import_refresh"}
    with connection() as db:
        db.execute(
            """INSERT INTO media_read_model_state(
                       resource_key,media_signature,common_index_stale,subtitle_index_stale,
                       language_index_stale,voice_index_stale,plex_index_stale,stale_reason,updated_at)
               VALUES (%s,%s::jsonb,%s,%s,%s,%s,%s,%s,now())
               ON CONFLICT(resource_key) DO UPDATE SET
                 media_signature=COALESCE(EXCLUDED.media_signature,media_read_model_state.media_signature),
                 common_index_stale=media_read_model_state.common_index_stale OR EXCLUDED.common_index_stale,
                 subtitle_index_stale=media_read_model_state.subtitle_index_stale OR EXCLUDED.subtitle_index_stale,
                 language_index_stale=media_read_model_state.language_index_stale OR EXCLUDED.language_index_stale,
                 voice_index_stale=media_read_model_state.voice_index_stale OR EXCLUDED.voice_index_stale,
                 plex_index_stale=media_read_model_state.plex_index_stale OR EXCLUDED.plex_index_stale,
                 stale_reason=EXCLUDED.stale_reason,updated_at=now()""",
            (resource_key, json.dumps(signature or {}, ensure_ascii=False), common, subtitle, language, voice, plex, f"LUW committed: {operation}"),
        )


def mark_read_models_fresh(resource_key: str, family: str, signature: dict | None = None) -> None:
    """Clear one index family's stale flag after a successful incremental rebuild."""
    if not DATABASE_URL or not resource_key:
        return
    column_map = {
        "core": ("common_index_stale", "common_index_version"),
        "common": ("common_index_stale", "common_index_version"),
        "subtitles": ("subtitle_index_stale", "subtitle_index_version"),
        "language": ("language_index_stale", "language_index_version"),
        "voice": ("voice_index_stale", "voice_index_version"),
        "plex": ("plex_index_stale", "plex_index_version"),
    }
    stale_column, version_column = column_map.get(str(family), (None, None))
    if not stale_column:
        return
    with connection() as db:
        db.execute(
            f"""INSERT INTO media_read_model_state(resource_key,media_signature,{stale_column},{version_column},stale_reason,updated_at)
                VALUES (%s,%s::jsonb,FALSE,1,NULL,now())
                ON CONFLICT(resource_key) DO UPDATE SET
                  media_signature=COALESCE(EXCLUDED.media_signature,media_read_model_state.media_signature),
                  {stale_column}=FALSE,
                  {version_column}=media_read_model_state.{version_column}+1,
                  stale_reason=NULL,
                  updated_at=now()""",
            (resource_key, json.dumps(signature or {}, ensure_ascii=False)),
        )



def read_model_summary() -> dict:
    """Return aggregate stale/current counts for each indexed read-model family."""
    if not DATABASE_URL:
        return {"media": 0, "families": {}}
    with connection() as db:
        row = db.execute(
            """SELECT count(*) AS media,
                      count(*) FILTER (WHERE common_index_stale) AS common_stale,
                      count(*) FILTER (WHERE subtitle_index_stale) AS subtitles_stale,
                      count(*) FILTER (WHERE language_index_stale) AS language_stale,
                      count(*) FILTER (WHERE voice_index_stale) AS voice_stale,
                      count(*) FILTER (WHERE plex_index_stale) AS plex_stale
               FROM media_read_model_state"""
        ).fetchone()
    total = int(row["media"])
    return {
        "media": total,
        "families": {
            "common": {"stale": int(row["common_stale"]), "current": total - int(row["common_stale"])},
            "subtitles": {"stale": int(row["subtitles_stale"]), "current": total - int(row["subtitles_stale"])},
            "language": {"stale": int(row["language_stale"]), "current": total - int(row["language_stale"])},
            "voice": {"stale": int(row["voice_stale"]), "current": total - int(row["voice_stale"])},
            "plex": {"stale": int(row["plex_stale"]), "current": total - int(row["plex_stale"])},
        },
    }



def read_model_state(resource: str | None = None, limit: int = 200) -> list[dict]:
    if not DATABASE_URL:
        return []
    limit = max(1, min(int(limit), 500))
    if resource and resource.strip():
        where, params = " WHERE resource_key ILIKE %s", [f"%{resource.strip()}%"]
    else:
        where, params = "", []
    with connection() as db:
        rows = db.execute(f"SELECT * FROM media_read_model_state{where} ORDER BY updated_at DESC LIMIT %s", [*params, limit]).fetchall()
    return [dict(row) for row in rows]



def commit_luw(luw_id: str, output_signature: dict | None = None) -> bool:
    ok = transition_luw(luw_id, "committed", "commit", "LUW committed", output_signature=output_signature)
    if not ok:
        return False
    uid = _luw_uuid(luw_id)
    with connection() as db:
        row = db.execute("SELECT resource_key,operation_type FROM workflow_luws WHERE luw_id=%s", (uid,)).fetchone()
        db.execute("UPDATE workflow_luw_journal SET committed=TRUE WHERE luw_id=%s", (uid,))
        db.execute("DELETE FROM workflow_luw_locks WHERE luw_id=%s", (uid,))
    if row:
        mark_read_models_stale(str(row["resource_key"]), str(row["operation_type"]), output_signature)
    return True


def recover_luws() -> int:
    """Return interrupted LUWs to waiting and release their leases on startup."""
    if not DATABASE_URL:
        return 0
    with connection() as db:
        rows = db.execute(
            "SELECT luw_id FROM workflow_luws WHERE status IN ('locked','applying','verifying')"
        ).fetchall()
        if not rows:
            return 0
        ids = [row["luw_id"] for row in rows]
        db.execute("DELETE FROM workflow_luw_locks WHERE luw_id = ANY(%s)", (ids,))
        db.execute(
            """UPDATE workflow_luws
               SET status='waiting', current_step='recovered',
                   error='Recovered after application restart', updated_at=now()
             WHERE luw_id = ANY(%s)""",
            (ids,),
        )
        for uid in ids:
            db.execute(
                """INSERT INTO workflow_luw_events(luw_id,state,step,message)
                   VALUES (%s,'waiting','recovered','Recovered after application restart')""",
                (uid,),
            )
    return len(ids)


def luw_read_model(limit: int = 200, status: str | None = None, resource: str | None = None) -> dict:
    """Return compact durable operation state for the redesigned task UI."""
    if not DATABASE_URL:
        return {"counts": {}, "items": []}
    limit = max(1, min(int(limit), 500))
    allowed = {"planned", "preflighted", "waiting", "locked", "applying", "verifying", "committed", "rolled_back", "failed", "cancelled", "obsolete"}
    if status and status not in allowed:
        raise ValueError("Unsupported LUW status")
    clauses = []
    params: list[object] = []
    if status:
        clauses.append("l.status=%s")
        params.append(status)
    if resource and resource.strip():
        clauses.append("l.resource_key ILIKE %s")
        params.append(f"%{resource.strip()}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connection() as db:
        counts = db.execute("SELECT status,count(*) AS amount FROM workflow_luws GROUP BY status ORDER BY status").fetchall()
        rows = db.execute(
            f"""SELECT l.luw_id,l.group_id,l.resource_key,l.operation_type,l.mode,l.status,
                       l.current_step,l.attempts,l.priority_class,l.boost_until,
                       l.error,l.created_at,l.started_at,l.finished_at,l.updated_at,
                       (SELECT count(*) FROM workflow_luw_events e WHERE e.luw_id=l.luw_id) AS event_count,
                       (SELECT count(*) FROM workflow_luw_journal j WHERE j.luw_id=l.luw_id) AS journal_count
                FROM workflow_luws l{where}
                ORDER BY l.updated_at DESC,l.created_at DESC
                LIMIT %s""",
            [*params, limit],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["luw_id"] = str(item["luw_id"])
        if item.get("group_id"):
            item["group_id"] = str(item["group_id"])
        items.append(item)
    return {"counts": {str(row["status"]): int(row["amount"]) for row in counts}, "items": items}



def workflow_read_model(limit: int = 100, status: str | None = None) -> dict:
    """Return parent workflows and stage summaries without raw payloads."""
    if not DATABASE_URL:
        return {"counts": {}, "groups": []}
    limit = max(1, min(int(limit), 300))
    allowed = {"pending", "running", "waiting", "succeeded", "failed", "cancelled", "blocked"}
    if status and status not in allowed:
        raise ValueError("Unsupported workflow status")
    clauses = []
    params: list[object] = []
    if status:
        clauses.append("g.status=%s")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connection() as db:
        counts = db.execute("SELECT status,count(*) AS amount FROM workflow_groups GROUP BY status ORDER BY status").fetchall()
        groups = db.execute(
            f"""SELECT g.group_id,g.kind,g.resource_key,g.status,g.current_stage,
                       g.error,g.created_at,g.started_at,g.finished_at,g.updated_at,
                       (SELECT count(*) FROM workflow_stages s WHERE s.group_id=g.group_id) AS stage_count,
                       (SELECT count(*) FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status='succeeded') AS succeeded_stages,
                       (SELECT count(*) FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status='failed') AS failed_stages
                FROM workflow_groups g{where}
                ORDER BY g.updated_at DESC,g.created_at DESC
                LIMIT %s""",
            [*params, limit],
        ).fetchall()
        stage_rows = db.execute(
            """SELECT s.group_id,s.stage_number,s.task_type,s.status,s.attempts,
                      s.error,s.created_at,s.started_at,s.finished_at,s.updated_at
               FROM workflow_stages s
               JOIN workflow_groups g ON g.group_id=s.group_id
               """ + (where.replace("g.status", "g.status") if where else "") + """
               ORDER BY s.group_id,s.stage_number""",
            params,
        ).fetchall()
    stages_by_group: dict[str, list[dict]] = {}
    for row in stage_rows:
        item = dict(row)
        gid = str(item.pop("group_id"))
        stages_by_group.setdefault(gid, []).append(item)
    result = []
    for row in groups:
        item = dict(row)
        gid = str(item["group_id"])
        item["group_id"] = gid
        item["stages"] = stages_by_group.get(gid, [])
        result.append(item)
    return {"counts": {str(row["status"]): int(row["amount"]) for row in counts}, "groups": result}



def luw_details(luw_id: str) -> dict | None:
    if not DATABASE_URL:
        return None
    uid = _luw_uuid(luw_id)
    with connection() as db:
        luw = db.execute("SELECT * FROM workflow_luws WHERE luw_id=%s", (uid,)).fetchone()
        if not luw:
            return None
        events = db.execute("SELECT event_id,state,step,message,details,created_at FROM workflow_luw_events WHERE luw_id=%s ORDER BY event_id", (uid,)).fetchall()
        journal = db.execute("SELECT journal_id,kind,resource_path,before_value,after_value,checksum,committed,created_at FROM workflow_luw_journal WHERE luw_id=%s ORDER BY journal_id", (uid,)).fetchall()
    return {"luw": dict(luw), "events": [dict(row) for row in events], "journal": [dict(row) for row in journal]}
