"""Real PostgreSQL tests in a rollback-only schema; no live jobs or media edits."""

import ast
import hashlib
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import postgres_store as store
from app.pg_compat import Connection

root = Path(__file__).resolve().parents[1]

def load(file, names, scope):
    nodes = [node for node in ast.parse((root / file).read_text()).body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    for node in nodes:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, 'exec'), scope)

with store.connection() as raw, tempfile.TemporaryDirectory() as folder:
    schema = 'vse_failed_fix_' + uuid.uuid4().hex
    raw.execute(f'CREATE SCHEMA {schema}')
    raw.execute(f'SET LOCAL search_path TO {schema}')
    adapter = Connection.__new__(Connection)
    adapter.raw, adapter.total_changes = raw, 0
    @contextmanager
    def isolated():
        yield adapter
    scope = dict(connect=isolated, hashlib=hashlib, dataclass=dataclass,
                 CACHE_FORMAT_VERSION=1, IMAGE_CACHE_FORMAT_VERSION=1)
    load('app/subtitle_cache.py', {'TextSubtitle', 'ensure_subtitle_cache_schema', 'upgrade_subtitle_cache_schema',
                                  'publish_media', 'get_valid_media', 'get_valid_track', 'publish_track'}, scope)
    scope['ensure_subtitle_cache_schema']()
    # Exercise upgrades from an old cache schema, retaining existing rows.
    raw.execute('ALTER TABLE subtitle_cache_track DROP COLUMN extraction_status')
    raw.execute('ALTER TABLE subtitle_cache_track DROP COLUMN source_encoding')
    raw.execute("INSERT INTO subtitle_cache_media(path,source_signature,format_version,expected_tracks,cached_tracks) VALUES('/old','original',1,1,1)")
    old = '1\n00:00:00,000 --> 00:00:01,000\nUnchanged\n'
    raw.execute("INSERT INTO subtitle_cache_track(path,source,type_index,codec,text_content,sha256) VALUES('/old','embedded',0,'subrip',%s,%s)",
                (old, hashlib.sha256(old.encode()).hexdigest()))
    scope['upgrade_subtitle_cache_schema']()
    scope['upgrade_subtitle_cache_schema']()
    assert scope['get_valid_media']('/old', 'original')[0].text == old
    empty = scope['TextSubtitle']('embedded', 1, '', 'subrip', '', 'empty', 'UTF-8')
    legacy = scope['TextSubtitle']('embedded', 0, '', 'subrip', old, 'ready', 'Windows-1252 (inferred)')
    scope['publish_media']('/new', 'new-source', {legacy.key, empty.key}, [legacy, empty])
    assert scope['get_valid_media']('/new', 'new-source') == [legacy, empty]
    scope['publish_track']('/new', 'new-source', {legacy.key, empty.key}, empty)
    assert scope['get_valid_track']('/new', 'new-source', empty.key).extraction_status == 'empty'

    # A safety refusal can precede begin_task_stage; it must not strand its
    # owner as pending or release a sibling's live lock. Test real UUID, JSONB,
    # transactions and row projections, rather than just matching SQL strings.
    with __import__('unittest.mock', fromlist=['patch']).patch.object(store, 'connection', lambda: __import__('contextlib').nullcontext(raw)):
        store.initialize_schema()
    failed_group = uuid.uuid4()
    raw.execute("INSERT INTO workflow_groups(group_id,kind,status) VALUES(%s,'media_edit','pending')", (failed_group,))
    for number, task_id, status in ((0, 700, 'pending'), (1, 701, 'running'), (2, 702, 'succeeded'), (3, 703, 'pending')):
        raw.execute("INSERT INTO workflow_stages(group_id,stage_number,task_type,status,payload) VALUES(%s,%s,'media_edit',%s,%s::jsonb)",
                    (failed_group, number, status, __import__('json').dumps({'task_id': task_id})))
    sibling_stage = raw.execute("SELECT stage_id FROM workflow_stages WHERE group_id=%s AND stage_number=1", (failed_group,)).fetchone()['stage_id']
    raw.execute("INSERT INTO workflow_locks(resource_key,group_id,stage_id,lease_until) VALUES('/same-media',%s,%s,now()+interval '1 hour')",
                (failed_group, sibling_stage))
    for task_id in (700, 701, 703):
        raw.execute("INSERT INTO workflow_luws(luw_id,group_id,resource_key,operation_type,mode,status,idempotency_key) "
                    "VALUES(%s,%s,'/same-media','media_edit','queued','planned',%s)",
                    (uuid.uuid4(), failed_group, f'task:{task_id}'))
    locked_luw = raw.execute("SELECT luw_id FROM workflow_luws WHERE idempotency_key='task:703'").fetchone()['luw_id']
    raw.execute("INSERT INTO workflow_luw_locks(resource_key,luw_id,lease_until) VALUES('/locked-media',%s,now()+interval '1 hour')", (locked_luw,))
    raw.execute("INSERT INTO workflow_artifacts(group_id,artifact_path,kind) VALUES(%s,'/recovery/original','media-original')", (failed_group,))
    store.fail_task_stage(str(failed_group), 'media_edit', '/same-media', 'Refused stale signature', 700, db=raw)
    assert raw.execute("SELECT status FROM workflow_stages WHERE group_id=%s AND stage_number=0", (failed_group,)).fetchone()['status'] == 'pending'
    raw.execute('SAVEPOINT safety_failure')
    store.fail_task_stage(str(failed_group), 'media_edit', '/same-media', 'Refused stale signature', 700, db=raw, allow_pending=True)
    assert raw.execute("SELECT status,error FROM workflow_stages WHERE group_id=%s AND stage_number=0", (failed_group,)).fetchone() == {'status': 'failed', 'error': 'Refused stale signature'}
    assert raw.execute("SELECT status FROM workflow_luws WHERE idempotency_key='task:700'").fetchone()['status'] == 'failed'
    assert raw.execute("SELECT status FROM workflow_groups WHERE group_id=%s", (failed_group,)).fetchone()['status'] == 'failed'
    raw.execute('ROLLBACK TO SAVEPOINT safety_failure')
    assert raw.execute("SELECT status FROM workflow_stages WHERE group_id=%s AND stage_number=0", (failed_group,)).fetchone()['status'] == 'pending'
    assert raw.execute("SELECT status FROM workflow_luws WHERE idempotency_key='task:700'").fetchone()['status'] == 'planned'
    store.fail_task_stage(str(failed_group), 'media_edit', '/same-media', 'Refused stale signature', 700, db=raw, allow_pending=True)
    assert raw.execute("SELECT stage_id FROM workflow_locks WHERE group_id=%s", (failed_group,)).fetchone()['stage_id'] == sibling_stage
    assert raw.execute("SELECT status FROM workflow_luws WHERE idempotency_key='task:701'").fetchone()['status'] == 'planned'
    assert raw.execute("SELECT count(*) AS n FROM workflow_artifacts WHERE group_id=%s", (failed_group,)).fetchone()['n'] == 1
    store.fail_task_stage(str(failed_group), 'media_edit', '/same-media', 'Do not reopen', 702, db=raw, allow_pending=True)
    assert raw.execute("SELECT status FROM workflow_stages WHERE group_id=%s AND stage_number=2", (failed_group,)).fetchone()['status'] == 'succeeded'
    store.fail_task_stage(str(failed_group), 'media_edit', '/locked-media', 'Refused stale signature', 703, db=raw, allow_pending=True)
    assert raw.execute("SELECT status FROM workflow_luws WHERE idempotency_key='task:703'").fetchone()['status'] == 'planned'
    store.fail_task_stage(str(failed_group), 'media_edit', '/same-media', 'Execution failed', 701, db=raw)
    assert raw.execute("SELECT status FROM workflow_stages WHERE group_id=%s AND stage_number=1", (failed_group,)).fetchone()['status'] == 'failed'
    assert not raw.execute("SELECT 1 FROM workflow_locks WHERE group_id=%s", (failed_group,)).fetchone()

    raw.execute("CREATE TABLE index_task_queue(id BIGINT PRIMARY KEY,job TEXT,path TEXT,group_id TEXT,status TEXT,error TEXT,finished_at TEXT,updated_at TEXT)")
    raw.execute("CREATE TABLE plex_media(path TEXT PRIMARY KEY)")
    raw.execute("CREATE TABLE media_stream_index_state(path TEXT PRIMARY KEY,modified_ns BIGINT,size BIGINT)")
    raw.execute("CREATE TABLE subtitle_extended_media(path TEXT PRIMARY KEY,modified BIGINT,size BIGINT,markup_version INTEGER)")
    with __import__('unittest.mock', fromlist=['patch']).patch.object(store, 'connection', lambda: __import__('contextlib').nullcontext(raw)):
        store.initialize_schema()
    test_paths = {}
    for index, name in enumerate(('current', 'stale', 'recovery', 'unfinished', 'no-new-success'), 1):
        file = Path(folder) / (name + '.mkv')
        file.write_bytes(b'fixture')
        test_paths[name] = str(file)
        group = uuid.uuid4()
        raw.execute("INSERT INTO plex_media(path) VALUES(%s)", (str(file),))
        stat = file.stat()
        raw.execute("INSERT INTO media_stream_index_state VALUES(%s,%s,%s)",
                    (str(file), stat.st_mtime_ns - (1 if name == 'stale' else 0), stat.st_size))
        raw.execute("INSERT INTO workflow_groups(group_id,kind,status) VALUES(%s,'index:core','failed')", (group,))
        raw.execute("INSERT INTO index_task_queue(id,job,path,group_id,status,error) VALUES(%s,'core',%s,%s,'failed','Old catalog miss')", (index, str(file), str(group)))
        raw.execute("INSERT INTO workflow_stages(group_id,stage_number,task_type,status,payload) VALUES(%s,0,'index:core','failed',%s::jsonb)",
                    (group, __import__('json').dumps({'task_id': index})))
        if name != 'no-new-success':
            raw.execute("INSERT INTO index_task_queue(id,job,path,status) VALUES(%s,'core',%s,'succeeded')", (100 + index, str(file)))
        if name == 'recovery':
            raw.execute("INSERT INTO workflow_artifacts(group_id,artifact_path,kind) VALUES(%s,'/fixture/original','media-original')", (group,))
        if name == 'unfinished':
            raw.execute("INSERT INTO workflow_stages(group_id,stage_number,task_type,status) VALUES(%s,1,'other','pending')", (group,))
    index_scope = dict(os=os, Path=Path, datetime=datetime, connection=isolated, MARKUP_VERSION=1, logger=logging.getLogger(__name__))
    load('app/v80.py', {'retire_superseded_index_failures', '_indexed_fingerprint_current'}, index_scope)
    assert index_scope['retire_superseded_index_failures']() == 2
    assert raw.execute("SELECT status FROM index_task_queue WHERE id=1").fetchone()['status'] == 'cancelled'
    for index in (2, 3, 5):
        assert raw.execute("SELECT status FROM index_task_queue WHERE id=%s", (index,)).fetchone()['status'] == 'failed'
    assert raw.execute("SELECT status FROM workflow_groups WHERE group_id=(SELECT group_id::uuid FROM index_task_queue WHERE id=4)").fetchone()['status'] == 'failed'
    assert index_scope['retire_superseded_index_failures']() == 0
    raw.rollback()  # Includes schema creation: nothing from this test persists.
print('PASS: cache upgrade and evidence; atomic pre-execution failure, exact stage/LUW ownership, recovery retention; obsolete index failures retire safely')
