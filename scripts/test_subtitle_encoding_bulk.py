"""Bulk Windows-1252 quickfix contracts; disposable fixtures, no live writes."""
import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from pydantic import ValidationError
from app import subtitle_autofix_media as workflow
from app.subtitle_cache import TextSubtitle
from app.subtitle_encoding import LEGACY_ENCODING


def refused(status, action):
    try:
        action()
    except HTTPException as exc:
        assert exc.status_code == status, (status, exc.status_code, exc.detail)
    else:
        raise AssertionError('Unsafe action accepted')


items = [dict(path=f'/fixture/{i}.mkv', label=f'Movie {i}', selected_streams=[
    dict(source='embedded', type_index=n, external_path='') for n in range(2)]) for i in range(40)]
with patch.object(workflow, '_encoding_report_items', return_value=items), \
     patch('app.preflight_dispatcher.enqueue_bulk_preflight', return_value={'id': 123}) as enqueue:
    result = workflow.queue_encoding_report(workflow.EncodingReportRequest(kind='movies'))
    assert result == dict(preflight_id=123, media_count=40, stream_count=80, accepted=True)
    assert enqueue.call_args.args == (workflow.ENCODING_BULK_OPERATION, items)
    assert enqueue.call_args.kwargs['mode'] == 'queued'
with patch.object(workflow, '_encoding_report_items', return_value=[]), \
     patch('app.preflight_dispatcher.enqueue_bulk_preflight') as enqueue:
    assert not workflow.queue_encoding_report(workflow.EncodingReportRequest(kind='tv'))['accepted']
    enqueue.assert_not_called()
try:
    workflow.EncodingReportRequest(kind='all')
except ValidationError:
    pass
else:
    raise AssertionError('An unspecified cross-report scope was accepted')

db = sqlite3.connect(':memory:')
db.row_factory = sqlite3.Row
db.executescript('''
    CREATE TABLE task_queue(id INTEGER PRIMARY KEY,status TEXT,payload_json TEXT);
    CREATE TABLE media_change_request(task_id INTEGER,path TEXT);
    CREATE TABLE tv_edit_sessions(show_id TEXT,status TEXT);
    CREATE TABLE plex_media(path TEXT,library_key TEXT,show_title TEXT);
    CREATE TABLE subtitle_extended_index(path TEXT);
    CREATE TABLE subtitle_extended_media(path TEXT);
''')


@contextmanager
def connection():
    with db:
        yield db


