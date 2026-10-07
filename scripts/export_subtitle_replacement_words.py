"""Export full damaged-report subtitles into per-language U+FFFD dictionaries.

Read-only for media, caches, rules and reports. Only new export files are
written. Valid complete cached text is preferred; missing/stale tracks can be
read completely in one bounded extraction per media, never a 15-minute sample.
The manifest records any failures, changes during reading or decoding loss.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.v2 import probe
from app.v19 import damaged_subtitle_report
from app.subtitle_damage_report import report_tracks, reasons
from app.subtitle_autofix_media import _streams
from app.subtitle_cache import get_valid_tracks
from app.subtitle_cache_worker import MAX_TEXT_BYTES, text_track_manifest
from app.subtitle_replacement_words import replacement_words
from app.job_safety import stamp


def diagnostic_decode(raw: bytes) -> tuple[str, str]:
    """Preserve already-lost glyphs for review; never publish this as a cache."""
    codec = ('utf-32' if raw.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff'))
             else 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
    try:
        return raw.decode(codec, errors='strict'), f'{codec} (strict diagnostic read)'
    except UnicodeError:
        if codec == 'utf-8-sig':
            try:
                from app.subtitle_text_decode import decode_complete_srt
                value = decode_complete_srt(raw)
                return value.text, value.encoding
            except RuntimeError:
                pass
        return raw.decode(codec, errors='replace'), f'{codec} (diagnostic decoding introduced replacement glyphs; manual source review needed)'


def extract_missing(media: Path, tracks: list[dict]) -> dict:
    result = {}
    embedded = [track for track in tracks if track['source'] == 'embedded']
    with tempfile.TemporaryDirectory(prefix='vse-damage-dictionary-') as folder:
        command = ['nice', '-n', '15', 'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-i', str(media)]
        outputs = []
        for track in embedded:
            output = Path(folder) / f"subtitle-{track['type_index']}.srt"
            outputs.append((track, output))
            command += ['-map', f"0:s:{track['type_index']}", '-c:s', 'copy' if track['codec'] in {'srt', 'subrip'} else 'srt', '-f', 'srt', str(output)]
        if embedded:
            completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300, check=False)
            if completed.returncode:
                raise RuntimeError('Complete diagnostic extraction failed; partial outputs refused: ' + completed.stderr.decode('utf-8', errors='replace')[-500:])
        outputs += [(track, Path(track['external_path'])) for track in tracks if track['source'] == 'external']
        for track, output in outputs:
            if not output.is_file() or output.stat().st_size > MAX_TEXT_BYTES:
                raise RuntimeError('Missing subtitle or subtitle exceeds the 32 MiB diagnostic limit')
            if track['source'] == 'external' and track['codec'] not in {'srt', 'subrip'}:
                completed = subprocess.run(['nice', '-n', '15', 'ffmpeg', '-nostdin', '-v', 'error', '-i', str(output), '-map', '0:0', '-f', 'srt', 'pipe:1'], capture_output=True, timeout=300, check=False)
                if completed.returncode or len(completed.stdout) > MAX_TEXT_BYTES:
                    raise RuntimeError('Complete external-subtitle diagnostic extraction failed')
                raw = completed.stdout
            else:
                raw = output.read_bytes()
            result[(track['source'], track['type_index'], track['external_path'])] = diagnostic_decode(raw)
    return result


def export(output: Path, kind='all') -> dict:
    # An exclusive directory avoids overwriting any user-edited dictionary.
    output.mkdir(parents=True, exist_ok=False)
    groups = defaultdict(list)
    for media_kind in (['movies', 'tv'] if kind == 'all' else [kind]):
        for track in report_tracks(damaged_subtitle_report(media_kind)['items']):
            if 'Replacement characters' in reasons(track['damage']):
                groups[track['path']].append({**track, 'kind': media_kind})
    dictionaries = defaultdict(Counter)
    evidence = []
    stats = dict(media=len(groups), reported_streams=sum(map(len, groups.values())), read_streams=0,
                 cached_streams=0, extracted_streams=0, unreproduced_streams=0, failed_streams=0)
    for number, (path, reported) in enumerate(sorted(groups.items()), 1):
        try:
            media = Path(path)
            before = stamp(media)
            data = probe(media)
            signature, manifest = text_track_manifest(media, data)
            metadata = {(s['source'], s['type_index'], s['external_path']): s for s in _streams(media, data)}
            tracks = {(t['source'], t['type_index'], t['external_path']): t for t in manifest}
            cached = {t.key: t for t in get_valid_tracks(path, signature)}
            sidecars = {t['external_path']: stamp(t['external_path']) for t in manifest if t['source'] == 'external'}
            wanted = [(t['source'], t['type_index'], t['external_path']) for t in reported]
            if any(key not in tracks or key not in metadata for key in wanted):
                raise RuntimeError('Reported subtitle identity no longer matches the media')
            raw = extract_missing(media, [tracks[key] for key in wanted if key not in cached])
            readings = []
            for track, key in zip(reported, wanted):
                item = cached.get(key)
                text, encoding = (item.text, item.source_encoding) if item else raw[key]
                # Read the entire track, not a report excerpt or its top 30.
                words = replacement_words(text)
                language = metadata[key]['language'] or 'und'
                if not re.fullmatch(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*', language):
                    language = 'und'
                readings.append((track, key, language, words, text, encoding, item is not None))
            if stamp(media) != before or text_track_manifest(media, data)[0] != signature or any(stamp(p) != identity for p, identity in sidecars.items()):
                raise RuntimeError('Media or sidecar changed during reading; results discarded')
            for track, key, language, words, text, encoding, from_cache in readings:
                dictionaries[language].update(words)
                stats['read_streams'] += 1
                stats['cached_streams' if from_cache else 'extracted_streams'] += 1
                stats['unreproduced_streams'] += not bool(words)
                evidence.append(dict(**track, language=language, status='read' if words else 'finding_not_reproduced',
                                     cached=from_cache, source_encoding=encoding,
                                     text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(), words=dict(words)))
        except Exception as exc:
            stats['failed_streams'] += len(reported)
            evidence.extend(dict(**track, status='failed', error=str(exc)) for track in reported)
        print(json.dumps(dict(processed_media=number, total_media=len(groups), **stats)), flush=True)
    files = []
    for language, words in sorted(dictionaries.items()):
        if not words:
            continue
        name = f'{language}.txt'
        (output / name).write_text(''.join(word + '\n' for word in sorted(words, key=lambda word: (word.casefold(), word))), encoding='utf-8')
        files.append(dict(language=language, file=name, unique_words=len(words), occurrences=sum(words.values())))
    (output / 'references.jsonl').write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in evidence), encoding='utf-8')
    result = dict(created_at=datetime.now().astimezone().isoformat(), scope=kind, **stats,
                  files=files, total_unique_words=sum(item['unique_words'] for item in files),
                  note='UTF-8, one unique damaged word per line, case preserved. No corrections applied. references.jsonl links words to full source streams. Diagnostic decoding loss, if any, is explicitly recorded.')
    (output / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='New export directory; existing directories are never overwritten')
    parser.add_argument('--kind', choices=['all', 'movies', 'tv'], default='all')
    args = parser.parse_args()
    print(json.dumps(export(args.output, args.kind), ensure_ascii=False), flush=True)
