"""Isolated retry contract regression; no application startup or media writes."""
import ast
from pathlib import Path

source = Path('app/v65.py').read_text()
tree = ast.parse(source)
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'retry_tasks_atomically')
events = []

class DB:
    raw = object()
    def execute(self, sql, params):
        events.append(('select' if sql.startswith('SELECT') else 'publish', params))
        return self
    def fetchall(self):
        return [{'id': 1, 'group_id': 'a'}, {'id': 2, 'group_id': 'b'}]

db = DB()
def reset(group, task, *, db):
    assert db is DB.raw
    events.append(('reset', task))

scope = {'_lock_workflow_mutation': lambda raw: events.append(('lock', raw)),
         'reset_task_stage_for_retry': reset, 'utc_now': lambda: 'now'}
exec(compile(ast.Module(body=[node], type_ignores=[]), '<retry>', 'exec'), scope)
assert scope['retry_tasks_atomically'](db, "status=?", ['failed']) == 2
assert [e[0] for e in events] == ['lock', 'select', 'reset', 'publish', 'reset', 'publish']
events.clear()
def fail(*args, **kwargs):
    raise RuntimeError('stage reset failed')
scope['reset_task_stage_for_retry'] = fail
try:
    scope['retry_tasks_atomically'](db, "status=?", ['failed'])
except RuntimeError:
    pass
else:
    raise AssertionError('Reset failure must abort caller transaction')
assert not any(e[0] == 'publish' for e in events)
assert source.count("AND {runnable}") == 3
assert source.count('changed = retry_tasks_atomically(') == 2
print('PASS: single/bulk shared transaction, lock order, reset-before-publication, failure propagation, all-lane cooldown')
