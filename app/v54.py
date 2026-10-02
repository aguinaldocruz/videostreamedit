"""Index processor registry and routes; execution belongs to the durable queue."""
from pathlib import Path
from app.v11 import column_exists, connection
from app.v38 import MovieStreamIndexInvalidate
from app.v51 import inspect_extended
from app.v53 import app

JOBS = ('core', 'subtitles')


@app.on_event('startup')
def initialize_split_indexes() -> None:
    with connection() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS subtitle_extended_media (
            path TEXT PRIMARY KEY, modified BIGINT NOT NULL, size BIGINT NOT NULL,
            indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            markup_version INTEGER NOT NULL DEFAULT 1);''')
        if not column_exists(db, 'subtitle_extended_media', 'markup_version'):
            db.execute('ALTER TABLE subtitle_extended_media ADD COLUMN markup_version INTEGER NOT NULL DEFAULT 1')


def index_core(item: dict) -> None:
    from app.v82 import unified_core_index
    unified_core_index(item)


def index_subtitles(item: dict) -> None:
    path = Path(item['path'])
    from app.matroska_layout import safe_checkpoint
    safe_checkpoint(path)
    if item.get('_subtitle_cache_only'):
        values = inspect_extended(path, item.get('_subtitle_text_cache'), cache_only=True,
                                  metadata=item.get('_subtitle_metadata'))
    else:
        values = inspect_extended(path, item.get('_subtitle_text_cache'))
    with connection() as db:
        db.execute('DELETE FROM subtitle_extended_index WHERE path=?', (str(path),))
        db.executemany('INSERT INTO subtitle_extended_index(path,source,type_index,external_path,codec,encoding,markup,damage) VALUES(?,?,?,?,?,?,?,?)', values)
        db.execute("INSERT OR REPLACE INTO subtitle_extended_media(path,modified,size,markup_version,indexed_at) VALUES(?,?,?,3,datetime('now'))", (str(path), item['modified'], item['size']))


processors = {'core': index_core, 'subtitles': index_subtitles}


def pending(job: str) -> list[dict]:
    from app.v82 import unified_pending_index_items
    return unified_pending_index_items(job)


def start(job: str):
    from app.v80 import check_index_queue
    return check_index_queue(job)


def status(job: str) -> dict:
    from app.v80 import queue_state
    return queue_state(job)


@app.get('/api/v54/setup/index/{job}/status')
def job_status(job: str) -> dict:
    return status(job)


@app.post('/api/v54/setup/index/{job}/check')
def check_job(job: str) -> dict:
    from app.v80 import check_index_queue
    check_index_queue(job)
    return status(job)


@app.post('/api/v54/setup/index/{job}/rebuild')
def rebuild_job(job: str) -> dict:
    from app.v80 import rebuild_index_queue
    return rebuild_index_queue(job)


@app.post('/api/v54/index/invalidate')
def invalidate_indexes(payload: MovieStreamIndexInvalidate) -> dict:
    from app.v80 import request_media_indexes
    added = request_media_indexes(payload.path, ['core', 'subtitles'], 'Media index refresh requested', defer_detection=True)
    return {'invalidated': True, 'queued': added}
