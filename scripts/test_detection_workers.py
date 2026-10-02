"""Run inside the application image; all database/service/media work mocked."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from app import v65, v79, v67
from app import detection_policy as policy

class DB:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=()): return self
    def fetchall(self): return []
    def executemany(self, sql, rows): self.rows = list(rows)

with patch.object(policy, 'is_final', return_value=True), patch.object(v65.subprocess, 'check_output', side_effect=AssertionError('Must not probe final media')):
    assert v65.process_audio_language_detection(0, {'path':'/not-needed'})['skipped']=='final_version'
    v79.inspect_portuguese_language('/not-needed')

with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'fixture.mkv';path.write_bytes(b'fixture')
    probe={'format':{'duration':600},'streams':[{'codec_type':'audio','tags':{'language':'en'}},{'codec_type':'audio','tags':{'language':'pt'}}]}
    db=DB()
    with patch.object(policy,'is_final',return_value=False), patch.object(v65,'connection',return_value=db), patch.object(v65.subprocess,'check_output',return_value=json.dumps(probe)), patch.object(v65,'extract_audio_sample') as extraction, patch.object(v65,'_language_service_request',return_value={'language_code':'en','confidence':.95}), patch.object(v65,'update_progress'), patch.object(v65,'mark_read_models_fresh'):
        result=v65.process_audio_language_detection(0,{'path':str(path),'stream_indices':[1]})
        assert result['streams']==1 and result['mismatches']==1
        assert len(extraction.call_args_list)==3
        assert all(call.args[1]==1 for call in extraction.call_args_list)
    with patch.object(policy,'is_final',side_effect=[False,True]), patch.object(v65,'connection',return_value=db), patch.object(v65.subprocess,'check_output',return_value=json.dumps(probe)), patch.object(v65,'extract_audio_sample') as extraction:
        assert v65.process_audio_language_detection(0,{'path':str(path),'stream_indices':[1]})['skipped']=='final_version_or_source_changed'
        extraction.assert_not_called()

text='\n\n'.join(f'{i}\n00:00:01,000 --> 00:00:03,000\nEu estou aqui para você. Não tenho medo. Estamos a chegar. Percebido.' for i in range(1,25))
result=policy.subtitle_assessment(text,'subrip','pt','',{'pt','en'})
assert result[3] != 'mismatch', result
assert policy.subtitle_assessment('','hdmv_pgs_subtitle','pt','',{'pt','en'})[3]=='unsupported'
print('PASS: final no-I/O, final during sampling, selected audio only, shared subtitle policy, unsupported image distinction')

# Transient startup/database errors must not permanently kill scheduling.
with patch.object(v67, 'connection', side_effect=[RuntimeError('startup lock'), DB()]) as connections, patch.object(v67.threading, 'Event') as event:
    event.return_value.wait.side_effect = [None, StopIteration]
    # DB is empty and must also support schedule iteration.
    with patch.object(DB, '__iter__', create=True, return_value=iter([])):
        try:
            v67.run_scheduler()
        except StopIteration:
            pass
    assert connections.call_count == 2

class RetirementDB(DB):
    raw = None
    def __init__(self, groups): self.groups, self.calls = groups, []
    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self.result = [{'group_id': group} for group in self.groups] if 'RETURNING group_id' in sql else []
        return self
    def fetchall(self): return self.result

with patch('app.postgres_store._lock_workflow_mutation'):
    db = RetirementDB([])
    policy.retire_final_detection(db)
    assert not any('UPDATE workflow_' in sql for sql, _ in db.calls)
    db = RetirementDB(['00000000-0000-0000-0000-000000000001'])
    policy.retire_final_detection(db)
    updates = [(sql, args) for sql, args in db.calls if 'UPDATE workflow_' in sql]
    assert len(updates) == 2
    assert all('group_id::text IN (?)' in sql and args == db.groups for sql, args in updates)
print('PASS: scheduler survives transient errors; retirement touches only affected workflow groups')

# Scheduled index checks must use the durable queue status, not the removed
# in-memory index worker state.
class ScheduleDB(DB):
    def __init__(self, running):
        self.running = running
        self.updated = False
    def execute(self, sql, params=()):
        if sql.startswith('SELECT job,frequency,time_of_day,last_run'):
            self.rows = [{'job': 'core', 'frequency': 'daily', 'time_of_day': '03:00', 'last_run': None}]
        elif sql.startswith('UPDATE index_job_schedule'):
            self.updated = True
            self.rows = []
        return self
    def __iter__(self): return iter(self.rows)

for running in (0, 1):
    db = ScheduleDB(running)
    with patch.object(v67, 'connection', return_value=db), patch.object(v67, 'schedule_due', return_value=True), patch.object(v67.index_jobs, 'status', return_value={'running': running, 'total': 0}) as status, patch.object(v67.index_jobs, 'start') as start, patch.object(v67.threading, 'Event') as event:
        event.return_value.wait.side_effect = StopIteration
        try:
            v67.run_scheduler()
        except StopIteration:
            pass
    status.assert_called_with('core')
    assert start.call_count == (0 if running else 1)
    assert db.updated == (running == 0)
print('PASS: scheduled index checks use durable queue status and skip active jobs')
