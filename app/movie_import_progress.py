"""Short-lived progress for an immediate import; no media copies or new jobs."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()
_lock = threading.Lock()
_runs: dict[str, dict] = {}


def start(operation_id: str, total: int = 6) -> None:
    with _lock:
        expired = [key for key, run in _runs.items() if run['status'] != 'running'
                   and time.monotonic() - run['_updated'] > 3600]
        for key in expired:
            del _runs[key]
        if operation_id in _runs:
            raise HTTPException(409, 'This import request was already submitted; do not repeat it')
        if len(_runs) >= 200:
            finished = sorted((key for key in _runs if _runs[key]['status'] != 'running'),
                              key=lambda key: _runs[key]['_updated'])
            for key in finished[:max(1, len(_runs) - 199)]:
                del _runs[key]
        _runs[operation_id] = {'status': 'running', 'step': 1, 'total': total,
                               'message': 'Validating media import', 'detail': 'Checking source and destination',
                               '_updated': time.monotonic()}


def update(operation_id: str, step: int, message: str, detail: str = '', *,
           copy_path: Path | None = None, copy_bytes: int = 0) -> None:
    with _lock:
        run = _runs.get(operation_id)
        if run is not None:
            run.update(step=step, message=message, detail=detail, _copy_path=copy_path,
                       _copy_bytes=copy_bytes, _updated=time.monotonic())


def finish(operation_id: str, error: str = '') -> None:
    with _lock:
        run = _runs.get(operation_id)
        if run is not None:
            run.update(status='failed' if error else 'completed',
                       message='Media import failed' if error else 'Media import completed',
                       detail=error or run.get('detail', ''), _copy_path=None, _updated=time.monotonic())
            if not error:
                run['step'] = run['total']


@router.get('/api/v28/import/progress/{operation_id}')
def status(operation_id: str) -> dict:
    with _lock:
        run = dict(_runs.get(operation_id) or {})
    if not run:
        raise HTTPException(404, 'Import progress is not available yet')
    result = {key: value for key, value in run.items() if not key.startswith('_')}
    copy_path, expected = run.get('_copy_path'), run.get('_copy_bytes', 0)
    if copy_path and expected > 0:
        try:
            copied = min(copy_path.stat().st_size, expected)
        except OSError:
            copied = 0
        result.update(copy_current=copied, copy_total=expected,
                      copy_percent=min(100, round(100 * copied / expected)))
    return result
