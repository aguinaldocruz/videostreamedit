"""Consented subtitle fixes, followed by one media-scoped commit.

Previews are bounded, short-lived memory, not recovery copies of a movie.
Individual repairs require explicit per-stream review. The Windows-1252 report
shortcut records explicit bulk consent, then validates texts in its worker.
Restarting before submission expires a preview safely; restarting afterwards
retains the job and its exact validated replacement texts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

import app.v65 as tasks
from app.subtitle_autofix import SavedRule, _stored, language_matches, list_rules, normalized_language
from app.subtitle_autofix_text import fix_srt
from app.v2 import app, connection, probe
from app.v28 import authorized_import_file
from app.v5 import external_subtitles, split_tag

logger = logging.getLogger('uvicorn.error')
TASK_TYPE = 'subtitle_autofix'
ENCODING_BULK_OPERATION = 'subtitle_encoding_quickfix'
TTL_SECONDS = 3600
MAX_REVIEW_BYTES = 64 * 1024**2
MAX_REVIEWS = 16
_lock = threading.RLock()
_reviews: dict[str, dict] = {}


class MediaRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=4096)


class EncodingPrepareRequest(MediaRequest):
    operation_id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r'^[0-9a-f]{32}$')


class PrepareRequest(EncodingPrepareRequest):
    rule_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stream_id: str
    approve: StrictBool


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['now', 'queue']


class EncodingReportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['movies', 'tv']


def _expire() -> None:
    now = time.monotonic()
    for key in list(_reviews):
        if _reviews[key]['status'] not in {'preparing', 'submitting'} and now - _reviews[key]['updated'] > TTL_SECONDS:
            del _reviews[key]


def _review(key: str) -> dict:
    _expire()
    result = _reviews.get(key)
    if not result:
        raise HTTPException(410, 'This autofix preview expired or the server restarted. Prepare a new preview; no unsubmitted changes were made.')
    return result


def _available(path: str, *, ignore_task_id: int | None = None) -> None:
    from app.v86 import assert_media_editable
    assert_media_editable(path)
    with connection() as db:
        if db.execute("SELECT 1 FROM media_change_request m JOIN task_queue t ON t.id=m.task_id "
                      "WHERE m.path=? AND t.id!=? AND t.status IN ('pending','running') LIMIT 1", (path, ignore_task_id or 0)).fetchone():
            raise HTTPException(423, 'This media already has pending changes. Wait for completion before preparing or applying autofix.')
        if db.execute("SELECT 1 FROM tv_edit_sessions s JOIN plex_media p ON s.show_id=p.library_key||':'||p.show_title "
                      "WHERE p.path=? AND s.status IN ('open','committing') LIMIT 1", (path,)).fetchone():
            raise HTTPException(423, 'Finish or discard the TV-show edit session before autofixing its real subtitles.')


def _streams(media: Path, data: dict) -> list[dict]:
    # Read native IETF language metadata, not ffprobe's legacy por fallback.
    native = []
    matroska = media.suffix.casefold() in {'.mkv', '.mka', '.mks', '.mk3d', '.webm'}
    if matroska:
        from app.movie_import_pipeline import identify
        native = [track for track in identify(media)['tracks'] if track['type'] == 'subtitles']
    streams = []
    for stream in data.get('streams', []):
        if stream.get('codec_type') != 'subtitle':
            continue
        index = len(streams)
        if matroska and index >= len(native):
            raise HTTPException(422, 'Subtitle track identities disagree; refresh the media before autofix')
        properties = native[index]['properties'] if native else {}
        tags = stream.get('tags') or {}
        language, region = split_tag(properties.get('language_ietf') or tags.get('language_ietf') or properties.get('language') or tags.get('language') or 'und')
        region = region or str(properties.get('tag_language_variant') or '')
        try:
            language = normalized_language(language or 'und', region)
        except ValueError:
            language = 'und'
        streams.append(dict(source='embedded', type_index=index, external_path='', codec=stream.get('codec_name') or '',
                            label=f'Subtitle {index + 1}', language=language,
                            title=properties.get('track_name') or tags.get('title') or ''))
    if matroska and len(streams) != len(native):
        raise HTTPException(422, 'Subtitle track identities disagree; refresh the media before autofix')
    for item in external_subtitles(media):
        path = str(Path(item['path']).resolve())
        try:
            language = normalized_language(item.get('language') or 'und', item.get('region') or '')
        except ValueError:
            language = 'und'
        streams.append(dict(source='external', type_index=-1, external_path=path, codec=Path(path).suffix[1:].casefold(),
                            label='External subtitle · ' + Path(path).name, language=language, title=item.get('title') or ''))
    return streams


def _eligible(rule: SavedRule, stream: dict) -> bool:
    return stream['codec'] in {'subrip', 'srt'} and language_matches(rule, stream['language'])


@app.post('/api/subtitle-autofix/options')
def options(request: MediaRequest) -> dict:
    media = authorized_import_file(request.path)
    _available(str(media))
    streams = _streams(media, probe(media))
    rules = [SavedRule.model_validate(value) for value in list_rules()['rules'] if value['enabled']]
    choices = [dict(id=rule.id, name=rule.name, description=rule.description, languages=rule.languages,
                    matching_streams=sum(_eligible(rule, stream) for stream in streams)) for rule in rules]
    return dict(path=str(media), rules=choices, streams=streams,
                note='SRT/SubRip text only. Image and other subtitle formats are left unchanged. Language matching uses current stream metadata.')


def _progress(key: str, step: int, message: str) -> None:
    with _lock:
        review = _reviews[key]
        review.update(step=step, message=message, updated=time.monotonic())


def _summary(review: dict) -> dict:
    streams = [{key: value for key, value in stream.items() if key not in {'original', 'text', 'before_digest'}}
               for stream in review.get('streams', [])]
    return dict(review_id=review['id'], status=review['status'], rule=review.get('rule_name', ''),
                repair_kind=review.get('repair_kind', 'rule'),
                streams=streams, unchanged=review.get('unchanged', 0), warnings=review.get('warnings', []),
                approved=sum(stream.get('decision') is True for stream in streams),
                reviewed=sum(stream.get('decision') is not None for stream in streams),
                task_id=review.get('task_id'))


@app.get('/api/subtitle-autofix/reviews/{review_id}/progress')
def progress(review_id: str) -> dict:
    with _lock:
        review = _review(review_id)
        return {key: review[key] for key in ('status', 'step', 'total', 'message')}


@app.post('/api/subtitle-autofix/prepare')
def prepare(request: PrepareRequest) -> dict:
    with connection() as db:
        rule = _stored(db, request.rule_id)[0]
    if not rule.enabled:
        raise HTTPException(422, 'This rule is disabled. Enable it in Setup before use.')
    return _prepare_review(request, rule)


@app.post('/api/subtitle-autofix/encoding/prepare')
def prepare_encoding(request: EncodingPrepareRequest) -> dict:
    return _prepare_review(request, None)


def _encoding_report_items(kind: str) -> list[dict]:
    """Select the entire current encoding group, not its three example locations.

    Submission reads cache metadata only. Complete text validation/extraction
    runs under each child media's normal workflow lock, never in the HTTP call.
    """
    from app.v19 import damaged_subtitle_report
    from app.subtitle_damage_report import report_tracks, reasons
    from app.subtitle_cache import CACHE_FORMAT_VERSION
    from app.subtitle_encoding import LEGACY_ENCODING
    selected = [t for t in report_tracks(damaged_subtitle_report(kind)['items'])
                if t['source'] == 'embedded' and t['codec'].casefold() in {'srt', 'subrip'}
                and 'Non-UTF-8 source bytes' in reasons(t['damage'])]
    grouped = {}
    for offset in range(0, len(selected), 64):
        batch = selected[offset:offset + 64]
        labels = {t['path']: t['label'] for t in batch}
        with connection() as db:
            rows = db.execute('''SELECT c.path,c.source,c.type_index,c.external_path
                FROM jsonb_to_recordset(CAST(? AS jsonb)) AS wanted(path text,source text,type_index int,external_path text)
                JOIN subtitle_cache_track c ON (c.path,c.source,c.type_index,c.external_path)=
                    (wanted.path,wanted.source,wanted.type_index,wanted.external_path)
                JOIN subtitle_cache_media m ON m.path=c.path
                WHERE m.format_version=? AND m.expected_tracks=m.cached_tracks
                  AND c.extraction_status='ready' AND c.source_encoding=?
                  AND lower(c.codec) IN ('srt','subrip')
                  AND NOT EXISTS (SELECT 1 FROM subtitle_cache_pending p WHERE p.path=c.path)
                  AND NOT EXISTS (SELECT 1 FROM subtitle_cache_failure f WHERE f.path=c.path)
                ORDER BY c.path,c.type_index''', (json.dumps(batch), CACHE_FORMAT_VERSION, LEGACY_ENCODING)).fetchall()
        for row in rows:
            item = grouped.setdefault(row['path'], dict(path=row['path'], label=labels[row['path']], selected_streams=[]))
            item['selected_streams'].append({key: row[key] for key in ('source', 'type_index', 'external_path')})
    return sorted(grouped.values(), key=lambda item: item['path'])


@app.post('/api/subtitle-autofix/encoding/queue-report')
def queue_encoding_report(request: EncodingReportRequest) -> dict:
    from app.preflight_dispatcher import enqueue_bulk_preflight
    items = _encoding_report_items(request.kind)
    if not items:
        return dict(preflight_id=None, media_count=0, stream_count=0, accepted=False)
    preflight = enqueue_bulk_preflight(ENCODING_BULK_OPERATION, items, mode='queued', priority=70)
    return dict(preflight_id=preflight['id'], media_count=len(items),
                stream_count=sum(len(item['selected_streams']) for item in items), accepted=True)


def _preflight_encoding_report_item(payload: dict, fingerprint: dict) -> dict:
    media = authorized_import_file(payload['path'])
    _available(str(media))
    if not fingerprint.get('exists') or payload.get('_preflight_fingerprint') != fingerprint:
        return dict(decision='stale', reason='Media changed after the bulk quickfix request; no job created')
    return dict(decision='approved', reason='Listed Windows-1252 subtitles selected for guarded UTF-8 normalization',
                signature=tasks.media_configuration_signature(str(media)))


def _approve_encoding_report_items(payload: dict, result: dict) -> dict:
    task_ids = []
    for item in payload.get('_bulk_items') or []:
        signature = item['_preflight_result']['signature']
        selection = item['selected_streams']
        identity = hashlib.sha256(json.dumps([item['path'], signature, selection], sort_keys=True).encode()).hexdigest()
        request = dict(path=item['path'], mode='queue', repair_kind='encoding', approval_mode='bulk_without_review',
                       review_id='encoding-report-' + identity, rule_name='Normalize Windows-1252 to UTF-8',
                       rule_id='', rule_revision=0, selected_streams=selection,
                       reviewed_signature=signature, _media_signature=signature)
        # Execution enriches this same job with verified replacement texts.
        # Stable request identity still deduplicates a replay after that point.
        with connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(?))', (request['review_id'],))
            task = db.execute("SELECT id FROM task_queue WHERE task_type=? AND status IN ('pending','running') "
                              "AND payload_json::jsonb->>'review_id'=? LIMIT 1", (TASK_TYPE, request['review_id'])).fetchone()
            if not task:
                task = tasks.enqueue(TASK_TYPE, request, f"UTF-8 quickfix {item['label']} · {len(selection)} subtitle(s) · bulk, no review", deduplicate=True)
        task_ids.append(task['id'])
    return dict(task_ids=task_ids, queued=len(task_ids), task_type=TASK_TYPE)


def _prepare_review(request: EncodingPrepareRequest, rule: SavedRule | None, *,
                    selected_keys: set[tuple] | None = None, ignore_task_id: int | None = None) -> dict:
    from app.subtitle_cache import get_valid_tracks, publish_track
    from app.subtitle_cache_worker import _extract_embedded_batch, _extract_track, text_track_manifest
    from app.subtitle_encoding import LEGACY_ENCODING, validate_encoding_fix
    encoding_only = rule is None
    name = 'Normalize encoding to UTF-8' if encoding_only else rule.name
    media = authorized_import_file(request.path)
    path = str(media)
    _available(path, ignore_task_id=ignore_task_id)
    with _lock:
        _expire()
        if request.operation_id in _reviews:
            raise HTTPException(409, 'This preview request was already submitted')
        if len(_reviews) >= MAX_REVIEWS:
            raise HTTPException(429, 'Too many open previews. Close an older review and try again.')
        _reviews[request.operation_id] = dict(id=request.operation_id, path=path, status='preparing', updated=time.monotonic(),
                                              step=0, total=4, message='Checking media and subtitle cache', streams=[], bytes=0)
    try:
        before = tasks.media_configuration_signature(path)
        data = probe(media)
        signature, manifest = text_track_manifest(media, data)
        streams = [stream for stream in _streams(media, data)
                   if stream['codec'] in {'srt', 'subrip'} and (encoding_only or _eligible(rule, stream))
                   and (selected_keys is None or (stream['source'], stream['type_index'], stream['external_path']) in selected_keys)]
        if not streams:
            raise HTTPException(422, 'No SRT/SubRip subtitles are available for this repair')
        with _lock:
            _reviews[request.operation_id]['total'] = len(streams) + 3
        metadata = {(t['source'], t['type_index'], t['external_path']): t for t in manifest}
        # Review and commit must use the same strict extraction engine. Old
        # decoded caches can hide invalid bytes or normalize subtitle tags;
        # refresh just these selected inputs before asking for user consent.
        cached = {item.key: item for item in get_valid_tracks(path, signature)
                  if item.extraction_revision >= 2}
        missing = [metadata[(s['source'], s['type_index'], s['external_path'])] for s in streams
                   if s['source'] == 'embedded' and (s['source'], s['type_index'], '') not in cached]
        _progress(request.operation_id, 1, f'Loading {len(streams)} matching subtitles · {len(missing)} embedded extraction(s) needed')
        batch = _extract_embedded_batch(media, missing)
        prepared, extracted, warnings, unchanged, size = [], [], [], 0, 0
        for number, stream in enumerate(streams, 1):
            _progress(request.operation_id, number + 1, f"Preparing {name} · {stream['label']} · {number}/{len(streams)}")
            key = (stream['source'], stream['type_index'], stream['external_path'])
            try:
                item = cached.get(key)
                if item is None and stream['source'] == 'embedded':
                    item = batch.get(stream['type_index'])
                cache_hit = key in cached
                if item is None:
                    item = _extract_track(media, metadata[key])
                if not cache_hit:
                    extracted.append(item)
                if encoding_only:
                    if item.source_encoding != LEGACY_ENCODING:
                        unchanged += 1
                        continue
                    validate_encoding_fix(item.text, item.source_encoding)
                    fixed = dict(changed=True, text=item.text, replacement_count=0)
                else:
                    fixed = fix_srt(rule, item.text)
                if not fixed['changed']:
                    unchanged += 1
                    continue
                size += len(item.text.encode('utf-8')) + len(fixed['text'].encode('utf-8'))
                if size > MAX_REVIEW_BYTES:
                    raise HTTPException(413, 'The full review exceeds the 64 MiB safety limit. Use a more targeted rule.')
                # Reserve while preparing, not only when ready. Concurrent
                # preview requests must share the same bounded memory budget.
                with _lock:
                    used = sum(value['bytes'] for key, value in _reviews.items() if key != request.operation_id)
                    if used + size > MAX_REVIEW_BYTES:
                        raise HTTPException(429, 'Open previews use the review memory budget. Close an older review and try again.')
                    _reviews[request.operation_id]['bytes'] = size
                prepared.append(dict(**stream, id=uuid.uuid4().hex, cached=cache_hit, original=item.text, text=fixed['text'],
                                     before_digest=hashlib.sha256(item.text.encode('utf-8')).hexdigest(),
                                     source_encoding=item.source_encoding, normalize_encoding=encoding_only,
                                     replacements=fixed['replacement_count'], decision=None))
            except HTTPException as exc:
                if exc.status_code in {413, 429}:
                    raise
                warnings.append(f"{stream['label']}: {exc.detail}")
            except (RuntimeError, ValueError) as exc:
                warnings.append(f"{stream['label']}: {exc}")
        _progress(request.operation_id, len(streams) + 2, 'Checking that media and sidecars stayed unchanged during preview')
        _available(path, ignore_task_id=ignore_task_id)
        if tasks.media_configuration_signature(path) != before:
            raise HTTPException(409, 'Media changed during preview. Prepare a new review; no subtitle was replaced.')
        # Publishing original extractions is a cache fill, not applying fixes.
        for item in extracted:
            try:
                publish_track(path, signature, set(metadata), item)
            except Exception:
                logger.warning('subtitle_autofix event=preview_cache_fill_deferred path=%s', path)
                break
        with _lock:
            _expire()
            used = sum(value['bytes'] for key, value in _reviews.items() if key != request.operation_id)
            if used + size > MAX_REVIEW_BYTES:
                raise HTTPException(429, 'Open previews use the review memory budget. Close an older review and try again.')
            review = _reviews[request.operation_id]
            review.update(status='ready', rule_name=name, rule_id=rule.id if rule else '', rule_revision=rule.revision if rule else 0,
                          repair_kind='encoding' if encoding_only else 'rule', streams=prepared,
                          signature=before, unchanged=unchanged, warnings=warnings, bytes=size, updated=time.monotonic(),
                          step=len(streams) + 3, message='Preview ready · original media unchanged')
            return _summary(review)
    except Exception as exc:
        with _lock:
            _reviews[request.operation_id].update(status='failed', bytes=0, streams=[],
                                                 message=str(getattr(exc, 'detail', exc)), updated=time.monotonic())
        raise


@app.get('/api/subtitle-autofix/reviews/{review_id}/streams/{stream_id}')
def stream_preview(review_id: str, stream_id: str) -> dict:
    with _lock:
        review = _review(review_id)
        stream = next((item for item in review['streams'] if item['id'] == stream_id), None)
        if not stream:
            raise HTTPException(404, 'Subtitle preview not found')
        review['updated'] = time.monotonic()
        return {key: value for key, value in stream.items() if key != 'before_digest'}


@app.post('/api/subtitle-autofix/reviews/{review_id}/decision')
def decision(review_id: str, request: DecisionRequest) -> dict:
    with _lock:
        review = _review(review_id)
        if review['status'] != 'ready':
            raise HTTPException(409, 'This review is not available for new approval decisions')
        stream = next((item for item in review['streams'] if item['id'] == request.stream_id), None)
        if not stream:
            raise HTTPException(404, 'Subtitle preview not found')
        stream['decision'] = request.approve
        review['updated'] = time.monotonic()
        return _summary(review)


@app.delete('/api/subtitle-autofix/reviews/{review_id}')
def discard(review_id: str) -> dict:
    with _lock:
        review = _reviews.get(review_id)
        if review and review['status'] in {'preparing', 'submitting'}:
            raise HTTPException(409, 'Wait for the current autofix operation to finish')
        _reviews.pop(review_id, None)
    return dict(discarded=True)


@app.post('/api/subtitle-autofix/reviews/{review_id}/apply')
def apply_review(review_id: str, request: ApplyRequest) -> dict:
    with _lock:
        review = _review(review_id)
        if review['status'] == 'submitted':
            return dict(task_id=review['task_id'], mode=review['mode'], accepted=True)
        if review['status'] != 'ready' or any(stream['decision'] is None for stream in review['streams']):
            raise HTTPException(409, 'Approve or reject every subtitle before choosing Apply now or Queue')
        approved = [stream for stream in review['streams'] if stream['decision'] is True]
        if not approved:
            raise HTTPException(422, 'No subtitles were approved; the original media is unchanged')
        review.update(status='submitting', updated=time.monotonic())
    try:
        _available(review['path'])
        if request.mode == 'now' and tasks.queue_paused():
            raise HTTPException(409, 'The task engine is paused. Resume it or choose Queue; your approvals are still available.')
        if tasks.media_configuration_signature(review['path']) != review['signature']:
            raise HTTPException(409, 'Media changed since preview. Nothing was applied; prepare a new review.')
        payload = dict(path=review['path'], review_id=review_id, rule_name=review['rule_name'], rule_id=review['rule_id'],
                       rule_revision=review['rule_revision'], mode=request.mode,
                       repair_kind=review.get('repair_kind', 'rule'),
                       reviewed_signature=review['signature'],
                       replacements=[{key: stream[key] for key in ('source', 'type_index', 'external_path', 'before_digest', 'text', 'source_encoding', 'normalize_encoding')}
                                     for stream in approved], _media_signature=review['signature'])
        try:
            action = 'UTF-8 quickfix' if review.get('repair_kind') == 'encoding' else 'Autofix'
            task = tasks.enqueue(TASK_TYPE, payload, f"{action} {Path(review['path']).name} · {review['rule_name']} · {len(approved)} subtitles", deduplicate=True)
        except Exception:
            # enqueue commits its owner before workflow registration. If a
            # later registration/response fails, acknowledge that exact owner
            # instead of offering the accepted replacement for submission again.
            with connection() as db:
                task = db.execute("SELECT id FROM task_queue WHERE task_type=? AND payload_json::jsonb->>'review_id'=? ORDER BY id DESC LIMIT 1",
                                  (TASK_TYPE, review_id)).fetchone()
            if not task:
                raise
        with _lock:
            review.update(status='submitted', mode=request.mode, task_id=task['id'], updated=time.monotonic(), bytes=0)
            # The queue owns approved texts now; no second persistent staging
            # area or extra media original is retained for successful work.
            review['streams'] = []
        return dict(task_id=task['id'], mode=request.mode, accepted=True)
    except Exception:
        with _lock:
            review.update(status='ready', updated=time.monotonic())
        raise


def _materialize_bulk_encoding(task_id: int, payload: dict) -> dict | None:
    """Reuse the individual quickfix validator, with the user's bulk consent.

    Prepare one media at a time and persist exact replacement texts before any
    write so existing retry/commit receipts still protect restart recovery.
    No browser preview, additional approval or full-media staging is created.
    """
    selection = {(s['source'], s['type_index'], s['external_path']) for s in payload['selected_streams']}
    request = EncodingPrepareRequest(path=payload['path'])
    tasks.update_progress(task_id, 1, 8, f'Validating {len(selection)} listed Windows-1252 subtitle(s) · no approval dialogs')
    try:
        _prepare_review(request, None, selected_keys=selection, ignore_task_id=task_id)
        with _lock:
            review = _review(request.operation_id)
            if review['signature'] != payload['reviewed_signature']:
                raise HTTPException(409, 'Media changed since the bulk quickfix request; original retained')
            replacements = [{key: stream[key] for key in ('source','type_index','external_path','before_digest','text','source_encoding','normalize_encoding')}
                            for stream in review['streams']]
            warnings = list(review['warnings'])
        if not replacements:
            return dict(changed=False, path=payload['path'], changed_subtitles=0, remuxes=0,
                        skipped_subtitles=len(selection), warnings=warnings,
                        reason='No listed subtitle passed the safe Windows-1252 encoding check; original unchanged')
        payload.update(replacements=replacements, bulk_encoding_warnings=warnings,
                       skipped_subtitles=len(selection)-len(replacements))
        with connection() as db:
            updated = db.execute("UPDATE task_queue SET payload_json=? WHERE id=? AND status='running'",
                                 (json.dumps(payload, ensure_ascii=False), task_id)).rowcount
            if updated != 1:
                raise HTTPException(409, 'The quickfix no longer owns its running job; no media changed')
        return None
    finally:
        discard(request.operation_id)


def process_autofix(task_id: int, payload: dict) -> dict:
    from app.job_safety import receipt, record, stamp, verify_output
    from app.subtitle_cleanup import replace_reviewed_subtitles
    from app.subtitle_cache import invalidate_and_enqueue_media_many
    from app.v80 import request_media_indexes
    from app.v86 import assert_media_editable
    path = str(authorized_import_file(payload['path']))
    action = 'UTF-8 encoding repair' if payload.get('repair_kind') == 'encoding' else 'subtitle autofix'
    assert_media_editable(path)
    saved = receipt(task_id, 'autofix')
    if saved:
        verify_output(saved)
        result = saved['result']
    else:
        if payload.get('approval_mode') == 'bulk_without_review' and not payload.get('replacements'):
            skipped = _materialize_bulk_encoding(task_id, payload)
            if skipped is not None:
                tasks.update_progress(task_id, 8, 8, skipped['reason'])
                return skipped
        # Resume only exact outputs confirmed by the durable rename receipt.
        # Never reapply a rule to an already corrected, committed subtitle.
        pending = []
        resumed = False
        for item in payload['replacements']:
            target = item['external_path'] if item['source'] == 'external' else path
            committed = receipt(task_id, 'file:' + target)
            if committed and committed.get('after') and all(stamp(target).get(k) == value for k, value in committed['after'].items()):
                resumed = True
            else:
                pending.append(item)
        if not resumed and tasks.media_configuration_signature(path) != payload['reviewed_signature']:
            raise HTTPException(409, f'Media changed since approval; {action} refused. Prepare a new preview.')
        result = replace_reviewed_subtitles(Path(path), pending, operation_id=f'task-{task_id}',
                    progress=lambda step, message: tasks.update_progress(task_id, step, 8, message)) if pending else dict(changed=resumed, path=path, changed_subtitles=0, remuxes=0)
        result['changed'] = bool(result['changed'] or resumed)
        result.update(rule=payload['rule_name'], approved_subtitles=len(payload['replacements']))
        if payload.get('approval_mode') == 'bulk_without_review':
            result.update(approval_mode='bulk_without_review', skipped_subtitles=payload.get('skipped_subtitles', 0),
                          warnings=[*result.get('warnings', []), *payload.get('bulk_encoding_warnings', [])])
        if resumed:
            invalidate_and_enqueue_media_many([path])
        record(task_id, 'autofix', dict(result=result, signature=tasks.media_configuration_signature(path)))
    if result['changed']:
        tasks.update_progress(task_id, 7, 8, 'Refreshing subtitle findings · audio detection is unchanged')
        with connection() as db:
            db.execute('DELETE FROM subtitle_extended_index WHERE path=?', (path,))
            db.execute('DELETE FROM subtitle_extended_media WHERE path=?', (path,))
        indices = sorted({item['type_index'] for item in payload['replacements'] if item['source'] == 'embedded'})
        scope = {'subtitle_indices': indices} if indices else {}
        external = [item['external_path'] for item in payload['replacements'] if item['source'] == 'external']
        if external:
            scope['subtitle_external_paths'] = external
        request_media_indexes(path, ['core', 'subtitles'], f'Approved {action}', detection_scope=scope)
    tasks.update_progress(task_id, 8, 8, f'Approved {action} completed')
    logger.info('subtitle_autofix event=completed task=%s path=%s approved=%s remuxes=%s',
                task_id, path, len(payload['replacements']), result.get('remuxes', 0))
    return result


tasks.TASK_HANDLERS[TASK_TYPE] = process_autofix

from app.preflight_dispatcher import register_handler, register_approval_handler
register_handler(ENCODING_BULK_OPERATION, _preflight_encoding_report_item)
register_approval_handler(ENCODING_BULK_OPERATION, _approve_encoding_report_items)
