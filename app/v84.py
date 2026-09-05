from __future__ import annotations

from app.v11 import connection
from app.v83 import app


@app.get("/api/v84/activity")
def background_activity() -> dict:
    with connection() as db:
        counts = {row["status"]: row["amount"] for row in db.execute(
            "SELECT status,count(*) amount FROM task_queue WHERE status IN ('pending','running') GROUP BY status"
        )}
        running = [dict(row) for row in db.execute(
            "SELECT id,label,task_type,progress_message,progress_current,progress_total FROM task_queue WHERE status='running' ORDER BY id LIMIT 5"
        )]
    return {"active": bool(counts.get("pending") or counts.get("running")), "pending": counts.get("pending", 0), "running": counts.get("running", 0), "items": running}
