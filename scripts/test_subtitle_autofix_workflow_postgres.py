"""Real queue/LUW/receipt contracts in a rollback-only PostgreSQL schema.

No worker starts, and all files and database rows belong to this disposable
fixture. Production jobs, sequence values, rules and catalog are untouched.
"""
import ast
import hashlib
import json
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

from app import postgres_store as store, job_safety as safety, v65 as tasks
from app import subtitle_autofix_media as autofix
from app.pg_compat import Connection

with store.connection() as raw, tempfile.TemporaryDirectory(prefix='vse-autofix-queue-') as folder:
    schema = 'vse_autofix_queue_' + uuid.uuid4().hex
    raw.execute(f'CREATE SCHEMA {schema}')
    raw.execute(f'SET LOCAL search_path TO {schema}')
    adapter = Connection.__new__(Connection)
    adapter.raw, adapter.total_changes = raw, 0

    @contextmanager
    def connection():
        yield adapter

    with patch.object(store, 'connection', lambda: nullcontext(raw)), \
         patch.object(safety, 'connection', lambda: nullcontext(raw)), \
         patch.object(tasks, 'connection', connection), \
         patch.object(autofix, 'connection', connection), \
         patch.object(tasks, 'wake_queue'):
        store.initialize_schema()
        # Reuse the queue's real schema, without invoking startup/recovery or
        # its cleanup routines. Its sequence is new and schema-local.
        tree = ast.parse(Path('app/v65.py').read_text())
        init = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'initialize_task_queue')
        ddl = next(n.args[0].value for n in ast.walk(init) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and n.func.attr == 'executescript')
        adapter.executescript(ddl)
        raw.execute('CREATE TABLE index_task_queue(id BIGINT PRIMARY KEY,group_id TEXT,job TEXT,status TEXT,path TEXT,expedite_until TEXT,error TEXT)')
        safety.initialize()

        media = Path(folder)/'fixture.mkv'
        media.write_bytes(b'fixture original')
        signature = {'path': str(media), 'size': media.stat().st_size, 'mtime_ns': media.stat().st_mtime_ns, 'digest': 'reviewed'}
        original = '1\n00:00:01,000 --> 00:00:02,000\nFez cafÃ©?\n'
        fixed = original.replace('cafÃ©', 'café')
        payload = dict(path=str(media), review_id=uuid.uuid4().hex, rule_id=uuid.uuid4().hex, rule_revision=3,
                       rule_name='Disposable Portuguese rule', mode='queue', reviewed_signature=signature,
                       _media_signature=signature, replacements=[dict(source='embedded', type_index=0, external_path='',
                           before_digest=hashlib.sha256(original.encode()).hexdigest(), text=fixed)])
        with patch.object(tasks, 'media_configuration_signature', return_value=signature):
            queued = tasks.enqueue(autofix.TASK_TYPE, payload, 'Disposable autofix', deduplicate=True)
            repeat = tasks.enqueue(autofix.TASK_TYPE, payload, 'Disposable autofix', deduplicate=True)
            assert queued['id'] == repeat['id']
            assert raw.execute('SELECT count(*) n FROM task_queue').fetchone()['n'] == 1
            assert 'text' not in queued['payload']['replacements'][0]
            row = raw.execute('SELECT payload_json,group_id FROM task_queue WHERE id=%s', (queued['id'],)).fetchone()
            durable = json.loads(row['payload_json'])
            assert durable['replacements'][0]['text'] == fixed
            assert raw.execute('SELECT path FROM media_change_request WHERE task_id=%s', (queued['id'],)).fetchone()['path'] == str(media)
            stage = raw.execute('SELECT task_type,status FROM workflow_stages WHERE group_id=%s', (uuid.UUID(row['group_id']),)).fetchone()
            assert stage == {'task_type': autofix.TASK_TYPE, 'status': 'pending'}
            luw = tasks._create_media_luw(queued['id'], autofix.TASK_TYPE, durable, str(media))
            assert luw and durable['_luw_strategy'] == 'verified-stream-copy-then-atomic-replacement'
            assert store.begin_task_stage(row['group_id'], autofix.TASK_TYPE, str(media), queued['id'])
            with patch.object(store, '_stage_root', side_effect=AssertionError('Unnecessary full media backup')):
                store.prepare_task_artifact(row['group_id'], queued['id'], autofix.TASK_TYPE, str(media), durable)
            assert raw.execute('SELECT count(*) n FROM workflow_artifacts').fetchone()['n'] == 0
            assert autofix.TASK_TYPE in tasks.task_priority_order()

        encoding_media = Path(folder)/'legacy.mkv'
        encoding_media.write_bytes(b'disposable legacy source')
        encoding_signature = {'path': str(encoding_media), 'digest': 'legacy-reviewed'}
        unchanged_text = '1\n00:00:01,000 --> 00:00:02,000\ncafé\n'
        encoding_payload = dict(payload, path=str(encoding_media), review_id=uuid.uuid4().hex,
                                repair_kind='encoding', rule_name='Normalize encoding to UTF-8', rule_id='', rule_revision=0,
                                reviewed_signature=encoding_signature, _media_signature=encoding_signature,
                                replacements=[dict(source='embedded',type_index=0,external_path='',text=unchanged_text,
                                                   before_digest=hashlib.sha256(unchanged_text.encode()).hexdigest(),
                                                   normalize_encoding=True,source_encoding='Windows-1252 (inferred)')])
        with patch.object(tasks, 'media_configuration_signature', return_value=encoding_signature):
            encoding_task=tasks.enqueue(autofix.TASK_TYPE,encoding_payload,'Disposable UTF-8 quickfix',deduplicate=True)
            repeated=tasks.enqueue(autofix.TASK_TYPE,encoding_payload,'Disposable UTF-8 quickfix',deduplicate=True)
            assert encoding_task['id']==repeated['id']
            encoding_row=raw.execute('SELECT payload_json,group_id FROM task_queue WHERE id=%s',(encoding_task['id'],)).fetchone()
            approved=json.loads(encoding_row['payload_json'])
            assert approved['repair_kind']=='encoding' and approved['replacements'][0]['normalize_encoding'] is True
            assert approved['replacements'][0]['text']==unchanged_text
            assert 'text' not in encoding_task['payload']['replacements'][0]
            assert tasks._create_media_luw(encoding_task['id'],autofix.TASK_TYPE,approved,str(encoding_media))
            assert store.begin_task_stage(encoding_row['group_id'],autofix.TASK_TYPE,str(encoding_media),encoding_task['id'])
            with patch.object(store,'_stage_root',side_effect=AssertionError('Encoding repair allocated a full media backup')):
                store.prepare_task_artifact(encoding_row['group_id'],encoding_task['id'],autofix.TASK_TYPE,str(encoding_media),approved)
            assert raw.execute('SELECT count(*) n FROM workflow_artifacts').fetchone()['n']==0

        # Bulk-report submission uses only metadata, includes every matching
        # cached stream (not the three example locations) and excludes other
        # encodings/codecs, incomplete/quarantined caches and non-report paths.
        from app.subtitle_cache import CACHE_FORMAT_VERSION
        from app import v19 as reports
        raw.execute('CREATE TABLE subtitle_cache_media(path TEXT PRIMARY KEY,format_version INTEGER,expected_tracks INTEGER,cached_tracks INTEGER)')
        raw.execute('CREATE TABLE subtitle_cache_track(path TEXT,source TEXT,type_index INTEGER,external_path TEXT,codec TEXT,extraction_status TEXT,source_encoding TEXT)')
        raw.execute('CREATE TABLE subtitle_cache_pending(path TEXT)')
        raw.execute('CREATE TABLE subtitle_cache_failure(path TEXT)')
        selected_refs = []
        for i in range(40):
            ref = dict(path=str(encoding_media), source='embedded', type_index=i, external_path='', codec='subrip', damage='Non-UTF-8 source bytes')
            selected_refs.append(ref)
            raw.execute("INSERT INTO subtitle_cache_track VALUES(%s,'embedded',%s,'','subrip','ready','Windows-1252 (inferred)')", (str(encoding_media),i))
        for i, codec, encoding in [(40,'ass','Windows-1252 (inferred)'),(41,'subrip','UTF-8'),(42,'subrip','UTF-16 (inferred)')]:
            selected_refs.append(dict(selected_refs[0], type_index=i, codec=codec))
            raw.execute("INSERT INTO subtitle_cache_track VALUES(%s,'embedded',%s,'',%s,'ready',%s)", (str(encoding_media),i,codec,encoding))
        raw.execute('INSERT INTO subtitle_cache_media VALUES(%s,%s,43,43)', (str(encoding_media),CACHE_FORMAT_VERSION))
        report = {'items':[dict(title='Fixture movie', paths=[str(encoding_media)], streams=selected_refs)]}
        with patch.object(reports,'damaged_subtitle_report',return_value=report):
            selected = autofix._encoding_report_items('movies')
            assert len(selected)==1 and len(selected[0]['selected_streams'])==40
            assert [s['type_index'] for s in selected[0]['selected_streams']]==list(range(40))
            raw.execute('INSERT INTO subtitle_cache_pending VALUES(%s)', (str(encoding_media),))
            assert not autofix._encoding_report_items('movies')
            raw.execute('DELETE FROM subtitle_cache_pending')
            raw.execute('INSERT INTO subtitle_cache_failure VALUES(%s)', (str(encoding_media),))
            assert not autofix._encoding_report_items('movies')
            raw.execute('DELETE FROM subtitle_cache_failure')
            raw.execute('UPDATE subtitle_cache_media SET cached_tracks=42')
            assert not autofix._encoding_report_items('movies')
            raw.execute('UPDATE subtitle_cache_media SET cached_tracks=43')

        # Stable identity survives adding the exact validated texts to the
        # existing job during execution. A replay cannot create another stage.
        bulk_items = {'_bulk_items':[dict(selected[0],_preflight_result={'signature':encoding_signature})]}
        before_count = raw.execute('SELECT count(*) n FROM task_queue').fetchone()['n']
        batch = autofix._approve_encoding_report_items(bulk_items,{})
        assert len(batch['task_ids'])==1
        bulk_task_id = batch['task_ids'][0]
        row = raw.execute('SELECT payload_json FROM task_queue WHERE id=%s',(bulk_task_id,)).fetchone()
        expanded = json.loads(row['payload_json'])
        assert expanded['approval_mode']=='bulk_without_review' and 'replacements' not in expanded
        expanded.update(replacements=encoding_payload['replacements'],bulk_encoding_warnings=[],skipped_subtitles=0)
        raw.execute("UPDATE task_queue SET payload_json=%s,status='running' WHERE id=%s",(json.dumps(expanded),bulk_task_id))
        replay = autofix._approve_encoding_report_items(bulk_items,{})
        assert replay['task_ids']==batch['task_ids']
        assert raw.execute('SELECT count(*) n FROM task_queue').fetchone()['n']==before_count+1

        # Durable atomic output receipts survive a bookkeeping failure without
        # rerunning a replacement. External modifications refuse recovery.
        candidate = Path(folder)/'candidate.mkv'
        candidate.write_bytes(b'fixture replacement')
        with safety.task_context(queued['id']):
            safety.replace_prepared(candidate, media, safety.stamp(media))
        assert safety.recovery(queued['id'], autofix.TASK_TYPE, durable) == (None, True)
        output_signature = {'path': str(media), 'digest': 'committed'}
        safety.record(queued['id'], 'autofix', dict(result={'changed': True}, signature=output_signature))
        with patch.object(tasks, 'media_configuration_signature', return_value=output_signature):
            result, resumed = safety.recovery(queued['id'], autofix.TASK_TYPE, durable)
            assert result is None and resumed
            assert safety.receipt(queued['id'], 'autofix')['result'] == {'changed': True}
        media.write_bytes(b'external change')
        with patch.object(tasks, 'media_configuration_signature', return_value={'path': str(media), 'digest': 'external'}):
            try:
                safety.recovery(queued['id'], autofix.TASK_TYPE, durable)
            except RuntimeError:
                pass
            else:
                raise AssertionError('Changed approved media was accepted for recovery')
        # Repair invalidation is narrow for embedded tracks and sidecars;
        # other subtitles, voice evidence and media-level state stay valid.
        from app import v80
        raw.execute('CREATE TABLE portuguese_language_detection(path TEXT,source TEXT,type_index INTEGER,external_path TEXT)')
        raw.execute('CREATE TABLE subtitle_detection_stream_state(path TEXT,source TEXT,type_index INTEGER,external_path TEXT)')
        raw.execute('CREATE TABLE portuguese_detection_state(path TEXT)')
        raw.execute('CREATE TABLE audio_language_detection(path TEXT,type_index INTEGER)')
        for table in ('portuguese_language_detection','subtitle_detection_stream_state'):
            raw.cursor().executemany(f'INSERT INTO {table} VALUES(%s,%s,%s,%s)',[
                (str(media),'embedded',0,''),(str(media),'embedded',1,''),
                (str(media),'external',-1,'/fixture/selected.srt'),(str(media),'external',-1,'/fixture/rejected.srt')])
        raw.execute('INSERT INTO portuguese_detection_state VALUES(%s)',(str(media),))
        raw.execute('INSERT INTO audio_language_detection VALUES(%s,0)',(str(media),))
        with patch.object(v80,'connection',connection):
            assert v80.invalidate_language_detection(str(media),{'subtitle_indices':[0],'subtitle_external_paths':['/fixture/selected.srt']})
        for table in ('portuguese_language_detection','subtitle_detection_stream_state'):
            remaining=raw.execute(f'SELECT source,type_index,external_path FROM {table} ORDER BY source').fetchall()
            assert remaining==[{'source':'embedded','type_index':1,'external_path':''},
                               {'source':'external','type_index':-1,'external_path':'/fixture/rejected.srt'}]
        assert raw.execute('SELECT count(*) n FROM audio_language_detection').fetchone()['n']==1
        assert raw.execute('SELECT count(*) n FROM portuguese_detection_state').fetchone()['n']==1
    raw.rollback()  # Removes the entire test schema, its rows and its sequence.

print('PASS: real PostgreSQL autofix/encoding queue deduplication, lightweight public payload/exact durable consent, report marker, staged media lock, LUW, priorities, no full-media backup, atomic recovery and stale-source refusal; production untouched')
