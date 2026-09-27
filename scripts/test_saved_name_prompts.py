"""Exercise production prompt decisions against isolated SQLite, never live data."""
import ast
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS

source = Path(__file__).resolve().parents[1] / 'app/v8.py'
tree = ast.parse(source.read_text())
names = {'record_value_uses', 'decide_saved_value', 'declined_track_names', 'clear_declined_track_names'}
functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
for node in functions:
    node.decorator_list = []
db = sqlite3.connect(':memory:')
db.row_factory = sqlite3.Row
db.executescript('CREATE TABLE reusable_stream_values(field TEXT,value TEXT,use_count INTEGER DEFAULT 0,saved INTEGER DEFAULT 0,prompted INTEGER DEFAULT 0,PRIMARY KEY(field,value));')

@contextmanager
def connection():
    with db:
        yield db

available = {'title_audio': ['Already offered']}
scope = dict(connection=connection, saved_values=lambda: available,
             ValueUsesRequest=object, SavedValueDecision=object, logger=logging.getLogger(__name__))
exec(compile(ast.Module(body=functions,type_ignores=[]),str(source),'exec'),scope)

def use(value, copies=1):
    return scope['record_value_uses'](NS(values=[NS(field='title_audio',value=value)]*copies))['prompts']

def decide(value, save):
    scope['decide_saved_value'](NS(field='title_audio',value=value,save=save))

assert not use('New', 20), 'Bulk duplicates must count as a single use'
assert use('New') == [{'field':'title_audio','value':'New'}]
assert not use('Unrelated'), 'Do not prompt for older unrelated names'
decide('New',False)
assert not use('New')
assert scope['declined_track_names']()['values'][0]['value']=='New'
decide('New',True)
assert not scope['declined_track_names']()['values']
assert not use('New')
assert not use('Already offered')
assert not use('Already offered')
decide('Declined',False)
assert scope['clear_declined_track_names']()['removed']==1
assert not use('Declined')
assert use('Declined')
assert not use('New'), 'Clearing denials must preserve saved names'
print('PASS: two uses, bulk deduplication, scoped prompts, saved/learned exclusion, persistent refusal, manual save and clear/reset')
