"""Real queue deduplication in a rollback-only schema; no live media/jobs."""
import ast
import copy
import json
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

from app import postgres_store as store, v65 as tasks
from app.pg_compat import Connection

with store.connection() as raw, tempfile.TemporaryDirectory(prefix='vse-edit-dedup-') as folder:
    schema = 'vse_edit_dedup_' + uuid.uuid4().hex
    raw.execute(f'CREATE SCHEMA {schema}')
    raw.execute(f'SET LOCAL search_path TO {schema}')
    adapter = Connection.__new__(Connection)
    adapter.raw, adapter.total_changes = raw, 0

    @contextmanager
    def connection():
        yield adapter

    with patch.object(store, 'connection', lambda: nullcontext(raw)), \
         patch.object(tasks, 'connection', connection), patch.object(tasks, 'wake_queue'):
        store.initialize_schema()
        init = next(n for n in ast.parse(Path('app/v65.py').read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'initialize_task_queue')
        ddl = next(n.args[0].value for n in ast.walk(init) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and n.func.attr == 'executescript')
        adapter.executescript(ddl)
        media = Path(folder)/'fixture.mkv'
        media.write_bytes(b'Disposable source; never run any mutation')
        signature = {'path': str(media), 'digest': 'snapshot-1'}
        payload = {'edit': {'path': str(media), 'tracks': [{'codec_type': 'subtitle', 'type_index': 0,
                                                         'title': 'A new title'}]},
                   '_media_signature': signature}
        first = tasks.enqueue('media_edit', payload, 'First click')
        second = tasks.enqueue('media_edit', payload, 'Repeated click')
        assert second['id'] == first['id']
        assert second['deduplicated'] is True
        raw.execute("UPDATE task_queue SET status='running' WHERE id=%s", (first['id'],))
        assert tasks.enqueue('media_edit', payload, 'Retry during processing')['id'] == first['id']
        assert raw.execute('SELECT count(*) n FROM task_queue').fetchone()['n'] == 1
        assert raw.execute('SELECT count(*) n FROM workflow_stages').fetchone()['n'] == 1
        distinct = copy.deepcopy(payload)
        distinct['edit']['tracks'][0]['title'] = 'Another edit'
        assert tasks.enqueue('media_edit', distinct)['id'] != first['id']
        newer = copy.deepcopy(payload)
        newer['_media_signature']['digest'] = 'snapshot-2'
        assert tasks.enqueue('media_edit', newer)['id'] != first['id']
        # The same proposal is allowed again after completion against a fresh
        # source; idempotency never hides genuinely new user actions.
        raw.execute("UPDATE task_queue SET status='succeeded' WHERE id=%s", (first['id'],))
        assert tasks.enqueue('media_edit', payload)['id'] != first['id']
        assert media.read_bytes() == b'Disposable source; never run any mutation'
    raw.rollback()
print('PASS: repeated pending/running edit returns one job/stage; distinct proposals and source snapshots remain independent; production untouched')
