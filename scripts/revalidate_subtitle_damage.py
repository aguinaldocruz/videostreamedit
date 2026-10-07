"""Recheck old character-related findings from complete subtitle caches only.

Dry-run by default; --apply updates damage labels, never media or cached text.
Uncached/pending/failed media are left to their normal inspection workflow.
--ocr-only limits the refresh to the isolated-letter finding and preserves
every other recorded reason. No extraction, indexing or detection is queued.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.v2 import connection
from app.subtitle_cache import CACHE_FORMAT_VERSION
from app.subtitle_damage_policy import has_mojibake, isolated_letter_stats, OCR_REASON
from app.subtitle_damage_report import reasons
from app.v51 import damage_kind


def run(apply=False, *, ocr_only=False):
    stats = {'checked': 0, 'changed': 0, 'removed_from_damage': 0, 'updated': 0,
             'concurrent_skip': 0, 'invalid_cache_skip': 0, 'ocr_removed': 0}
    selected_reasons = "s.damage LIKE '%Likely OCR gibberish (isolated letters)%'" if ocr_only else (
        "s.damage LIKE '%Possible mojibake%' OR s.damage LIKE '%No recognizable text%' "
        "OR s.damage LIKE '%Likely OCR gibberish (isolated letters)%'")
    cursor = ('', '', -2, '')
    while True:
        with connection() as db:
            db.execute("SET LOCAL statement_timeout='20s'")
            rows = db.execute('''SELECT s.path,s.source,s.type_index,s.external_path,s.damage,
                      t.text_content,t.sha256,t.cached_at,m.indexed_at,c.source_signature
                FROM subtitle_extended_index s
                JOIN subtitle_cache_track t USING(path,source,type_index,external_path)
                JOIN subtitle_cache_media c ON c.path=s.path
                JOIN subtitle_extended_media m ON m.path=s.path
                WHERE (s.path,s.source,s.type_index,s.external_path)>(?,?,?,?)
                  AND (''' + selected_reasons + ''')
                  AND lower(s.codec) IN ('subrip','srt','ass','ssa','webvtt','mov_text','text')
                  AND c.format_version=? AND c.expected_tracks=c.cached_tracks
                  AND t.extraction_status IN ('ready','empty')
                  AND NOT EXISTS (SELECT 1 FROM subtitle_cache_pending p WHERE p.path=s.path)
                  AND NOT EXISTS (SELECT 1 FROM subtitle_cache_failure f WHERE f.path=s.path)
                  AND NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE q.path=s.path AND q.job='subtitles' AND q.status IN ('pending','running'))
                ORDER BY s.path,s.source,s.type_index,s.external_path LIMIT 64''',
                (*cursor, CACHE_FORMAT_VERSION)).fetchall()
        if not rows: break
        updates = []
        for row in rows:
            stats['checked'] += 1
            if hashlib.sha256(row['text_content'].encode('utf-8')).hexdigest() != row['sha256']:
                stats['invalid_cache_skip'] += 1
                continue
            labels = reasons(row['damage'])
            if not ocr_only and 'Possible mojibake' in labels and not has_mojibake(row['text_content']):
                labels.remove('Possible mojibake')
            if not ocr_only and 'No recognizable text' in labels and 'No recognizable text' not in reasons(damage_kind(row['text_content'])):
                labels.remove('No recognizable text')
            if OCR_REASON in labels and not isolated_letter_stats(row['text_content']).suspicious:
                labels.remove(OCR_REASON)
                stats['ocr_removed'] += 1
            updated = ' + '.join(labels) or 'None'
            if updated == row['damage']: continue
            stats['changed'] += 1
            stats['removed_from_damage'] += updated == 'None'
            updates.append((updated, row['path'], row['source'], row['type_index'], row['external_path'], row['damage'],
                            row['sha256'], row['cached_at'], row['indexed_at'], CACHE_FORMAT_VERSION, row['source_signature']))
        if apply and updates:
            # Optimistic guards: do not replace a concurrent edit, cache
            # refresh or inspection result with evidence read earlier.
            with connection() as db:
                db.execute("SET LOCAL statement_timeout='20s'")
                count = db.executemany('''UPDATE subtitle_extended_index s SET damage=?
                        WHERE s.path=? AND s.source=? AND s.type_index=? AND s.external_path=? AND s.damage=?
                          AND EXISTS (SELECT 1 FROM subtitle_cache_track t WHERE
                            (t.path,t.source,t.type_index,t.external_path)=(s.path,s.source,s.type_index,s.external_path)
                            AND t.sha256=? AND t.cached_at=?)
                          AND EXISTS (SELECT 1 FROM subtitle_extended_media m WHERE m.path=s.path AND m.indexed_at=?)
                          AND EXISTS (SELECT 1 FROM subtitle_cache_media c WHERE c.path=s.path AND c.expected_tracks=c.cached_tracks
                            AND c.format_version=? AND c.source_signature=?)
                          AND NOT EXISTS (SELECT 1 FROM subtitle_cache_pending p WHERE p.path=s.path)
                          AND NOT EXISTS (SELECT 1 FROM subtitle_cache_failure f WHERE f.path=s.path)
                          AND NOT EXISTS (SELECT 1 FROM index_task_queue q WHERE q.path=s.path AND q.job='subtitles' AND q.status IN ('pending','running'))''',
                    updates).rowcount
            stats['updated'] += count
            stats['concurrent_skip'] += len(updates) - count
        last = rows[-1]
        cursor = (last['path'], last['source'], last['type_index'], last['external_path'])
        print(json.dumps(stats), flush=True)
    return stats


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--ocr-only', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.apply, ocr_only=args.ocr_only)), flush=True)
