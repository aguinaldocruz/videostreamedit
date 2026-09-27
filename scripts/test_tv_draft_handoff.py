"""Isolated draft submission tests; no live DB, media or jobs."""
import ast
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS

db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
db.executescript('''CREATE TABLE tv_edit_sessions(session_id TEXT,show_id TEXT,show_title TEXT,status TEXT,updated_at TEXT,committed_at TEXT,error TEXT,commit_task_id INTEGER);
CREATE TABLE tv_edit_operations(session_id TEXT,path TEXT,operation_json TEXT,input_signature TEXT,sequence INTEGER);
CREATE TABLE task_queue(id INTEGER,status TEXT,task_type TEXT,payload_json TEXT,progress_current INTEGER,progress_total INTEGER,progress_message TEXT,error TEXT);
INSERT INTO tv_edit_sessions VALUES('draft','show','Fixture','open','','',NULL,NULL);
''')
for i in range(500):
    db.execute('INSERT INTO tv_edit_operations VALUES(?,?,?,?,?)',('draft',f'episode{i}',json.dumps({'remove':True}),json.dumps({'size':100}),i))
db.commit()
class Conn:
    def execute(self,sql,params=()): return db.execute(sql.replace(' FOR UPDATE',''),params)
@contextmanager
def connection():
    with db: yield Conn()
calls=[]
def enqueue(kind,payload,label,**kwargs):
    calls.append(payload)
    assert 'items' not in payload
    assert payload['journal_reference'] is True
    db.execute("INSERT INTO task_queue VALUES(1,'pending',?,?,0,0,'Waiting',NULL)",(kind,json.dumps(payload)))
    return {'id':1}
class HTTPException(Exception):
    def __init__(self,status,detail): self.status_code=status;super().__init__(detail)
scope=dict(connection=connection,tasks=NS(enqueue=enqueue),_tv_edit_now=lambda:'now',HTTPException=HTTPException,json=json)
tree=ast.parse((Path(__file__).resolve().parents[1]/'app/v79.py').read_text())
nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'save_tv_edit_session','tv_edit_save_status','load_tv_commit_journal'}]
for n in nodes:n.decorator_list=[]
exec(compile(ast.Module(body=nodes,type_ignores=[]),'handoff','exec'),scope)
result=scope['save_tv_edit_session']('draft')
assert result['task_id']==1 and result['operation_count']==500
assert len(json.dumps(calls[0]))<200
assert scope['save_tv_edit_session']('draft')['task_id']==1
assert len(calls)==1, 'Repeated Save duplicated the task'
assert len(scope['load_tv_commit_journal']('draft'))==500
assert db.execute('SELECT count(*) FROM tv_edit_operations').fetchone()[0]==500
db.execute("INSERT INTO tv_edit_sessions VALUES('empty','show2','Empty','open','','',NULL,NULL)");db.commit()
assert scope['save_tv_edit_session']('empty')['status']=='committed'
assert db.execute("SELECT status FROM tv_edit_sessions WHERE session_id='empty'").fetchone()[0]=='committed'
print('PASS: 500-episode draft uses a small queue reference, journal retained, idempotent Save, worker consolidation, empty draft completion')