with tempfile.TemporaryDirectory(prefix='vse-bulk-encoding-') as directory:
    media = Path(directory) / 'fixture.mkv'
    media.write_bytes(b'original media; no real writer is used')
    signature = dict(path=str(media), size=media.stat().st_size, mtime_ns=media.stat().st_mtime_ns, digest='before')
    fingerprint = dict(path=str(media), exists=True, size=media.stat().st_size, mtime_ns=media.stat().st_mtime_ns, inode=1)
    request = dict(path=str(media), _preflight_fingerprint=fingerprint, selected_streams=items[0]['selected_streams'])
    with patch.object(workflow, 'authorized_import_file', return_value=media), \
         patch.object(workflow, '_available'), \
         patch.object(workflow.tasks, 'media_configuration_signature', return_value=signature) as capture:
        assert workflow._preflight_encoding_report_item(request, fingerprint)['decision'] == 'approved'
        capture.reset_mock()
        assert workflow._preflight_encoding_report_item(request, dict(fingerprint, mtime_ns=9))['decision'] == 'stale'
        capture.assert_not_called()

    # The worker ignores only its own marker, not another pending mutation or
    # an active TV-show draft. Final Version remains protected by the same gate.
    db.execute("INSERT INTO task_queue VALUES(7,'running','{}')")
    db.execute('INSERT INTO media_change_request VALUES(7,?)', (str(media),))
    db.execute("INSERT INTO plex_media VALUES(?,'2','Fixture show')", (str(media),))
    with patch.object(workflow, 'connection', connection), patch('app.v86.assert_media_editable'):
        workflow._available(str(media), ignore_task_id=7)
        refused(423, lambda: workflow._available(str(media)))
        db.execute("INSERT INTO task_queue VALUES(8,'pending','{}')")
        db.execute('INSERT INTO media_change_request VALUES(8,?)', (str(media),))
        refused(423, lambda: workflow._available(str(media), ignore_task_id=7))
        db.execute('DELETE FROM media_change_request WHERE task_id=8')
        db.execute('DELETE FROM task_queue WHERE id=8')
        db.execute("INSERT INTO tv_edit_sessions VALUES('2:Fixture show','open')")
        refused(423, lambda: workflow._available(str(media), ignore_task_id=7))
        db.execute('DELETE FROM tv_edit_sessions')

    text = '1\n00:00:01,000 --> 00:00:02,000\n<i>Fez café?</i> Não, amanhã.\n'
    streams = [dict(source='embedded', type_index=i, external_path='', codec='subrip',
                    label=f'Subtitle {i+1}', language='pt-BR', title='Preserve title') for i in range(5)]
    manifest = [{key: stream[key] for key in ('source', 'type_index', 'external_path', 'codec')} for stream in streams]
    original = [TextSubtitle('embedded', i, '', 'subrip', text if i != 3 else text + '\x00',
                            source_encoding='UTF-8' if i == 2 else LEGACY_ENCODING) for i in range(5)]
    payload = dict(path=str(media), repair_kind='encoding', approval_mode='bulk_without_review',
                   reviewed_signature=signature, _media_signature=signature, rule_name='Normalize Windows-1252 to UTF-8',
                   rule_id='', rule_revision=0, mode='queue',
                   selected_streams=[{key: s[key] for key in ('source','type_index','external_path')} for s in streams[:4]])
    receipts = {}

    def record(task_id, step, value):
        receipts[(task_id, step)] = copy.deepcopy(value)

    with ExitStack() as mocks:
        for stub in [
            patch.object(workflow, 'authorized_import_file', return_value=media),
            patch.object(workflow, 'connection', connection),
            patch.object(workflow, '_streams', return_value=streams),
            patch.object(workflow, 'probe', return_value={}),
            patch.object(workflow.tasks, 'media_configuration_signature', return_value=signature),
            patch('app.subtitle_cache_worker.text_track_manifest', return_value=('cache-signature', manifest)),
            patch('app.subtitle_cache.get_valid_tracks', return_value=original),
            patch('app.subtitle_cache_worker._extract_track', side_effect=AssertionError('Valid cache extracted again')),
            patch('app.subtitle_cache.publish_track'), patch('app.v86.assert_media_editable'),
            patch('app.job_safety.receipt', side_effect=lambda task, step: receipts.get((task, step))),
            patch('app.job_safety.record', side_effect=record), patch('app.job_safety.verify_output'),
            patch.object(workflow.tasks, 'update_progress'),
            patch.object(workflow, 'decision', side_effect=AssertionError('Bulk requested per-stream approval')),
            patch.object(workflow, 'apply_review', side_effect=AssertionError('Bulk submitted another job')),
        ]:
            mocks.enter_context(stub)
        available = mocks.enter_context(patch.object(workflow, '_available'))
        batch = mocks.enter_context(patch('app.subtitle_cache_worker._extract_embedded_batch', return_value={}))
        writer = mocks.enter_context(patch('app.subtitle_cleanup.replace_reviewed_subtitles',
            return_value=dict(changed=True, path=str(media), changed_subtitles=2, remuxes=1)))
        indexes = mocks.enter_context(patch('app.v80.request_media_indexes'))
        result = workflow.process_autofix(7, payload)
        assert result['remuxes'] == 1 and result['skipped_subtitles'] == 2
        assert result['approval_mode'] == 'bulk_without_review'
        assert any('control characters' in message for message in result['warnings'])
        selected = writer.call_args.args[1]
        assert [s['type_index'] for s in selected] == [0, 1], 'Unlisted/UTF-8/unsafe stream changed'
        assert all(s['text'] == text and s['normalize_encoding'] for s in selected)
        assert all(s['before_digest'] == hashlib.sha256(text.encode()).hexdigest() for s in selected)
        assert indexes.call_args.kwargs['detection_scope'] == {'subtitle_indices': [0, 1]}
        assert available.call_args.kwargs == {'ignore_task_id': 7}
        durable = json.loads(db.execute('SELECT payload_json FROM task_queue WHERE id=7').fetchone()[0])
        assert durable['replacements'] == selected and durable['_media_signature'] == signature
        assert not workflow._reviews, 'Bulk kept its transient preview/full text in memory'
        batch.assert_called_once_with(media, [])
        with patch.object(workflow, '_prepare_review', side_effect=AssertionError('Committed job prepared/remuxed again')):
            workflow.process_autofix(7, durable)
        assert writer.call_count == 1
        receipts.clear()
        skipped_payload = dict(payload, selected_streams=payload['selected_streams'][2:])
        skipped_payload.pop('replacements')
        assert not workflow.process_autofix(7, skipped_payload)['changed']
        assert writer.call_count == 1 and not workflow._reviews
        stale = dict(payload, reviewed_signature=dict(signature, digest='changed'))
        stale.pop('replacements')
        refused(409, lambda: workflow.process_autofix(7, stale))
        assert writer.call_count == 1 and not workflow._reviews
        db.execute("UPDATE task_queue SET status='pending' WHERE id=7")
        no_owner = dict(payload)
        no_owner.pop('replacements')
        refused(409, lambda: workflow.process_autofix(7, no_owner))
        assert writer.call_count == 1 and not workflow._reviews
        assert media.read_bytes() == b'original media; no real writer is used'

print('PASS: whole-report queue scope, empty/invalid scope, stale fingerprint, own-task/other-mutation/draft protection; no real jobs')
print('PASS: no per-stream approvals, selected cached inputs only, guarded Windows-1252 round trip, unsafe/UTF-8/unlisted exclusion, one writer, durable exact texts before mutation, narrow reinspection, receipt recovery and transient memory cleanup')
