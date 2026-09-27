"""Safety regressions; mocks only, no production database/media writes."""
import tempfile
from pathlib import Path
from unittest.mock import patch
from app.pg_compat import Connection
from app import postgres_store as store


class Result:
    def __init__(self, rows=(), count=0):
        self.rows = list(rows)
        self.rowcount = count
    def fetchone(self): return self.rows.pop(0) if self.rows else None
    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class Raw:
    def __init__(self, conflict=False): self.sql, self.conflict = [], conflict
    def execute(self, sql, params=()):
        self.sql.append(sql)
        if 'information_schema.columns' in sql: return Result([{'exists': 1}])
        if sql.startswith('INSERT'):
            assert 'RETURNING id' in sql
            return Result([] if self.conflict else [{'id': 41}], 0 if self.conflict else 1)
        return Result()


for conflict in [False, True]:
    db = object.__new__(Connection)
    db.raw, db.total_changes = Raw(conflict), 0
    result = db.execute('INSERT INTO task_queue(label) VALUES(?)', ['fixture'])
    assert result.lastrowid == (None if conflict else 41)
    assert not any('last_value' in sql or 'pg_get_serial_sequence' in sql for sql in db.raw.sql)
db = object.__new__(Connection)
db.raw, db.total_changes = Raw(), 0
assert db.execute('INSERT INTO task_queue(label) VALUES(?) RETURNING id', ['fixture']).fetchone()['id'] == 41


class ArtifactDB:
    def __init__(self, path): self.path, self.deleted = path, []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=()):
        if 'SELECT status' in sql: return Result([{'status': 'succeeded'}])
        if 'SELECT artifact_id' in sql: return Result([{'artifact_id': 7, 'artifact_path': str(self.path)}])
        if 'DELETE' in sql: self.deleted.append(params)
        return Result()


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    source = root / 'original.mkv'
    source.touch()
    db = ArtifactDB(source)
    with patch.object(store, 'DATABASE_URL', 'fixture'), patch.object(store, '_stage_root', return_value=root), patch.object(store, 'connection', return_value=db):
        with patch.object(Path, 'unlink', side_effect=PermissionError('fixture')):
            assert store.cleanup_succeeded_workflow_group_artifacts('00000000-0000-0000-0000-000000000001') == 0
        assert not db.deleted and source.exists()
        assert store.cleanup_succeeded_workflow_group_artifacts('00000000-0000-0000-0000-000000000001') == 1
        assert len(db.deleted) == 1 and not source.exists()
assert 'split_saved_track_names' not in Path('app/v35.py').read_text()
calls = []
class CancellationDB:
    def execute(self, sql, params): calls.append((sql, params))
store.cancel_pending_task_stage(CancellationDB(), '00000000-0000-0000-0000-000000000001', 17, 'media_edit')
assert len(calls) == 2
assert "payload->>'task_id'=%s AND task_type=%s AND status='pending'" in calls[0][0]
assert calls[0][1][1:] == ('17', 'media_edit')
assert not any('DELETE' in sql for sql, _ in calls)
print('PASS: statement-owned IDs, conflicts, explicit RETURNING, cleanup failure retention, migration removed')
