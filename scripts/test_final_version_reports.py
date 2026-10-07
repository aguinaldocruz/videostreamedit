"""All report queries against a disposable PostgreSQL schema, never real media.

Run inside the application container. No workers, scans, or catalog mutations.
"""
import ast
import hashlib
import json
import sys
import types
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db_bootstrap, pg_compat, matroska_layout
from app.detection_policy import final_paths
from app.subtitle_detector_config import SUBTITLE_DETECTOR_VERSION
from app.subtitle_html import MARKUP_VERSION
from fastapi import HTTPException
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
schema = 'vse_final_reports_test_' + uuid.uuid4().hex


@contextmanager
def connection():
    with pg_compat.connect() as db:
        db.raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        yield db


def functions(file, names, scope):
    nodes = []
    for node in ast.parse((ROOT / file).read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert {node.name for node in nodes} == names
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, 'exec'), scope)


def media_paths(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'path' and isinstance(item, str):
                found.add(item)
            elif key == 'paths':
                found.update(item)
            else:
                found.update(media_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.update(media_paths(item))
    return found


with pg_compat.connect() as db:
    db.raw.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
try:
    with connection() as db:
        db.executescript('''
            CREATE TABLE plex_media(path TEXT PRIMARY KEY,kind TEXT,library_key TEXT,
                show_title TEXT,title TEXT,season_number INTEGER,episode_number INTEGER);
            CREATE TABLE media_notes(entity_type TEXT,entity_key TEXT,final_version INTEGER,reviewed INTEGER);
            CREATE TABLE media_change_request(path TEXT,task_id INTEGER);
            CREATE TABLE task_queue(id INTEGER,task_type TEXT,status TEXT,payload_json TEXT);
            CREATE TABLE preflight_requests(operation_type TEXT,status TEXT,media_path TEXT,payload_json TEXT);
            CREATE TABLE index_task_queue(path TEXT,job TEXT,status TEXT);
            CREATE TABLE media_stream_index(path TEXT,source TEXT,type_index INTEGER,stream_type TEXT,
                external_path TEXT,language TEXT,region TEXT,track_name TEXT,codec TEXT,is_forced INTEGER,filename_tags TEXT);
            CREATE TABLE subtitle_extended_index(path TEXT,source TEXT,type_index INTEGER,
                external_path TEXT,codec TEXT,markup TEXT,damage TEXT);
            CREATE TABLE subtitle_extended_media(path TEXT,markup_version INTEGER);
            CREATE TABLE portuguese_language_detection(path TEXT,source TEXT,type_index INTEGER,
                external_path TEXT,metadata_language TEXT,metadata_region TEXT,detected_language TEXT,
                confidence DOUBLE PRECISION,analysis_status TEXT,analysis_reason TEXT,detector_version INTEGER,
                evidence TEXT,sdh_label TEXT,sdh_confidence DOUBLE PRECISION,sdh_evidence TEXT,
                evidence_sample TEXT,cue_count INTEGER,text_chars INTEGER,text_coverage DOUBLE PRECISION,
                markup_count INTEGER,damage TEXT);
            CREATE TABLE audio_language_detection(path TEXT,type_index INTEGER,metadata_language TEXT,
                metadata_region TEXT,detected_language TEXT,confidence DOUBLE PRECISION,samples_json TEXT,mismatch INTEGER);
            CREATE TABLE duplicate_language_report_suppressions(path TEXT,stream_type TEXT,language TEXT,fingerprint TEXT);
            CREATE TABLE language_detection_settings(key TEXT,value TEXT);
            CREATE TABLE media_video_title(path TEXT,title TEXT);
            CREATE TABLE matroska_layout_check(path TEXT,size BIGINT,modified_ns BIGINT,
                detail TEXT,checked_at TEXT,status TEXT);
        ''')
        fixtures = []
        for label, kind, library, show, approved in [
            ('final-movie', 'movie', 'movies', None, True),
            ('reviewed-movie', 'movie', 'movies', None, False),
            ('final-show', 'episode', 'tv', 'Approved show', True),
            ('same-name-other-library', 'episode', 'other-tv', 'Approved show', False),
            ('final-episode', 'episode', 'tv', 'Mixed show', True),
            ('open-episode', 'episode', 'tv', 'Mixed show', False),
            ('unknown-show', 'episode', 'unknown', None, True),
            ('blank-show', 'episode', 'unknown', '', True),
        ]:
            for variant in ('rich', 'audio-only'):
                path = f'/fixture/{label}-{variant}.mkv'
                fixtures.append({'path': path, 'kind': kind, 'library_key': library,
                                 'show_title': show, 'approved': approved, 'variant': variant})
                db.execute('INSERT INTO plex_media VALUES(?,?,?,?,?,1,1)', (path, kind, library, show, label))
                if kind == 'movie' or label == 'final-episode':
                    db.execute('INSERT INTO media_notes VALUES(?,?,?,1)',
                               ('movie' if kind == 'movie' else 'tv', path if kind == 'movie' else 'episode:' + path, int(approved)))
                streams = [('embedded', 0, 'audio', '', 'en', 'aac', 1)]
                if variant == 'rich':
                    streams += [('embedded', i, 'audio', '', lang, 'aac', 0)
                                for i, lang in enumerate(('en', 'es', 'de', 'ja'), 1)]
                    streams += [('embedded', 0, 'subtitle', '', 'en', 'subrip', 0),
                                ('embedded', 1, 'subtitle', '', 'en', 'subrip', 0),
                                ('embedded', 2, 'subtitle', '', 'en', 'pgs', 0),
                                ('external', -1, 'external', path + '.srt', 'en', 'srt', 0)]
                    db.execute("INSERT INTO subtitle_extended_index VALUES(?,'embedded',0,'','subrip','HTML tags','Control characters')", (path,))
                    db.execute('INSERT INTO subtitle_extended_media VALUES(?,?)', (path, MARKUP_VERSION))
                    for index, status in ((0, 'mismatch'), (1, 'no_confidence')):
                        db.execute('INSERT INTO portuguese_language_detection VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (path, 'embedded', index, '', 'en', '', 'pt', .95, status, 'Fixture',
                                    SUBTITLE_DETECTOR_VERSION, '', '', 0, '', '', 100, 1000, .8, 1, ''))
                for source, index, stream_type, external, language, codec, forced in streams:
                    db.execute('INSERT INTO media_stream_index VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (path, source, index, stream_type, external, language, '', '', codec, forced, '[]'))
                db.execute('INSERT INTO audio_language_detection VALUES(?,0,?,?,?,.95,?,1)',
                           (path, 'en', '', 'pt', '[{"language":"pt"},{"language":"pt"}]'))
                db.execute('INSERT INTO media_video_title VALUES(?,?)', (path, 'Old title'))
                db.execute("INSERT INTO matroska_layout_check VALUES(?,100,100,'Fixture','2026-10-05','tracks_after_cluster')", (path,))
        db.execute("INSERT INTO media_notes VALUES('tv','tv:Approved show',1,1)")
        db.execute("INSERT INTO media_notes VALUES('tv','unknown:Unknown show',1,1)")

    def movies():
        return [{'path': m['path'], 'name': Path(m['path']).name} for m in fixtures if m['kind'] == 'movie']

    def shows():
        groups = {}
        for m in fixtures:
            if m['kind'] == 'episode':
                key = m['library_key'] + ':' + (m['show_title'] or 'Unknown show')
                groups.setdefault(key, {'id': key, 'name': m['show_title'] or 'Unknown show',
                                        'seasons': [{'episodes': []}]})['seasons'][0]['episodes'].append(
                                            {'path': m['path'], 'name': Path(m['path']).name})
        return list(groups.values())

    scope = {'Path': Path, 'json': json, 'hashlib': hashlib, 'Literal': Literal,
             'connection': connection, 'HTTPException': HTTPException, 'plex_movies': movies, 'plex_tv': shows,
             '_configured_common_languages': lambda: ['pt'], '_duplicate_language_values': lambda kind: [],
             'ISO_639_TO_1': {'eng': 'en'}, 'MARKUP_VERSION': MARKUP_VERSION,
             'SUBTITLE_DETECTOR_VERSION': SUBTITLE_DETECTOR_VERSION,
             'IMAGE_SUBTITLE_CODECS': ('pgs',)}
    functions('app/v5.py', {'canonical_language'}, scope)
    reports = {'image_subtitle_report', 'html_subtitle_report', 'damaged_subtitle_report',
               'portuguese_language_report', 'subtitle_no_confidence_report', 'duplicate_language_report',
               'forced_stream_report', 'english_only_report', 'audio_only_report',
               'external_only_report', 'video_title_report', 'uncommon_language_report'}
    functions('app/v19.py', reports | {'report_blocked_paths', 'report_availability', '_report_episode_label',
              '_canonical_detection_value', '_detection_row_current', '_uncommon_language_rows',
              '_duplicate_group_fingerprint'}, scope)
    checked_layout_paths = []
    matroska_layout.active_remux_paths = lambda: set()

    def layout_rows(rows):
        paths = {str(row['path']) for row in rows}
        checked_layout_paths.extend(paths)
        return paths

    matroska_layout.revalidate_warning_rows = layout_rows
    functions('app/v82.py', {'matroska_layout_report'}, scope)
    # The audio report imports v19 at call time; expose only tested helpers,
    # without importing the application/worker startup chain.
    module = types.ModuleType('app.v19')
    module.report_blocked_paths = scope['report_blocked_paths']
    module.audio_detection_by_path = lambda paths: {path: {} for path in paths}
    sys.modules['app.v19'] = module
    scope['_audio_language_code'] = scope['canonical_language']
    functions('app/v79.py', {'audio_language_report'}, scope)

    expected_final = {m['path'] for m in fixtures if m['approved']}
    with connection() as db:
        assert final_paths(db=db) == expected_final
        assert final_paths([], db=db) == set()
        assert final_paths(['/fixture/final-movie-rich.mkv', '/fixture/reviewed-movie-rich.mkv'], db=db) == {'/fixture/final-movie-rich.mkv'}

    def audit(excluded):
        results = []
        for kind in ('movies', 'tv'):
            for name in sorted(reports):
                for args in ((kind, 'audio'), (kind, 'subtitle')) if name == 'duplicate_language_report' else ((kind,),):
                    result = scope[name](*args)
                    assert result['media_count'] > 0, (name, args, result)
                    assert not media_paths(result) & excluded, (name, args)
                    results.append(result)
            result = scope['matroska_layout_report'](kind)
            assert result['media_count'] > 0 and not media_paths(result) & excluded
            results.append(result)
        voice = scope['audio_language_report']()
        assert voice['media_count'] > 0 and not media_paths(voice) & excluded
        results.append(voice)
        counts = scope['report_availability']()['counts']
        assert all(any(counts[key].values()) for key in counts), counts
        return results, counts

    before, counts_before = audit(expected_final)
    assert not set(checked_layout_paths) & expected_final, 'Final media were revalidated while reading a report'
    with connection() as db:
        db.execute('UPDATE media_notes SET final_version=0')
    after, counts_after = audit(set())
    assert all(a['media_count'] > b['media_count'] for a, b in zip(after, before)), 'Unfreezing did not restore every report'
    assert all(counts_after[k][kind] > counts_before[k][kind] for k in counts_before for kind in ('tv', 'movies'))
    with connection() as db:
        assert final_paths(db=db) == set()
        assert db.execute('SELECT count(*) AS n FROM subtitle_extended_index').fetchone()['n'] == 8
        # Preserve existing in-flight exclusions; failure history is not an approval.
        path = '/fixture/reviewed-movie-rich.mkv'
        db.execute("INSERT INTO task_queue VALUES(1,'media_edit','pending',?)", (json.dumps({'edit': {'path': path}}),))
    assert path in scope['report_blocked_paths']()
    with connection() as db:
        db.execute("UPDATE task_queue SET status='failed'")
    assert path not in scope['report_blocked_paths']()
    print('PASS: all 29 movie/TV/audio report queries and all report counts exclude final movies, episodes and whole shows; reviewed-only stays eligible; unfreezing restores findings; no final file checks or catalog scans')
finally:
    with pg_compat.connect() as db:
        db.raw.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
