"""Durable autofix CRUD/CAS using real PostgreSQL in a disposable schema."""
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from psycopg import sql
from app import subtitle_autofix as autofix
from app.pg_compat import connect

schema = 'vse_autofix_test_' + uuid.uuid4().hex
with connect() as db:
    db.raw.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))


@contextmanager
def isolated():
    with connect() as db:
        db.raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        yield db


try:
    with isolated() as db:
        db.execute('CREATE TABLE application_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute("INSERT INTO application_settings VALUES('subtitle_color','#FFFF00')")
    with patch.object(autofix, 'connection', isolated):
        request = autofix.RuleFields(name='Portuguese 100% literal', languages=['por|BR', 'pt_PT'],
                                    replacements=[{'from': 'Ã£', 'to': 'ã'}, {'from': '100%', 'to': 'cem por cento'}])
        first = autofix.create_rule(request)
        assert autofix.list_rules()['rules'] == [first]
        assert first['languages'] == ['pt-BR', 'pt-PT']
        body = {key: first[key] for key in ('name', 'description', 'languages', 'enabled', 'replacements', 'revision')}
        body['description'] = 'Verified before media integration'
        newer = autofix.update_rule(first['id'], autofix.RuleUpdate.model_validate(body))
        assert newer['revision'] == 2
        # Both editors read the same revision. Only one can commit even if
        # they pass revision validation before either UPDATE reaches the DB.
        barrier = threading.Barrier(2)
        original_stored = autofix._stored

        def simultaneous_read(db, rule_id):
            saved = original_stored(db, rule_id)
            barrier.wait(timeout=10)
            return saved

        outcomes = []

        def write(name):
            update = dict(body, name=name, revision=2)
            try:
                outcomes.append(autofix.update_rule(first['id'], autofix.RuleUpdate.model_validate(update)))
            except HTTPException as exc:
                outcomes.append(exc.status_code)

        with patch.object(autofix, '_stored', simultaneous_read):
            threads = [threading.Thread(target=write, args=(name,)) for name in ('Editor A', 'Editor B')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
                assert not thread.is_alive(), 'Concurrent edit did not finish'
        assert len(outcomes) == 2 and 409 in outcomes, outcomes
        saved = autofix.list_rules()['rules'][0]
        assert saved['revision'] == 3 and saved['name'] in ('Editor A', 'Editor B')
        assert saved['replacements'] == first['replacements']
        autofix.delete_rule(saved['id'], revision=saved['revision'])
        assert not autofix.list_rules()['rules']
    with isolated() as db:
        assert db.execute("SELECT value FROM application_settings WHERE key='subtitle_color'").fetchone()[0] == '#FFFF00'
    print('PASS: real PostgreSQL Unicode/percent storage, filtered library read, atomic concurrent editing, deletion; production rules and media untouched')
finally:
    with connect() as db:
        db.raw.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
