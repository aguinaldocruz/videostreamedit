import logging

import app.v28 as movie_import
import app.v65 as tasks
from app.v28 import ImportCleanupRequest, MovieImportRequest
from app.v72 import app


logger = logging.getLogger("uvicorn.error")


def process_movie_import(task_id: int, payload: dict) -> dict:
    remove_original = bool(payload.pop("remove_original", False))
    request = MovieImportRequest.model_validate(payload)
    tasks.update_progress(task_id, 0, 4, "Validating and copying movie")
    result = movie_import.import_movie(request)
    tasks.update_progress(task_id, 3, 4, "Movie imported and stream changes applied")
    if remove_original:
        tasks.update_progress(task_id, 3, 4, "Removing original movie and subtitles")
        try:
            result["originals_removed"] = movie_import.cleanup_import_source(
                ImportCleanupRequest(source=request.source)
            )["removed"]
        except Exception as exc:
            warning = str(getattr(exc, "detail", exc))
            result.setdefault("warnings", []).append(f"Import succeeded, but originals were retained: {warning}")
            logger.warning("task_queue event=import_original_cleanup_failed id=%d source=%s error=%s", task_id, request.source.replace("\n", "\\n"), warning.replace("\n", " ")[-500:])
    tasks.update_progress(task_id, 4, 4, "Movie import completed")
    return result


tasks.TASK_HANDLERS["movie_import"] = process_movie_import
