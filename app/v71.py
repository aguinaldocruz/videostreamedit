from __future__ import annotations

import app.v65 as tasks
from app.v51 import SubtitleCleanup, apply_subtitle_cleanups
from app.v69 import app  # noqa: F401 - imports the active application layers

base_media_edit_task = tasks.TASK_HANDLERS["media_edit"]


def media_edit_with_html_cleanup(task_id: int, payload: dict) -> dict:
    cleanups = payload.get("html_cleanups") or []
    if cleanups:
        requests = [SubtitleCleanup.model_validate(cleanup) for cleanup in cleanups]
        path = (payload.get("edit") or payload)["path"]
        apply_subtitle_cleanups(path, requests, operation_id=f"task-{task_id}-html",
                               progress=lambda step, message: tasks.update_progress(task_id, step, 8, message))
    return base_media_edit_task(task_id, payload)


tasks.TASK_HANDLERS["media_edit"] = media_edit_with_html_cleanup
