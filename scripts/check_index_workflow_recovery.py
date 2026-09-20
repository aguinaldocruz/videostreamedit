#!/usr/bin/env python3
"""Synthetic regression check for incremental index workflow recovery.

No media files are created. The script refuses to run while real index work is
active, inserts synthetic rows for all three queues, verifies recovery and
orphan handling, then removes every synthetic row in a finally block.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.postgres_store import connection as workflow_connection, register_task_stage
from app.v11 import connection as queue_connection
from app.v80 import ensure_queue_tables, reconcile_index_workflow_stages


def main() -> int:
    ensure_queue_tables()
    jobs = ("core", "subtitles", "previews")
    with queue_connection() as db:
        busy = db.execute("SELECT count(*) FROM index_task_queue WHERE status IN ('pending','running')").fetchone()[0]
        if busy:
            print(f"refusing self-test: {busy} real index item(s) are active", file=sys.stderr)
            return 2
        now = datetime.now(timezone.utc).isoformat()
        cases: list[tuple[str, str, int, str, str]] = []
        for job in jobs:
            recovered_group = str(uuid.uuid4())
            orphan_group = str(uuid.uuid4())
            path = f"/tmp/vse-index-workflow-self-test-{job}-{recovered_group}.mkv"
            row = db.execute(
                "INSERT INTO index_task_queue(job,path,reason,status,attempts,created_at,updated_at,started_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
                (job, path, "workflow self-test", "running", 1, now, now, now),
            ).fetchone()
            task_id = int(row["id"])
            cases.append((job, path, task_id, recovered_group, orphan_group))

    try:
        for job, path, task_id, recovered_group, orphan_group in cases:
            register_task_stage(recovered_group, f"index:{job}", path, {"_self_test": True}, task_id)
            register_task_stage(orphan_group, f"index:{job}", path, {"_self_test": True}, 999999999)
        with workflow_connection() as db:
            for _job, _path, task_id, recovered_group, _orphan_group in cases:
                db.execute(
                    "UPDATE workflow_stages SET status='cancelled', error='synthetic cancelled stage' WHERE group_id=%s AND payload->>'task_id'=%s",
                    (uuid.UUID(recovered_group), str(task_id)),
                )

        result = reconcile_index_workflow_stages()
        with workflow_connection() as db:
            for job, _path, task_id, recovered_group, orphan_group in cases:
                recovered = db.execute(
                    "SELECT status FROM workflow_stages WHERE group_id=%s AND payload->>'task_id'=%s",
                    (uuid.UUID(recovered_group), str(task_id)),
                ).fetchone()
                orphaned = db.execute(
                    "SELECT status FROM workflow_stages WHERE group_id=%s AND payload->>'task_id'='999999999'",
                    (uuid.UUID(orphan_group),),
                ).fetchone()
                if not recovered or recovered["status"] != "pending":
                    raise RuntimeError(f"{job}: cancelled runnable stage was not reopened: {recovered}")
                if not orphaned or orphaned["status"] != "cancelled":
                    raise RuntimeError(f"{job}: orphaned stage was not cancelled: {orphaned}")
        print(f"PASS queues={','.join(jobs)} recovered={result.get('reopened', 0)} orphaned={result.get('orphaned', 0)}")
        return 0
    finally:
        with workflow_connection() as db:
            groups = [uuid.UUID(value) for case in cases for value in (case[3], case[4])]
            for group in groups:
                db.execute("DELETE FROM workflow_groups WHERE group_id=%s", (group,))
        with queue_connection() as db:
            for _job, _path, task_id, _recovered_group, _orphan_group in cases:
                db.execute("DELETE FROM index_task_queue WHERE id=?", (task_id,))


if __name__ == "__main__":
    raise SystemExit(main())
