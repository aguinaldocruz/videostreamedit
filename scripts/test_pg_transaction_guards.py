"""Real PostgreSQL regression for transaction guards; temporary tables only."""
import psycopg
from app import db_bootstrap
from app.pg_compat import Connection

with Connection(db_bootstrap.effective_url()) as db:
    db.execute('CREATE TEMP TABLE vse_guard_test (n INTEGER UNIQUE) ON COMMIT DROP')
    db.execute('INSERT INTO vse_guard_test(n) VALUES(?)', (1,))
    db.execute('ALTER TABLE vse_guard_test ADD COLUMN label TEXT')
    try:
        db.execute('ALTER TABLE vse_guard_test ADD COLUMN label TEXT')
    except psycopg.errors.DuplicateColumn:
        pass
    else:
        raise AssertionError('Expected duplicate-column compatibility error')
    # Caught schema races must not poison the surrounding transaction.
    assert db.execute('SELECT n FROM vse_guard_test').fetchone()['n'] == 1
    # Non-schema failures still abort the transaction; they must not commit
    # an earlier partial write after a caller catches a constraint error.
    try:
        db.execute('INSERT INTO vse_guard_test(n) VALUES(?)', (1,))
    except psycopg.errors.UniqueViolation:
        pass
    else:
        raise AssertionError('Expected uniqueness violation')
    try:
        db.execute('SELECT 1')
    except psycopg.errors.InFailedSqlTransaction:
        pass
    else:
        raise AssertionError('Failed transaction unexpectedly usable')
    db.rollback()
    assert db.raw.execute("SELECT to_regclass('pg_temp.vse_guard_test') AS name").fetchone()['name'] is None
print('PASS: DDL race recovery, normal transaction failure, rollback of prior work')
