"""Lossless learning upgrades in isolated SQLite and PostgreSQL schemas."""
import sqlite3
import uuid
from psycopg import sql
from app import db_bootstrap
from app.pg_compat import Connection
from app.learning_schema import ensure_learning_schema


def check(db):
    db.execute('''CREATE TABLE track_name_correction_history (
        stream_type TEXT,old_value TEXT,new_value TEXT,use_count INTEGER,
        last_used TEXT,enabled INTEGER,PRIMARY KEY(stream_type,old_value,new_value))''')
    db.execute("INSERT INTO track_name_correction_history VALUES('audio','old','new',12,'2026-09-27T12:00:00Z',0)")
    ensure_learning_schema(db)
    ensure_learning_schema(db)
    row=db.execute('SELECT * FROM track_name_correction_history').fetchone()
    assert row['track_language']=='' and row['use_count']==12 and row['enabled']==0
    db.execute("INSERT INTO track_name_correction_history(stream_type,track_language,old_value,new_value) VALUES('audio','pt','old','new')")
    assert db.execute('SELECT count(*) FROM track_name_correction_history').fetchone()[0]==2


with sqlite3.connect(':memory:') as db:
    db.row_factory=sqlite3.Row
    check(db)
with Connection(db_bootstrap.effective_url()) as db:
    name='test_learning_'+uuid.uuid4().hex
    db.raw.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
    db.raw.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(name)))
    try:
        check(db)
    finally:
        db.rollback()
print('PASS: learned counts, declined/disabled values and mappings preserved; region-aware uniqueness; idempotency; both backends')
