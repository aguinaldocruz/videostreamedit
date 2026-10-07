"""Isolated PostgreSQL regressions; never touch live queue rows or media."""
import ast
import json
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from app import postgres_store as store
from app import job_safety as safety
from app.job_outcomes import finalize_missing_workflow, MISSING_PREFIX
from app.pg_compat import Connection

root = Path(__file__).resolve().parents[1]

with store.connection() as db:
    schema = 'vse_terminal_test_' + uuid.uuid4().hex
    db.execute(f'CREATE SCHEMA {schema}')
    db.execute(f'SET LOCAL search_path TO {schema}')
    with patch.object(store, 'connection', lambda: nullcontext(db)):
        store.initialize_schema()
    db.execute("""CREATE TABLE task_queue(id BIGINT PRIMARY KEY,task_type TEXT,group_id TEXT,
        status TEXT,payload_json TEXT,error TEXT,progress_message TEXT,progress_current INTEGER,progress_total INTEGER,started_at TEXT,finished_at TEXT,updated_at TEXT);
        CREATE TABLE index_task_queue(id BIGINT PRIMARY KEY,job TEXT,path TEXT,group_id TEXT,
        status TEXT,error TEXT,reason TEXT,attempts INTEGER DEFAULT 0,created_at TEXT,started_at TEXT,finished_at TEXT,updated_at TEXT);
        CREATE TABLE subtitle_cache_failure(path TEXT PRIMARY KEY,error TEXT);
        CREATE TABLE subtitle_cache_media(path TEXT PRIMARY KEY,format_version INTEGER,expected_tracks INTEGER,cached_tracks INTEGER);
        CREATE TABLE index_queue_settings(job TEXT,paused INTEGER,stop_requested INTEGER);
        CREATE TABLE media_stream_index_state(path TEXT);
        CREATE TABLE subtitle_extended_media(path TEXT);
        INSERT INTO index_queue_settings VALUES('subtitles',0,0)""")
    group = uuid.uuid4()
    other_group = uuid.uuid4()
    with patch.object(store, 'connection', lambda: nullcontext(db)):
        for _ in range(3):
            store.register_task_stage(str(group), 'media_edit', '/missing', {}, 1)
        assert db.execute('SELECT count(*) n FROM workflow_stages WHERE group_id=%s', (group,)).fetchone()['n'] == 1
        db.execute("UPDATE workflow_stages SET status='succeeded' WHERE group_id=%s", (group,))
        store.register_task_stage(str(group), 'media_edit', '/missing', {}, 1)
        assert db.execute('SELECT count(*) n FROM workflow_stages WHERE group_id=%s', (group,)).fetchone()['n'] == 1
        store.register_task_stage(str(other_group), 'media_edit', '/other', {}, 1)
    db.execute("INSERT INTO task_queue(id,task_type,group_id,status,payload_json) VALUES(1,'media_edit',%s,'succeeded','{}')", (str(group),))
    # Reproduce a historical registration race: completed owner + phantom stage.
    db.execute("INSERT INTO workflow_stages(group_id,stage_number,task_type,status,payload) VALUES(%s,1,'media_edit','pending','{\"task_id\":1}')", (group,))
    with patch.object(safety, 'connection', lambda: nullcontext(db)):
        assert safety.reconcile_terminal_owners() == 1
        assert safety.reconcile_terminal_owners() == 0
    assert db.execute('SELECT status FROM workflow_stages WHERE group_id=%s', (other_group,)).fetchone()['status'] == 'pending'
    # Fail only this media's unstarted dependents, not a sibling media.
    db.execute("INSERT INTO task_queue(id,task_type,group_id,status,payload_json) VALUES(2,'media_edit',%s,'pending','{\"path\":\"/missing\"}'),(3,'media_edit',%s,'pending','{\"path\":\"/sibling\"}')", (str(group),str(group)))
    db.execute("INSERT INTO task_queue(id,task_type,group_id,status,payload_json) VALUES(4,'media_edit',%s,'pending','{\"edit\":{\"path\":\"/missing\"}}')", (str(group),))
    with patch.object(store, 'connection', lambda: nullcontext(db)):
        store.register_task_stage(str(group), 'media_edit', '/missing', {}, 2)
        store.register_task_stage(str(group), 'media_edit', '/sibling', {}, 3)
        store.register_task_stage(str(group), 'media_edit', '/missing', {}, 4)
    db.execute("INSERT INTO workflow_artifacts(group_id,artifact_path,kind) VALUES(%s,'/retained-original','media-original')", (group,))
    finalize_missing_workflow(db, str(group), '/missing', MISSING_PREFIX+' /missing')
    assert db.execute('SELECT status FROM task_queue WHERE id=2').fetchone()['status'] == 'failed'
    assert db.execute('SELECT status FROM task_queue WHERE id=3').fetchone()['status'] == 'pending'
    assert db.execute('SELECT status FROM task_queue WHERE id=4').fetchone()['status'] == 'failed'
    assert db.execute('SELECT count(*) n FROM workflow_artifacts').fetchone()['n'] == 1
    retry_node = next(n for n in ast.parse((root/'app/v65.py').read_text()).body
                      if isinstance(n,ast.FunctionDef) and n.name=='retry_tasks_atomically')
    retry_adapter = Connection.__new__(Connection)
    retry_adapter.raw, retry_adapter.total_changes = db, 0
    retry_scope = {'_lock_workflow_mutation':store._lock_workflow_mutation,
                   'reset_task_stage_for_retry':store.reset_task_stage_for_retry,'utc_now':lambda:'now'}
    exec(compile(ast.Module(body=[retry_node],type_ignores=[]),'retry','exec'),retry_scope)
    assert retry_scope['retry_tasks_atomically'](retry_adapter,"id=? AND status='failed'",[2]) == 0
    assert db.execute('SELECT status FROM task_queue WHERE id=2').fetchone()['status'] == 'failed'
    # Orphan plan retirement must preserve real mutation journals and locks.
    for number, kind in enumerate(('operation_plan','file_replace'), 1):
        luw = uuid.uuid4()
        db.execute("INSERT INTO workflow_luws(luw_id,group_id,resource_key,operation_type,mode,status,idempotency_key,updated_at) VALUES(%s,NULL,%s,'media_edit','queued','waiting',%s,now()-interval '1 hour')", (luw,f'/orphan{number}',f'orphan{number}'))
        db.execute('INSERT INTO workflow_luw_journal(luw_id,kind) VALUES(%s,%s)', (luw,kind))
    with patch.object(safety, 'connection', lambda: nullcontext(db)):
        safety.reconcile_orphans()
    assert db.execute("SELECT status FROM workflow_luws WHERE idempotency_key='orphan1'").fetchone()['status'] == 'cancelled'
    assert db.execute("SELECT status FROM workflow_luws WHERE idempotency_key='orphan2'").fetchone()['status'] == 'waiting'
    # Queue status uses real SQL joins: cache failures are blocked, not ETA work.
    adapter = Connection.__new__(Connection)
    adapter.raw, adapter.total_changes = db, 0
    source = ast.parse((root/'app/v80.py').read_text())
    fn = next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='queue_state')
    from datetime import datetime
    sql_node = next(n for n in source.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SUBTITLE_CACHE_READY_SQL' for t in n.targets))
    scope = {'connection':lambda:nullcontext(adapter),'datetime':datetime}
    exec(compile(ast.Module(body=[sql_node,fn],type_ignores=[]),'queue-state','exec'),scope)
    db.execute("INSERT INTO index_task_queue(id,job,path,status) VALUES(100,'subtitles','/damaged','pending')")
    db.execute("INSERT INTO subtitle_cache_failure VALUES('/damaged','Replacement characters need review')")
    state = scope['queue_state']('subtitles')
    assert state['blocked_cache']==1 and state['waiting_cache']==1 and state['eta_seconds'] is None
    assert state['items'][0]['display_status']=='blocked_cache'
    assert 'Replacement characters' in state['items'][0]['error']
    db.rollback()  # rolls back schema and every test record
print('PASS: idempotent registration, exact terminal owners, isolated missing-media failure, recovery retention, blocked cache status')
