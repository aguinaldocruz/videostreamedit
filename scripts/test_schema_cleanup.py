"""Real PostgreSQL cleanup regression; all fixture DDL rolls back."""
import uuid
from psycopg import sql
from app.postgres_store import connection
from scripts.cleanup_obsolete_schema import cleanup

with connection() as db:
    schema='cleanup_test_'+uuid.uuid4().hex
    db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    db.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
    db.execute('''CREATE TABLE performance_metric(name text);
        CREATE TABLE feature_migrations(name text);
        INSERT INTO feature_migrations VALUES('unified_stream_index_v1');
        CREATE TABLE tv_stream_index_settings(key text,value text);
        INSERT INTO tv_stream_index_settings VALUES ('format_version','2');
        CREATE TABLE core_index_parity_runs(cursor_offset int,cursor_cursor_offset int DEFAULT 0);
        INSERT INTO core_index_parity_runs VALUES(17,0);
        CREATE TABLE media_video_title(path text PRIMARY KEY);
        CREATE INDEX media_video_title_path ON media_video_title(path);
        INSERT INTO media_video_title VALUES('keep.mkv');
        CREATE TABLE track_name_correction_history(stream_type text,track_language text,old_value text,use_count int);
        CREATE INDEX track_name_correction_lookup ON track_name_correction_history(stream_type,track_language,old_value,use_count DESC);
        CREATE INDEX track_name_correction_language_lookup ON track_name_correction_history(stream_type,track_language,old_value,use_count DESC);''')
    assert len(cleanup(db,False,schema))==6
    db.execute("INSERT INTO performance_metric VALUES('unexpected')")
    try:
        cleanup(db,True,schema)
        raise AssertionError('Unexpected data was not protected')
    except RuntimeError as e:
        assert 'unexpected data' in str(e)
    db.execute('DELETE FROM performance_metric')
    assert len(cleanup(db,True,schema))==6
    assert cleanup(db,True,schema)==[]
    assert db.execute('SELECT cursor_offset FROM core_index_parity_runs').fetchone()['cursor_offset']==17
    assert db.execute('SELECT path FROM media_video_title').fetchone()['path']=='keep.mkv'
    db.rollback()
print('PASS: exact obsolete objects, unexpected-data guard, preserved data, idempotency; rolled back fixtures')
