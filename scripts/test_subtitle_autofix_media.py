"""Supervised autofix decisions/signatures; no production jobs/media changed."""
import copy
import hashlib
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from app import subtitle_autofix as rules, subtitle_autofix_media as workflow, subtitle_autofix_text as text
from app.subtitle_cache import TextSubtitle


def refused(status, action):
    try:
        action()
    except HTTPException as exc:
        assert exc.status_code == status, (status, exc.status_code, exc.detail)
    else:
        raise AssertionError('Unsafe action accepted')


rule = rules.RuleFields(name='Repair Portuguese', languages=['pt-BR'], replacements=[
    {'from': 'Ã©', 'to': 'é'}, {'from': 'teh', 'to': 'the', 'match': 'word'},
    {'from': '00', 'to': '99'}, {'from': '1', 'to': 'ONE'},
])
srt = '1\r\n00:00:01,000 --> 00:00:02,000\r\n<font color="00FF00">Fez cafÃ©?</font> <i>teh</i> bother\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\n1\r\n'
fixed = text.fix_srt(rule, srt)
assert fixed['text'] == srt.replace('cafÃ©', 'café').replace('<i>teh</i>', '<i>the</i>').replace('\r\n1\r\n', '\r\nONE\r\n')
assert fixed['replacement_count'] == 3, fixed
assert '00:00:01,000' in fixed['text'] and 'color="00FF00"' in fixed['text']
assert not text.fix_srt(rule, fixed['text'])['changed']
refused(422, lambda: text.validate_replacement(srt, fixed['text'].replace('00:00:02,000', '00:00:09,000')))
large = '\n\n'.join(f'{i}\n00:00:01,000 --> 00:00:02,000\nFez cafÃ©?' for i in range(1, 1201))
assert len(large) > rules.MAX_PREVIEW_LENGTH
assert text.fix_srt(rule, large)['replacement_count'] == 1200, 'Full subtitle was truncated to sample limit'

db = sqlite3.connect(':memory:')
db.row_factory = sqlite3.Row
db.execute('CREATE TABLE application_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')


@contextmanager
def connection():
    with db:
        yield db


with patch.object(rules, 'connection', connection):
    saved = rules.create_rule(rule)

