"""Real PostgreSQL adapter integration, confined to a disposable test schema.

Run inside the application image with /config mounted read-only. Imports no
worker modules and executes no catalog or media jobs. Always drops its own schema.
"""
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db_bootstrap  # Load encrypted connection; never print it.
from app.pg_compat import connect
from app import scheduled_job_log as logs
from psycopg import sql

schema='vse_schedule_test_'+uuid.uuid4().hex
with connect() as db:
    db.raw.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
@contextmanager
def isolated():
    with connect() as db:
        db.raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        yield db
try:
    logs.connect=isolated
    logs.initialize_scheduled_logs()
    with isolated() as db:
        db.executescript('''CREATE TABLE task_queue(id BIGINT PRIMARY KEY,status TEXT,error TEXT,progress_message TEXT);
            CREATE TABLE index_task_queue(id BIGINT PRIMARY KEY,status TEXT,error TEXT);
            INSERT INTO task_queue VALUES(5,'running',NULL,'Checking media');
            INSERT INTO index_task_queue VALUES(6,'failed','Media moved');''')
    run=logs.begin('core',task_id=5)
    logs.running(run)
    logs.record(run,'Index refresh queued',path='/fixture/file.mkv',queue_kind='index',task_id=6)
    logs.finish(run,'completed','1 queued')
    data=logs.latest_log('core',before=None,run_id=None,limit=100)
    assert data['run']['status']=='running'
    assert next(e for e in data['entries'] if e['task_id']==6)['error']=='Media moved'
    # Force a log insertion error inside a caller transaction. Its unrelated
    # successful update must still commit instead of being rolled back.
    with isolated() as db:
        db.execute("UPDATE task_queue SET status='succeeded' WHERE id=5")
        logs.record('nonexistent-run','Deliberate FK failure',db=db)
    assert logs.latest_log('core',before=None,run_id=None,limit=100)['run']['status']=='succeeded'
    logs.ENTRY_LIMIT=5
    logs.record_many(run,[{'message':'Processed fixture '+str(i)} for i in range(120)])
    logs.finish(run,'completed','120 fixture items')
    with isolated() as db:
        assert db.execute('SELECT count(*) FROM scheduled_job_event WHERE run_id=?',(run,)).fetchone()[0]==5
    print('PASS: actual PostgreSQL DDL, inserts, savepoint isolation, live child errors, authoritative queue state and bounded logs; no production tables or media touched')
finally:
    with connect() as db:
        db.raw.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
