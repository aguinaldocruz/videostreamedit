"""Isolated episode/movie discovery and note-identity tests; no DB or Plex I/O."""
import ast
import logging
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def load(file, names, scope):
    nodes = [n for n in ast.parse((ROOT / file).read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, 'exec'), scope)


class Rows(list):
    def fetchone(self):
        return self[0] if self else None


with TemporaryDirectory(prefix='vse-import-discovery-') as folder:
    root = Path(folder)
    movies = root / 'Movies'
    episodes = root / 'TV' / 'Show' / 'Season 01'
    movies.mkdir(); episodes.mkdir(parents=True)
    calls = []; queries = []
    media = {'movie': [{'library_key': '1', 'path': str(movies / 'Existing.mkv')}],
             'episode': [{'library_key': '2', 'path': str(episodes / 'Show.S01E01.mkv')}]}

    class DB:
        def execute(self, sql, params=()):
            queries.append((sql, params))
            if sql.startswith('SELECT library_key,path'):
                return Rows(media[params[0]])
            if sql.startswith('INSERT INTO media_notes'):
                return Rows()
            raise AssertionError(sql)

    @contextmanager
    def connection():
        yield DB()

    fake_tasks = types.ModuleType('app.v65')
    fake_tasks.enqueue = lambda *a, **kw: calls.append((a, kw))
    sys.modules['app.v65'] = fake_tasks
    scope = dict(Path=Path, os=os, connection=connection, ReorderEditRequest=object,
                 logger=logging.getLogger('import-discovery-test'))
    load('app/v28.py', {'inside', 'queue_post_import_refresh', '_record_import_final_revision'}, scope)
    for kind, parent, key in [('movie', movies, '1'), ('episode', episodes, '2')]:
        source = movies / 'Existing.mkv'
        target = parent / 'Imported.S01E02.mkv'
        target.write_bytes(b'disposable fixture')
        scope['queue_post_import_refresh'](source, target, kind)
        args, kw = calls[-1]
        assert args[1] == {'path': str(target), 'library_key': key, 'rating_key': ''}
        assert kw['deduplicate'] is True
        scope['_record_import_final_revision'](types.SimpleNamespace(final_version=True), target, kind)
        params = queries[-1][1]
        assert params == (('tv', 'episode:' + str(target), 1, 1) if kind == 'episode'
                          else ('movie', str(target), 1, 1))

    # Exercise the real Plex refresh worker with an episode library and the
    # bounded lookup fallback. Stub every network, indexing and DB operation.
    target = episodes / 'Imported.S01E02.mkv'
    library = {'library_key': '2', 'kind': 'show', 'title': 'TV'}
    item = {'ratingKey': 'new-episode', 'Media': [{'Part': [{'file': str(target)}]}]}
    class PlexDB:
        def execute(self, sql, params):
            assert params == ('2',)
            return Rows([library])

    @contextmanager
    def plex_connection():
        yield PlexDB()

    fallback = []; persisted = []; indexed = []
    indexing = types.ModuleType('app.v80')
    indexing.request_media_indexes = lambda *a, **kw: indexed.append((a, kw))
    indexing.detection_scope_for_operation = lambda value: value
    sys.modules['app.v80'] = indexing
    refresh_scope = dict(Path=Path, logger=logging.getLogger('import-test'),
                         time=types.SimpleNamespace(time=lambda: 100, sleep=lambda _: None),
                         tasks=types.SimpleNamespace(update_progress=lambda *a: None),
                         plex=types.SimpleNamespace(connection=plex_connection),
                         plex_scan=lambda *a: None,
                         plex_sync=types.SimpleNamespace(
                             changed_library_items=lambda *a: [],
                             paged_library=lambda key, kind: fallback.append((key, kind)) or [item],
                             rows_for_items=lambda lib, items: (items, []),
                             persist_library=lambda *a: persisted.append(a)))
    load('app/v74.py', {'item_for_path', 'process_plex_import_refresh'}, refresh_scope)
    result = refresh_scope['process_plex_import_refresh'](1, {'path': str(target), 'library_key': '2'})
    assert fallback == [('2', 'show')]
    assert persisted[0][0]['kind'] == 'show' and indexed[0][0][0] == str(target)
    assert result['rating_key'] == 'new-episode'

print('PASS: destination library selection, movie/episode note identities, episode Plex scan/fallback and indexing')
