from fastapi import Query

from app.v11 import connection
from app.v77 import app


@app.get("/api/v78/change-requested")
def change_requested(path: str = Query(min_length=1)) -> dict:
    with connection() as db:
        rows = db.execute(
            """SELECT queue.id,queue.status,queue.task_type,queue.label,queue.payload_json,marker.requested_at
                 FROM media_change_request marker
                 JOIN task_queue queue ON queue.id=marker.task_id
                WHERE marker.path=? AND queue.status IN ('pending','running','failed') ORDER BY queue.id""",
            (path,),
        ).fetchall()
    from app.v19 import queued_change_summary
    requests = [{"id": row["id"], "status": row["status"], "requested_at": row["requested_at"], "summary": queued_change_summary(row["task_type"], row["label"], row["payload_json"])} for row in rows]
    return {"change_requested": bool(requests), "requests": requests}
