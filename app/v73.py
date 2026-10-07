import logging

import app.v28 as movie_import
import app.v65 as tasks
from app.v28 import MovieImportRequest
from app.v72 import app  # noqa: F401 - importing this layer registers startup hooks

logger = logging.getLogger("uvicorn.error")


def process_movie_import(task_id: int, payload: dict) -> dict:
    request = MovieImportRequest.model_validate(payload)
    total = 7 if request.remove_original else 6
    result = movie_import.execute_movie_import(request, lambda step, message, detail='', **kw:
                                              tasks.update_progress(task_id, step - 1, total, message + (' · ' + detail if detail else '')))
    tasks.update_progress(task_id, total, total, 'Media import completed' + (' · source cleanup needs attention' if result.get('source_cleanup_status') == 'retained' else ''))
    return result


tasks.TASK_HANDLERS["movie_import"] = process_movie_import
