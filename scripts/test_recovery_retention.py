"""Real PostgreSQL policy regression using session-local temporary tables only."""
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from app import db_bootstrap
from app import postgres_store as store

with store.connection() as db, tempfile.TemporaryDirectory() as folder:
    for ddl in [
        'workflow_groups(group_id uuid, status text, definition jsonb, finished_at timestamptz, updated_at timestamptz)',
        'workflow_artifacts(artifact_id int,group_id uuid,artifact_path text)',
        'workflow_stages(group_id uuid,status text,finished_at timestamptz,updated_at timestamptz)',
        'workflow_locks(group_id uuid)',
        'workflow_luws(group_id uuid,status text)',
        'task_queue(group_id text,status text)',
        'index_task_queue(group_id text,status text)',
    ]:
        db.execute('CREATE TEMP TABLE '+ddl+' ON COMMIT DROP')
    @contextmanager
    def same_connection():
        yield db
    with patch.object(store, 'connection', same_connection), patch.object(store, '_stage_root', return_value=Path(folder)), patch.object(store, '_lock_workflow_mutation'):
        for n, (status, task, discard, active, expected) in enumerate([
            ('succeeded','succeeded',False,False,True),
            ('succeeded','failed',False,False,False),
            ('failed','failed',False,False,False),
            ('failed',None,False,False,False),
            ('failed',None,True,False,True),
            ('failed','failed',True,False,False),
            ('pending','pending',True,False,False),
            ('cancelled',None,True,True,False),
            ('pending',None,False,False,True),
        ],1):
            gid=uuid.uuid4()
            path=Path(folder)/f'{n}.mkv'; path.touch()
            db.execute("INSERT INTO workflow_groups(group_id,status,definition) VALUES(%s,%s,'{}')", (gid,status))
            db.execute('INSERT INTO workflow_artifacts VALUES(%s,%s,%s)', (n,gid,str(path)))
            if task: db.execute('INSERT INTO task_queue VALUES(%s,%s)', (gid.hex,task))
            if active: db.execute('INSERT INTO workflow_locks VALUES(%s)', (gid,))
            if discard: store.request_recovery_discard(db,str(gid))
            assert store._cleanup_retired_workflow_recovery(str(gid)) == int(expected), (status,task,discard,active)
            assert path.exists() != expected
        # An unlink failure must preserve the record for later retry.
        gid=uuid.uuid4(); path=Path(folder)/'locked.mkv'; path.touch()
        db.execute("INSERT INTO workflow_groups(group_id,status,definition) VALUES(%s,'succeeded','{}')", (gid,))
        db.execute('INSERT INTO workflow_artifacts VALUES(99,%s,%s)', (gid,str(path)))
        with patch.object(Path,'unlink',side_effect=PermissionError('fixture')):
            assert store._cleanup_retired_workflow_recovery(str(gid)) == 0
        assert db.execute('SELECT artifact_id FROM workflow_artifacts WHERE artifact_id=99').fetchone()
    db.rollback()
print('PASS: completed cleanup, failed retention, deleted failure cleanup, active protection, retryable unlink failure')
