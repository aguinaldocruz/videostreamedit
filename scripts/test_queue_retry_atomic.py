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
worker = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_queue')
failure_calls = [n for n in ast.walk(worker) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'fail_task_stage']
assert len(failure_calls) == 1
failure_call = failure_calls[0]
assert ast.unparse(next(k.value for k in failure_call.keywords if k.arg == 'db')) == 'db.raw'
assert next(k.value for k in failure_call.keywords if k.arg == 'allow_pending').value is True
atomic_failure = next(n for n in ast.walk(worker) if isinstance(n, ast.With)
                      and failure_call in list(ast.walk(n)))
assert 'connection()' in ast.unparse(atomic_failure.items[0].context_expr)
assert any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "SET status='failed'" in n.value for n in ast.walk(atomic_failure))
assert ast.unparse(atomic_failure.body[0]).startswith('fail_task_stage(')
print('PASS: atomic retry and early failure projection, exact transaction ownership, all-lane cooldown')