with tempfile.TemporaryDirectory(prefix='vse-autofix-workflow-') as directory:
    media = Path(directory)/'fixture.mkv'
    media.write_bytes(b'unmodified fixture media')
    signature = dict(path=str(media), size=media.stat().st_size, mtime_ns=media.stat().st_mtime_ns, digest='before')
    streams = [dict(source='embedded',type_index=i,external_path='',codec='subrip',label=f'Subtitle {i+1}',language=lang,title='')
               for i,lang in enumerate(('pt-BR','pt-BR','en','pt-BR'))]
    manifest = [{key: stream[key] for key in ('source','type_index','external_path','codec')} for stream in streams]
    original = [TextSubtitle('embedded',i,'','subrip',srt if i<3 else srt.replace('cafÃ©','café').replace('teh','the').replace('\r\n1\r\n','\r\nONE\r\n')) for i in range(4)]
    calls = []

    def enqueue(kind, payload, label, **_kw):
        calls.append((kind, copy.deepcopy(payload), label))
        return {'id': 7}

    with patch.object(workflow,'authorized_import_file',return_value=media), \
         patch.object(workflow,'_available'),patch.object(workflow,'connection',connection), \
         patch.object(workflow,'list_rules',return_value={'rules':[saved]}), \
         patch.object(workflow,'_streams',return_value=streams),patch.object(workflow,'probe',return_value={}), \
         patch.object(workflow.tasks,'media_configuration_signature',return_value=signature) as fingerprint, \
         patch('app.subtitle_cache_worker.text_track_manifest',return_value=('text-signature',manifest)), \
         patch('app.subtitle_cache.get_valid_tracks',return_value=original), \
         patch('app.subtitle_cache_worker._extract_embedded_batch',return_value={}) as batch, \
         patch('app.subtitle_cache_worker._extract_track',side_effect=AssertionError('Valid cache must avoid extraction')), \
         patch.object(workflow.tasks,'queue_paused',return_value=False) as paused, \
         patch.object(workflow.tasks,'enqueue',side_effect=enqueue):
        choice = workflow.options(workflow.MediaRequest(path=str(media)))
        assert choice['rules'][0]['matching_streams']==3
        request=workflow.PrepareRequest(path=str(media),rule_id=saved['id'])
        review=workflow.prepare(request)
        assert not calls and media.read_bytes()==b'unmodified fixture media'
        assert len(review['streams'])==2 and review['unchanged']==1
        assert 'text' not in review['streams'][0], 'Initial preview eagerly transfers every full subtitle'
        batch.assert_called_with(media, [])
        assert all(stream['cached'] for stream in review['streams'])
        first,second=review['streams']
        full=workflow.stream_preview(review['review_id'],first['id'])
        assert full['text']==fixed['text'] and full['original']==srt
        refused(409,lambda:workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='queue')))
        workflow.decision(review['review_id'],workflow.DecisionRequest(stream_id=first['id'],approve=True))
        refused(409,lambda:workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='queue')))
        review=workflow.decision(review['review_id'],workflow.DecisionRequest(stream_id=second['id'],approve=False))
        assert review['approved']==1 and review['reviewed']==2 and not calls
        fingerprint.return_value={**signature,'digest':'changed'}
        refused(409,lambda:workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='queue')))
        assert not calls
        fingerprint.return_value=signature
        paused.return_value=True
        refused(409,lambda:workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='now')))
        assert not calls
        result=workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='queue'))
        assert result==dict(task_id=7,mode='queue',accepted=True)
        assert len(calls)==1 and len(calls[0][1]['replacements'])==1
        payload=calls[0][1]
        assert payload['replacements'][0]['type_index']==0
        assert payload['replacements'][0]['text']==fixed['text']
        assert payload['replacements'][0]['before_digest']==hashlib.sha256(srt.encode()).hexdigest()
        assert payload['_media_signature']==signature and payload['reviewed_signature']==signature
        assert payload['rule_revision']==saved['revision']
        assert workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='queue'))==result
        assert len(calls)==1, 'Repeated Apply created a second job'
        assert workflow._reviews[review['review_id']]['bytes']==0
        assert not workflow._reviews[review['review_id']]['streams'], 'Submitted preview retained a second copy of full texts'
        public=workflow.tasks.public_task_payload(workflow.TASK_TYPE,payload)
        assert 'text' not in public['replacements'][0] and public['replacements'][0]['text_characters']==len(fixed['text'])
        assert payload['replacements'][0]['text']==fixed['text'], 'Presentation removed durable execution data'
        workflow.discard(review['review_id'])
        assert not workflow._reviews
        refused(410,lambda:workflow.stream_preview(review['review_id'],first['id']))
        # Cancel/reject has no execution side effects.
        paused.return_value=False
        review=workflow.prepare(workflow.PrepareRequest(path=str(media),rule_id=saved['id']))
        for stream in review['streams']:
            workflow.decision(review['review_id'],workflow.DecisionRequest(stream_id=stream['id'],approve=False))
        refused(422,lambda:workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='queue')))
        workflow.discard(review['review_id'])
        assert len(calls)==1
        review=workflow.prepare(workflow.PrepareRequest(path=str(media),rule_id=saved['id']))
        workflow._reviews[review['review_id']]['updated']=time.monotonic()-workflow.TTL_SECONDS-1
        refused(410,lambda:workflow.progress(review['review_id']))
        assert not workflow._reviews
        assert media.read_bytes()==b'unmodified fixture media'
        # In-flight previews count against the shared budget too, and a
        # refused preparation releases its own reservation.
        workflow._reviews['other'] = dict(status='preparing', updated=time.monotonic(), bytes=workflow.MAX_REVIEW_BYTES)
        request=workflow.PrepareRequest(path=str(media),rule_id=saved['id'])
        refused(429,lambda:workflow.prepare(request))
        assert workflow._reviews[request.operation_id]['status']=='failed'
        assert workflow._reviews[request.operation_id]['bytes']==0
        workflow._reviews.clear()

    # An older normalized cache is refreshed only for the rule's selected
    # language tracks, before consent. The source and media remain untouched.
    legacy_cache = [replace(item, extraction_revision=0) for item in original]
    with patch.object(workflow,'authorized_import_file',return_value=media), \
         patch.object(workflow,'_available'),patch.object(workflow,'connection',connection), \
         patch.object(workflow,'_streams',return_value=streams),patch.object(workflow,'probe',return_value={}), \
         patch.object(workflow.tasks,'media_configuration_signature',return_value=signature), \
         patch('app.subtitle_cache_worker.text_track_manifest',return_value=('text-signature',manifest)), \
         patch('app.subtitle_cache.get_valid_tracks',return_value=legacy_cache), \
         patch('app.subtitle_cache.publish_track') as publisher, \
         patch('app.subtitle_cache_worker._extract_embedded_batch',return_value={i:original[i] for i in (0,1,3)}) as batch, \
         patch('app.subtitle_cache_worker._extract_track',side_effect=AssertionError('One batch must supply selected legacy tracks')):
        review=workflow.prepare(workflow.PrepareRequest(path=str(media),rule_id=saved['id']))
        assert len(review['streams'])==2 and review['unchanged']==1
        assert not any(stream['cached'] for stream in review['streams'])
        assert [track['type_index'] for track in batch.call_args.args[1]] == [0,1,3]
        assert publisher.call_count==3
        assert media.read_bytes()==b'unmodified fixture media'
        workflow.discard(review['review_id'])

    # Native Matroska identities/regions win over legacy ffprobe aliases;
    # ambiguous/missing track identities must never match a different stream.
    metadata={'streams':[{'codec_type':'video'},{'codec_type':'subtitle','codec_name':'subrip','tags':{'language':'por'}}]}
    native={'tracks':[{'type':'subtitles','properties':{'language':'por','language_ietf':'pt-BR','track_name':'Brazil'}}]}
    with patch('app.movie_import_pipeline.identify',return_value=native),patch.object(workflow,'external_subtitles',return_value=[]):
        actual=workflow._streams(media,metadata)
        assert actual[0]['language']=='pt-BR' and actual[0]['title']=='Brazil'
        refused(422,lambda:workflow._streams(media,{'streams':[]}))
    with patch('app.movie_import_pipeline.identify',return_value={'tracks':[]}),patch.object(workflow,'external_subtitles',return_value=[]):
        refused(422,lambda:workflow._streams(media,metadata))

    for table in ('subtitle_extended_index','subtitle_extended_media'):
        db.execute(f'CREATE TABLE {table}(path TEXT)')

    # Encoding repair shares per-stream consent and signatures but never
    # needs a user rule. Identical decoded text still requires a byte rewrite.
    legacy_srt = '1\n00:00:01,000 --> 00:00:02,000\n<i>Fez café?</i> Não, amanhã.\n'
    originals = [TextSubtitle('embedded',i,'','subrip',legacy_srt if i != 3 else legacy_srt+'\x00',
                             source_encoding='UTF-8' if i == 2 else 'Windows-1252 (inferred)') for i in range(4)]
    with patch.object(workflow,'authorized_import_file',return_value=media), \
         patch.object(workflow,'_available'),patch.object(workflow,'_streams',return_value=streams), \
         patch.object(workflow,'probe',return_value={}), \
         patch.object(workflow.tasks,'media_configuration_signature',return_value=signature) as fingerprint, \
         patch('app.subtitle_cache_worker.text_track_manifest',return_value=('text-signature',manifest)), \
         patch('app.subtitle_cache.get_valid_tracks',return_value=originals), \
         patch('app.subtitle_cache_worker._extract_embedded_batch',return_value={}), \
         patch('app.subtitle_cache_worker._extract_track',side_effect=AssertionError('Cached legacy source re-extracted')), \
         patch.object(workflow.tasks,'queue_paused',return_value=False), \
         patch.object(workflow.tasks,'enqueue',side_effect=enqueue), \
         patch.object(workflow,'connection',side_effect=AssertionError('Encoding repair tried to load a saved rule')):
        request=workflow.EncodingPrepareRequest(path=str(media))
        review=workflow.prepare_encoding(request)
        assert review['repair_kind']=='encoding' and len(review['streams'])==2
        assert review['unchanged']==1 and 'control characters' in review['warnings'][0]
        first,second=review['streams']
        full=workflow.stream_preview(review['review_id'],first['id'])
        assert full['text']==full['original']==legacy_srt and full['normalize_encoding']
        assert full['source_encoding']=='Windows-1252 (inferred)' and full['replacements']==0
        refused(409,lambda:workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='queue')))
        workflow.decision(review['review_id'],workflow.DecisionRequest(stream_id=first['id'],approve=True))
        workflow.decision(review['review_id'],workflow.DecisionRequest(stream_id=second['id'],approve=False))
        workflow.apply_review(review['review_id'],workflow.ApplyRequest(mode='queue'))
        encoding_payload=calls[-1][1]
        assert encoding_payload['repair_kind']=='encoding' and len(encoding_payload['replacements'])==1
        assert encoding_payload['replacements'][0]['normalize_encoding']
        assert encoding_payload['replacements'][0]['text']==legacy_srt
        assert 'UTF-8 quickfix' in calls[-1][2]
        workflow.discard(review['review_id'])
    receipts = {}
    committed_stamp = {'path':str(media),'size':media.stat().st_size,'mtime_ns':media.stat().st_mtime_ns}
    rewrite_result = dict(path=str(media),changed=True,changed_subtitles=1,remuxes=1)
    def record(task_id, step, value): receipts[(task_id,step)] = copy.deepcopy(value)
    with patch.object(workflow,'authorized_import_file',return_value=media),patch.object(workflow,'connection',connection), \
         patch('app.v86.assert_media_editable'),patch('app.job_safety.receipt',side_effect=lambda task,step:receipts.get((task,step))), \
         patch('app.job_safety.record',side_effect=record),patch('app.job_safety.verify_output') as verify, \
         patch('app.job_safety.stamp',return_value=committed_stamp), \
         patch('app.subtitle_cleanup.replace_reviewed_subtitles',return_value=rewrite_result) as write, \
         patch('app.subtitle_cache.invalidate_and_enqueue_media_many') as recache, \
         patch('app.v80.request_media_indexes') as indexes,patch.object(workflow.tasks,'update_progress'), \
         patch.object(workflow.tasks,'media_configuration_signature',return_value=signature) as fingerprint:
        result=workflow.process_autofix(7,payload)
        assert result['remuxes']==1 and write.call_count==1
        assert indexes.call_args.kwargs['detection_scope']=={'subtitle_indices':[0]}, 'Unrelated audio/subtitle analysis triggered'
        assert (7,'autofix') in receipts
        workflow.process_autofix(7,payload)
        assert write.call_count==1 and verify.call_count==1, 'Completion retry repeated committed remux'
        recache.assert_not_called()
        # Power loss after an atomic replacement but before the aggregate
        # receipt: recover that exact file stamp; no second application.
        receipts={(8,'file:'+str(media)):{'after':committed_stamp,'state':'applied'}}
        result=workflow.process_autofix(8,payload)
        assert write.call_count==1 and result['changed']
        recache.assert_called_once_with([str(media)])
        # Queue group inheritance must not let another media edit silently
        # supersede the independent signature of the reviewed approval.
        receipts={}
        fingerprint.return_value={**signature,'digest':'another-edit'}
        refused(409,lambda:workflow.process_autofix(9,payload))
        assert write.call_count==1

print('PASS: full dialogue-only replacement, protected numbers/timing/tags, matching languages, cached reads, per-stream approval/rejection, stale source and paused guard, one durable job, repeat-submit idempotency, lightweight queue payload, cancel/expiry without media changes')
print('PASS: exact approved execution, affected-subtitle-only refresh, committed/partial-receipt recovery without remux repetition, independent reviewed signature guard')
print('PASS: encoding-only cached preview, no saved-rule dependency, unchanged Unicode but approved byte rewrite, corrupt-source refusal and rejected stream exclusion')
