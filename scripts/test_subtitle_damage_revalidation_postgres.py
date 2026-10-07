"""OCR-only refresh against disposable PostgreSQL rows, never catalog media."""
import ast
import hashlib
import json
import re
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import db_bootstrap  # Load the encrypted connection, never print it.
from app.pg_compat import connect
from app.subtitle_damage_policy import OCR_REASON, has_mojibake, isolated_letter_stats
from app.subtitle_damage_report import reasons
from psycopg import sql

schema = 'vse_damage_test_' + uuid.uuid4().hex
with connect() as db:
    db.raw.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))


@contextmanager
def isolated():
    with connect() as db:
        db.raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        yield db


def function(filename, name, scope):
    node = next(n for n in ast.parse((ROOT / filename).read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), filename, 'exec'), scope)
    return scope[name]


scope = {'re': re}
classify = function('app/v51.py', 'damage_kind', scope)
scope = {'connection': isolated, 'hashlib': hashlib, 'json': json, 'CACHE_FORMAT_VERSION': 1,
         'has_mojibake': has_mojibake, 'isolated_letter_stats': isolated_letter_stats,
         'OCR_REASON': OCR_REASON, 'reasons': reasons, 'damage_kind': classify}
refresh = function('scripts/revalidate_subtitle_damage.py', 'run', scope)

song = '\n\n'.join(f'{i}\n00:00:01,000 --> 00:00:02,000\n<i>Olá.</i>' for i in range(1, 13))
credit = '1\n00:00:00,642 --> 00:00:08,641\nleg. para remendos rmz BRRIP\n43m32s / 1h07m47s / 1h22m43s'
gibberish = '1\n00:00:01,000 --> 00:00:02,000\na b c d e f g h i j k l'
valid_updates = {'italic', 'timestamp-credit', 'old-sample', 'mixed'}
deferred = {'uncached', 'partial', 'old-format', 'pending-cache', 'failed-cache', 'pending-index',
            'running-index', 'invalid-hash', 'graphical'}
races = {'race-index', 'race-cache', 'race-signature', 'race-pending', 'race-failure', 'race-queued'}

