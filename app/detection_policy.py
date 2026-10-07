"""Shared language evidence, comparison and final-media eligibility rules."""
from pathlib import Path

ALIASES = {'eng':'en', 'por':'pt', 'pob':'pt', 'spa':'es', 'fra':'fr', 'fre':'fr',
           'deu':'de', 'ger':'de', 'ita':'it', 'jpn':'ja', 'rus':'ru'}

# One committed-state rule for reports and detection eligibility. A final TV
# show covers all its episodes, including ones without an individual note.
FINAL_MEDIA_QUERY = """SELECT p.path FROM plex_media p WHERE EXISTS (
    SELECT 1 FROM media_notes n WHERE n.final_version=1 AND
    ((p.kind='movie' AND n.entity_type='movie' AND n.entity_key=p.path) OR
     (p.kind='episode' AND n.entity_type='tv' AND
      (n.entity_key='episode:'||p.path OR
       n.entity_key=p.library_key||':'||COALESCE(NULLIF(p.show_title,''),'Unknown show')))))"""


def language_key(language, region=''):
    value = str(language or '').strip().lower().replace('_', '-')
    parts = value.split('-', 1)
    area = str(region or (parts[1] if len(parts) > 1 else '')).upper()
    if parts[0] == 'pob': area = area or 'BR'
    return ALIASES.get(parts[0], parts[0]), area


def language_matches(detected, metadata, region=''):
    left, right = language_key(detected), language_key(metadata, region)
    return bool(left[0] and right[0] not in ('', 'und', 'unknown') and left[0] == right[0]
                and (not left[1] or not right[1] or left[1] == right[1]))


def voice_consensus(samples):
    votes = {}
    for sample in samples:
        code = language_key(sample.get('language'))[0]
        if code not in ('', 'und', 'unknown') and float(sample.get('confidence') or 0) >= .60:
            votes.setdefault(code, []).append(float(sample['confidence']))
    if not votes: return '', 0.0, 0
    code, values = max(votes.items(), key=lambda pair: (len(pair[1]), sum(pair[1])))
    agreement = len(values) / max(1, len(samples))
    confidence = sum(values) / len(values) * agreement
    if len(values) < 2 or agreement < .67 or confidence < .60:
        return '', confidence, len(values)
    return code, confidence, len(values)


