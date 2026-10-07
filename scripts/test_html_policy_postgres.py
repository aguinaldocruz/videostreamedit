"""Policy refresh in a disposable PostgreSQL schema; no catalog processing."""
import ast
import re
import sys
import types
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db_bootstrap
from app import pg_compat
from app import subtitle_html as policy
from psycopg import sql

original_connect=pg_compat.connect
schema='vse_html_test_'+uuid.uuid4().hex
with original_connect() as db:
    db.raw.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
@contextmanager
def isolated():
    with original_connect() as db:
        db.raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        yield db
try:
    pg_compat.connect=isolated
    # Load only the pure classifier, not the application worker chain.
    scope={'has_removable_html':policy.has_removable_html,'ASS_TAG':re.compile(r'\{\\[^}]+}')}
    tree=ast.parse((Path(__file__).resolve().parents[1]/'app/v51.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='markup_kind')
    exec(compile(ast.Module(body=[node],type_ignores=[]),'markup','exec'),scope)
    v51=types.ModuleType('app.v51');v51.markup_kind=scope['markup_kind'];v51.TEXT_SUBTITLE_CODECS={'subrip'}
    sys.modules['app.v51']=v51
    with isolated() as db:
        db.executescript('''CREATE TABLE subtitle_extended_media(path TEXT PRIMARY KEY,modified BIGINT,size BIGINT,markup_version INTEGER,indexed_at TEXT);
          CREATE TABLE subtitle_extended_index(path TEXT,source TEXT,type_index INTEGER,external_path TEXT,codec TEXT,markup TEXT);
          CREATE TABLE media_stream_index_state(path TEXT,modified_ns BIGINT,size BIGINT,indexed_at TEXT);
          CREATE TABLE subtitle_cache_media(path TEXT,format_version INTEGER,expected_tracks INTEGER,cached_tracks INTEGER,cached_at TIMESTAMPTZ);
          CREATE TABLE subtitle_cache_track(path TEXT,source TEXT,type_index INTEGER,external_path TEXT,text_content TEXT);
          CREATE TABLE subtitle_cache_pending(path TEXT);''')
        for path,markup,version in [('italic','HTML tags',3),('mixed','HTML tags',4),('breaks','HTML tags',4),
                                   ('closing','HTML tags',4),('underline','HTML tags',5),('uncached','HTML tags',4),('unchanged','None',3),
                                   ('unchanged4','None',4),('stale','HTML tags',4),('current','HTML tags',6)]:
            db.execute('INSERT INTO subtitle_extended_media VALUES(?,100,1000,?,?)',(path,version,'2026-10-03T10:00:00Z'))
            db.execute("INSERT INTO subtitle_extended_index VALUES(?,'embedded',0,'','subrip',?)",(path,markup))
            db.execute('INSERT INTO media_stream_index_state VALUES(?,?,1000,?)',(path,90000000000 if path=='stale' else 100000000000,'2026-10-03T09:00:00Z'))
        for path,text in [('italic','<i>Hello</i>'),('mixed','<i><b>Hello<br/>world</b></i>'),
                          ('breaks','Hello<br>world</br><br />'),
                          ('closing','<font color="red"><i>Hello<br/>world</br></i></font>'),
                          ('underline','<i><u>Hello</u></i>'),
                          ('stale','<i>Hello</i>'),('current','<b>Hello</b>')]:
            db.execute('INSERT INTO subtitle_cache_media VALUES(?,1,1,1,?)',(path,'2026-10-03T09:30:00Z'))
            db.execute("INSERT INTO subtitle_cache_track VALUES(?,'embedded',0,'',?)",(path,text))
    result=policy.refresh_cached_markup()
    assert result=={'unchanged_media':2,'cached_media':5},result
    with isolated() as db:
        versions={r['path']:r['markup_version'] for r in db.execute('SELECT * FROM subtitle_extended_media')}
        markup={r['path']:r['markup'] for r in db.execute('SELECT * FROM subtitle_extended_index')}
    assert versions=={'italic':6,'mixed':6,'breaks':6,'closing':6,'underline':6,'uncached':4,'unchanged':6,'unchanged4':6,'stale':4,'current':6},versions
    assert markup['italic']=='None' and markup['mixed']=='HTML tags'
    assert markup['breaks']=='None' and markup['closing']=='None' and markup['current']=='HTML tags'
    assert markup['underline']=='None'
    assert policy.refresh_cached_markup()=={'unchanged_media':0,'cached_media':0}
    print('PASS: PostgreSQL upgrades prior policies; italic/underline/break-only removed, closing tags retained, mixed findings kept; stale/uncached deferred, idempotent')
finally:
    with original_connect() as db:
        db.raw.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