try:
    with isolated() as db:
        db.executescript('''
          CREATE TABLE subtitle_extended_index(path TEXT,source TEXT,type_index INT,external_path TEXT,codec TEXT,damage TEXT,
            PRIMARY KEY(path,source,type_index,external_path));
          CREATE TABLE subtitle_extended_media(path TEXT PRIMARY KEY,indexed_at TEXT);
          CREATE TABLE subtitle_cache_media(path TEXT PRIMARY KEY,format_version INT,expected_tracks INT,cached_tracks INT,source_signature TEXT);
          CREATE TABLE subtitle_cache_track(path TEXT,source TEXT,type_index INT,external_path TEXT,text_content TEXT,sha256 TEXT,
            cached_at TIMESTAMPTZ,extraction_status TEXT,PRIMARY KEY(path,source,type_index,external_path));
          CREATE TABLE subtitle_cache_pending(path TEXT);
          CREATE TABLE subtitle_cache_failure(path TEXT);
          CREATE TABLE index_task_queue(id BIGSERIAL PRIMARY KEY,path TEXT,job TEXT,status TEXT);
        ''')
        for path in sorted(valid_updates | deferred | races | {'actual-ocr', 'other-reason'}):
            damage = OCR_REASON if path != 'other-reason' else 'Possible mojibake'
            if path == 'mixed':
                damage += ' + Possible mojibake + Control characters'
            codec = 'hdmv_pgs_subtitle' if path == 'graphical' else 'subrip'
            source = 'external' if path == 'timestamp-credit' else 'embedded'
            external = '/fixture/credit.srt' if source == 'external' else ''
            index = -1 if source == 'external' else 0
            db.execute('INSERT INTO subtitle_extended_index VALUES(?,?,?,?,?,?)',
                       (path, source, index, external, codec, damage))
            db.execute('INSERT INTO subtitle_extended_media VALUES(?,?)', (path, '2026-09-20T20:00:00-03:00'))
            if path == 'uncached':
                continue
            text = gibberish if path == 'actual-ocr' else credit if path == 'timestamp-credit' else song
            if path == 'mixed':
                text += '\nCafÃ©. \x0b'
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            db.execute('INSERT INTO subtitle_cache_media VALUES(?,?,?,?,?)',
                       (path, 0 if path == 'old-format' else 1, 2 if path == 'partial' else 1, 1, 'source-1'))
            db.execute('INSERT INTO subtitle_cache_track VALUES(?,?,?,?,?,?,?,?)',
                       (path, source, index, external, text, 'invalid' if path == 'invalid-hash' else digest,
                        '2026-10-06T12:00:00-03:00', 'ready'))
        db.execute("INSERT INTO subtitle_cache_pending VALUES('pending-cache')")
        db.execute("INSERT INTO subtitle_cache_failure VALUES('failed-cache')")
        for path, status in [('pending-index', 'pending'), ('running-index', 'running')]:
            db.execute("INSERT INTO index_task_queue(path,job,status) VALUES(?,'subtitles',?)", (path, status))

    def labels():
        with isolated() as db:
            return {r['path']: r['damage'] for r in db.execute('SELECT path,damage FROM subtitle_extended_index')}

    def cache_snapshot():
        with isolated() as db:
            return [dict(r) for r in db.execute('SELECT * FROM subtitle_cache_track ORDER BY path')]

    before, cached_before = labels(), cache_snapshot()
    dry = refresh(ocr_only=True)
    assert dry['updated'] == 0 and dry['changed'] == len(valid_updates | races), dry
    assert dry['invalid_cache_skip'] == 1 and labels() == before
    assert cache_snapshot() == cached_before, 'Dry-run altered cached subtitles'

    calls = 0
    @contextmanager
    def concurrent():
        global calls
        calls += 1
        with isolated() as db:
            if calls == 2:
                # Changes arrive after the batch was read, before its UPDATE.
                db.execute("UPDATE subtitle_extended_media SET indexed_at='2026-10-06T13:00:00-03:00' WHERE path='race-index'")
                db.execute("UPDATE subtitle_cache_track SET cached_at=CURRENT_TIMESTAMP WHERE path='race-cache'")
                db.execute("UPDATE subtitle_cache_media SET source_signature='new-source' WHERE path='race-signature'")
                db.execute("INSERT INTO subtitle_cache_pending VALUES('race-pending')")
                db.execute("INSERT INTO subtitle_cache_failure VALUES('race-failure')")
                db.execute("INSERT INTO index_task_queue(path,job,status) VALUES('race-queued','subtitles','pending')")
            yield db
    scope['connection'] = concurrent
    applied = refresh(True, ocr_only=True)
    after = labels()
    assert applied['updated'] == len(valid_updates) and applied['concurrent_skip'] == len(races), applied
    for path in valid_updates - {'mixed'}:
        assert after[path] == 'None', (path, after[path])
    assert after['mixed'] == 'Possible mojibake + Control characters', after['mixed']
    for path in deferred | races | {'actual-ocr', 'other-reason'}:
        assert after[path] == before[path], (path, after[path])

    scope['connection'] = isolated
    # The remaining race guards become ordinary deferred/unchanged records;
    # release only timestamp guards to prove a subsequent run can safely finish.
    later = refresh(True, ocr_only=True)
    assert later['updated'] == 3, later
    assert refresh(True, ocr_only=True)['updated'] == 0, 'Refresh is not idempotent'
    with isolated() as db:
        text_after = {r['path']: (r['text_content'], r['sha256']) for r in db.execute('SELECT * FROM subtitle_cache_track')}
    assert text_after == {r['path']: (r['text_content'], r['sha256']) for r in cached_before}
    print('PASS: OCR refresh dry-run/apply, complete-cache/hash guards, concurrent cache/inspection/queue guards, genuine damage retained, no text rewrites, idempotence; disposable schema only')
finally:
    with connect() as db:
        db.raw.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