def source_stamp(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns


def final_paths(paths=None, db=None):
    if paths is not None and not paths: return set()
    if db is None:
        from app.v11 import connection
        with connection() as db:
            return final_paths(paths, db)
    clause, args = '', []
    if paths is not None:
        args = list(paths)
        clause = ' AND p.path IN (' + ','.join('?' for _ in args) + ')'
    rows = db.execute(FINAL_MEDIA_QUERY + clause, args).fetchall()
    return {str(r['path']) for r in rows}


def is_final(path):
    return bool(final_paths([str(path)]))


def filter_editor_findings(path, streams, detections):
    if is_final(path):
        for stream in streams: stream.pop('audio_detection', None)
        return []
    for stream in streams:
        evidence = stream.get('audio_detection')
        if evidence and (language_matches(evidence.get('detected_language'), stream.get('language'), stream.get('region')) or
                         language_key(evidence.get('metadata_language'))[0] != language_key(stream.get('language'))[0]):
            stream.pop('audio_detection', None)
    result = []
    for item in detections:
        target = next((s for s in streams if s.get('codec_type') == 'subtitle' and not s.get('external') and item.get('source') == 'embedded' and s.get('type_index') == item.get('type_index')), None)
        if item.get('source') == 'embedded' and not target: continue
        if item.get('analysis_status') == 'unsupported': continue
        if target and language_matches(item.get('detected_language'), target.get('language'), target.get('region')) and item.get('analysis_status') != 'no_confidence': continue
        result.append(item)
    return result


def retire_final_detection(db):
    """Cheap set-based retirement; never cancels mixed-purpose media edits."""
    from app.postgres_store import _lock_workflow_mutation
    _lock_workflow_mutation(db.raw)
    final_query = FINAL_MEDIA_QUERY
    db.execute('DELETE FROM deferred_language_detection WHERE path IN (' + final_query + ')')
    retired = list(db.execute("UPDATE task_queue SET status='cancelled',progress_message='Skipped: Final Version',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE status='pending' AND task_type='audio_language_detection' AND payload_json::jsonb->>'path' IN (" + final_query + ') RETURNING group_id').fetchall())
    retired += list(db.execute("UPDATE index_task_queue SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE status='pending' AND job='subtitles' AND path IN (" + final_query + ') RETURNING group_id').fetchall())
    groups = sorted({str(row['group_id']) for row in retired if row['group_id']})
    if not groups:
        return
    group_filter = ','.join('?' for _ in groups)
    # Keep workflow bookkeeping consistent with retired queue entries.
    db.execute("""UPDATE workflow_stages s SET status='cancelled',finished_at=now(),updated_at=now()
        WHERE s.group_id::text IN (""" + group_filter + """) AND s.status='pending' AND (
          (s.task_type='audio_language_detection' AND EXISTS (SELECT 1 FROM task_queue q
           WHERE q.id::text=s.payload->>'task_id' AND q.group_id::uuid=s.group_id AND q.status='cancelled')) OR
          (s.task_type='index:subtitles' AND EXISTS (SELECT 1 FROM index_task_queue q
           WHERE q.id::text=s.payload->>'task_id' AND q.group_id::uuid=s.group_id AND q.status='cancelled')))""", groups)
    db.execute("""UPDATE workflow_groups g SET status='cancelled',finished_at=now(),updated_at=now()
        WHERE g.group_id::text IN (""" + group_filter + """) AND g.status IN ('pending','waiting','running') AND EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status='cancelled')
        AND NOT EXISTS (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status NOT IN ('cancelled','succeeded'))""", groups)


def subtitle_assessment(text, codec, metadata, region, allowed):
    from app.v79 import detect_common_variant, calibrate_subtitle_confidence, subtitle_quality_issue
    from app.v51 import damage_kind
    from app.v79 import _subtitle_metrics
    supported = str(codec or '').lower() in {'', 'subrip','srt','ass','ssa','webvtt','mov_text','text','subtitles'}
    if not supported:
        return '', 0.0, 'Unsupported graphical subtitle; language was not evaluated', 'unsupported'
    metrics = _subtitle_metrics(text)
    detected, confidence, evidence = detect_common_variant(text, allowed)
    confidence = calibrate_subtitle_confidence(confidence, int(metrics.get('cue_count') or 0), int(metrics.get('text_chars') or 0), float(metrics.get('text_coverage') or 0))
    quality = subtitle_quality_issue(text, damage_kind(text))
    if quality: return detected, confidence, quality, 'no_confidence'
    if not detected and language_key(metadata)[0] not in {'', 'und', 'unknown', *allowed}:
        return '', 0.0, 'No common-language evidence; existing metadata retained', 'skipped_non_common'
    if not detected or confidence <= .60:
        analyzed = sorted(set(allowed or ()) & {'en', 'pt'})
        if not analyzed:
            reason = 'No confident result: the local subtitle detector currently has built-in language profiles only for English and Portuguese; configured languages are not deeply analyzed'
        elif evidence:
            reason = evidence
        else:
            reason = 'No confident English or Portuguese vocabulary evidence was found; other configured languages are not deeply analyzed'
        return detected, confidence, reason, 'no_confidence'
    agrees = language_matches(detected, metadata, region)
    return detected, confidence, ('Detected language agrees with metadata' if agrees else evidence or 'Detected language differs from metadata'), ('complete' if agrees else 'mismatch')


def reconcile_stored_findings(db):
    """One-time evidence-only policy refresh. No probes, reads, or queue adds."""
    import json
    changed = {'subtitle': 0, 'audio': 0}
    rows = db.execute("""SELECT d.path,d.source,d.type_index,d.external_path,d.detected_language,d.analysis_status,
        s.language,s.region,s.codec FROM portuguese_language_detection d JOIN media_stream_index s
        ON s.path=d.path AND s.stream_type IN ('subtitle','external') AND s.source=d.source
        AND s.type_index=d.type_index AND COALESCE(s.external_path,'')=COALESCE(d.external_path,'')""").fetchall()
    for row in rows:
        status, reason = None, None
        if row['analysis_status'] == 'mismatch' and language_matches(row['detected_language'], row['language'], row['region']):
            status, reason = 'complete', 'Stored language evidence agrees with current metadata'
        if str(row['codec'] or '').lower() in {'hdmv_pgs_subtitle','dvd_subtitle','dvb_subtitle','pgs','vobsub'}:
            status, reason = 'unsupported', 'Graphical subtitle; text language was not evaluated'
        if status and status != row['analysis_status']:
            db.execute("UPDATE portuguese_language_detection SET analysis_status=?,analysis_reason=? WHERE path=? AND source=? AND type_index=? AND external_path=?", (status,reason,row['path'],row['source'],row['type_index'],row['external_path']))
            changed['subtitle'] += 1
    for row in db.execute('SELECT path,type_index,metadata_language,samples_json FROM audio_language_detection').fetchall():
        try: samples = json.loads(row['samples_json'] or '[]')
        except (TypeError, ValueError): samples = []
        code, confidence, _ = voice_consensus(samples)
        mismatch = bool(code and not language_matches(code,row['metadata_language']))
        db.execute('UPDATE audio_language_detection SET detected_language=?,confidence=?,mismatch=? WHERE path=? AND type_index=?', (code,confidence,int(mismatch),row['path'],row['type_index']))
        changed['audio'] += 1
    retire_final_detection(db)
    return changed
