"""Exercise actual log functions against an isolated database; no live jobs."""
import ast
import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
db.execute('PRAGMA foreign_keys=ON')
@contextmanager
def connect():
    with db: yield db
class HTTPException(Exception):
    def __init__(self,status,detail): self.status_code=status;super().__init__(detail)
scope={'connect':connect,'datetime':datetime,'timezone':timezone,'uuid':uuid,'re':re,'json':json,
       'logger':logging.getLogger('test'),'HTTPException':HTTPException,'Query':lambda **kw:kw.get('default')}
tree=ast.parse((ROOT/'app/scheduled_job_log.py').read_text())
nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.Assign))]
for node in nodes:
    if isinstance(node,ast.FunctionDef):node.decorator_list=[]
nodes=[n for n in nodes if not (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='logger' for t in n.targets))]
exec(compile(ast.Module(body=nodes,type_ignores=[]),'scheduled-log-test','exec'),scope)
scope['initialize_scheduled_logs']()
db.executescript('''CREATE TABLE task_queue(id INTEGER PRIMARY KEY,status TEXT,error TEXT,progress_message TEXT);
CREATE TABLE index_task_queue(id INTEGER PRIMARY KEY,status TEXT,error TEXT);
CREATE TABLE subtitle_cache_run(run_id TEXT,status TEXT,current_path TEXT,processed INTEGER,skipped INTEGER,failed INTEGER);
INSERT INTO task_queue VALUES(20,'pending',NULL,'Waiting');
INSERT INTO index_task_queue VALUES(30,'failed','File is missing');''')
identity=scope['begin']('core',task_id=20)
assert scope['begin']('core',task_id=20)==identity
assert db.execute('SELECT count(*) FROM scheduled_job_run').fetchone()[0]==1
scope['running'](identity)
scope['record'](identity,'Index refresh queued',path='/media/example.mkv',queue_kind='index',task_id=30)
scope['finish'](identity,'completed','1 media checked; 1 queued')
data=scope['latest_log']('core',limit=2)
assert data['run']['status']=='pending' and data['more']
earlier=scope['latest_log']('core',before=data['before'],run_id=identity,limit=2)
assert earlier['entries'] and not earlier['more']
assert data['entries'][1]['item_status']=='failed' and data['entries'][1]['error']=='File is missing'
db.execute("UPDATE task_queue SET status='failed',error='Dispatch failed' WHERE id=20")
assert scope['latest_log']('core')['run']['error']=='Dispatch failed'
assert '[credentials hidden]' in scope['clean']('postgresql://user:secret@server/db')
assert 'secret' not in scope['clean']('password=secret api_key=secret token=secret')
scope['ENTRY_LIMIT']=10
scope['record_many'](identity,[{'message':f'Item {n}'} for n in range(120)])
scope['finish'](identity,'completed','All 120 items processed')
assert db.execute('SELECT count(*) FROM scheduled_job_event WHERE run_id=?',(identity,)).fetchone()[0]==10
assert scope['latest_log']('core')['truncated']
for _ in range(15):
    run=scope['begin']('backup');scope['finish'](run,'completed','Created')
assert db.execute("SELECT count(*) FROM scheduled_job_run WHERE job='backup'").fetchone()[0]==10
assert not db.execute('SELECT 1 FROM scheduled_job_event e LEFT JOIN scheduled_job_run r ON e.run_id=r.run_id WHERE r.run_id IS NULL').fetchone()
run=scope['begin']('subtitle_cache');scope['running'](run)
db.execute("INSERT INTO subtitle_cache_run VALUES(?,'running','/media/current.mkv',12,30,1)",(run,))
assert '/media/current.mkv' in scope['latest_log']('subtitle_cache')['run']['current_message']
scope['initialize_scheduled_logs']()
assert db.execute('SELECT status FROM scheduled_job_run WHERE run_id=?',(run,)).fetchone()[0]=='interrupted'
print('PASS: log lifecycle, deduplication, live child failures, pagination, secret masking, bounded retention, cache progress and restart recovery')
