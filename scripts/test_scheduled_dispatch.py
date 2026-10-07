"""Run-now dispatch uses durable queues, saved settings and narrow families."""
import ast
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
db.executescript('''CREATE TABLE deferred_language_detection(path TEXT,detection_json TEXT,requested_at INTEGER);
CREATE TABLE index_task_queue(id INTEGER,job TEXT,path TEXT,status TEXT);
INSERT INTO deferred_language_detection VALUES('/sub','{"subtitle_indices":[0]}',1),('/audio','{"audio_indices":[1]}',2);
INSERT INTO index_task_queue VALUES(44,'subtitles','/sub','pending');''')
@contextmanager
def connection():
    with db: yield db
calls=[];log=[]
def enqueue(kind,payload,label,**options):
    calls.append((kind,payload,label,options));return {'id':20,'status':'pending'}
def flush(path,family):
    calls.append(('flush',path,family))
    return {'queued':True,'voice_task_id':45 if family=='audio' else None}
def module(name,**values):
    value=ModuleType(name);value.__dict__.update(values);sys.modules[name]=value;return value
package=module('app');package.__path__=[]
module('app.v65',enqueue=enqueue,update_progress=lambda *v:None)
module('app.v80',flush_deferred_language_detection=flush)
module('app.subtitle_cache_schedule',start_run=lambda **kw: calls.append(('cache',kw)) or {'status':'pending'})
module('app.v68',queue_logged_plex_check=lambda source:calls.append(('plex',source)) or {'id':21})
module('app.v99_backup',_start=lambda *a,**kw:calls.append(('backup',a,kw)) or {'accepted':True})
module('app.preflight_dispatcher',_cleanup_if_due=lambda **kw:calls.append(('cleanup',kw)) or {'deleted':3})
class HTTPException(Exception):pass
tree=ast.parse((ROOT/'app/v67.py').read_text())
nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'queue_scheduled_job','dispatch_scheduled_job'}]
for node in nodes:node.decorator_list=[]
job_log=SimpleNamespace(JOBS={job:job for job in ('core','subtitles','subtitle_detection','voice_detection','preflight_cleanup')},
    begin=lambda *a,**kw:'run-20',running=lambda *a:None,record=lambda *a,**kw:log.append((a,kw)),finish=lambda *a:log.append(a))
scope={'connection':connection,'HTTPException':HTTPException,'index_jobs':SimpleNamespace(JOBS=('core','subtitles')),'job_log':job_log}
exec(compile(ast.Module(body=nodes,type_ignores=[]),'scheduled-dispatch-test','exec'),scope)
for job in ('core','subtitles','subtitle_detection','voice_detection','preflight_cleanup','subtitle_cache','plex_sync','backup'):
    scope['queue_scheduled_job'](job)
assert len(calls)==8 and all(c[3]['deduplicate'] for c in calls[:5])
assert calls[5]==('cache',{'manual':True})
assert calls[6]==('plex','manual') and calls[7][2]=={'source':'manual'}
scope['queue_scheduled_job']('core','schedule')
assert calls[-1][1]=={'job':'core','_schedule_source':'schedule'}
calls.clear()
result=scope['dispatch_scheduled_job'](20,{'job':'subtitle_detection'})
assert result['queued']==1 and calls==[('flush','/sub','subtitle')]
assert log[-2][1]['queue_kind']=='index' and log[-2][1]['task_id']==44
calls.clear()
result=scope['dispatch_scheduled_job'](20,{'job':'voice_detection'})
assert result['queued']==1 and calls==[('flush','/audio','audio')]
result=scope['dispatch_scheduled_job'](20,{'job':'preflight_cleanup'})
assert result['deleted']==3 and calls[-1]==('cleanup',{'force':True,'log_id':'run-20'})
worker=(ROOT/'app/v65.py').read_text()
assert "task_type IN ('index_check_prepare','index_rebuild_prepare','scheduled_task_dispatch')" in worker
assert "task_type NOT IN ('media_reindex','index_check_prepare','index_rebuild_prepare','scheduled_task_dispatch'" in worker
print('PASS: eight run-now targets, saved settings, durable deduplicated submission, scheduled source, targeted detection families, linked child logs and light-lane dispatch')
