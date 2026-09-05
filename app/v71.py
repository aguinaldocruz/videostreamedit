from __future__ import annotations

from pathlib import Path

import app.v65 as tasks
from app.v51 import SubtitleCleanup, apply_subtitle_cleanup
from app.v70 import app


base_media_edit_task = tasks.TASK_HANDLERS["media_edit"]


def media_edit_with_html_cleanup(task_id: int, payload: dict) -> dict:
    cleanups = payload.get("html_cleanups") or []
    for number, cleanup in enumerate(cleanups, 1):
        tasks.update_progress(task_id, number - 1, len(cleanups) + 2, f"Removing HTML tags from subtitle {number}")
        apply_subtitle_cleanup(SubtitleCleanup.model_validate(cleanup))
    return base_media_edit_task(task_id, payload)


tasks.TASK_HANDLERS["media_edit"] = media_edit_with_html_cleanup
