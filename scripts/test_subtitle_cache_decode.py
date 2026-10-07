"""Real extraction fixtures, including legacy bytes, empty tracks and bad SRT."""

import ast
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from app.subtitle_text_decode import decode_complete_srt

track = next(node for node in ast.parse((root / 'app/subtitle_cache.py').read_text()).body
             if isinstance(node, ast.ClassDef) and node.name == 'TextSubtitle')
functions = [node for node in ast.parse((root / 'app/subtitle_cache_worker.py').read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name in {'_extract_track', '_extract_embedded_batch'}]
scope = {'Path': Path, 'subprocess': subprocess, 'tempfile': tempfile, 'dataclass': dataclass,
         'decode_complete_srt': decode_complete_srt, 'MAX_TEXT_BYTES': 32 * 1024**2}
exec(compile(ast.Module(body=[track, *functions], type_ignores=[]), 'cache-decode-fixtures', 'exec'), scope)

valid = '1\n00:00:00,000 --> 00:00:01,000\nNão é uma conversão errada.\n'
for encoding in ('utf-8', 'utf-8-sig', 'utf-16', 'utf-32', 'cp1252'):
    result = decode_complete_srt(valid.encode(encoding))
    assert result.status == 'ready' and result.text == valid, (encoding, result)
for payload in (b'', b'\xef\xbb\xbf', b' \r\n\t'):
    assert decode_complete_srt(payload).status == 'empty'
legacy = '1\n00:00:00,000 --> 00:00:01,000\nÉ lá… Cartão.\n'
assert decode_complete_srt(legacy.encode('cp1252')).text == legacy
# Complete UTF-8 sequences mixed with legacy invalid bytes remain refused.
for character in ('é', '€', '😀'):
    try:
        decode_complete_srt(valid.encode('ascii', errors='replace') + character.encode() + b'\xe9')
    except RuntimeError as exc:
        assert 'mixes UTF-8' in str(exc)
    else:
        raise AssertionError('Complete mixed UTF-8 sequence accepted')
for payload in (b'<html>advertising, not subtitles</html>', valid.encode() + b'\x81',
                valid.encode() + b'\xe9', valid.encode() + b'\x00'):
    try:
        decode_complete_srt(payload)
    except RuntimeError:
        pass
    else:
        raise AssertionError('Invalid/ambiguous subtitle silently decoded')

with tempfile.TemporaryDirectory() as folder:
    directory = Path(folder)
    first, second = directory / 'legacy.srt', directory / 'second.srt'
    first.write_bytes(valid.encode('cp1252'))
    second.write_text(valid, encoding='utf-8')
    media = directory / 'sample.mkv'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(first), '-i', str(second),
                    '-map', '0:s', '-map', '1:s', '-c:s', 'copy', str(media)], check=True)
    tracks = [{'source': 'embedded', 'type_index': i, 'external_path': '', 'codec': 'subrip'} for i in (0, 1)]
    data = scope['_extract_embedded_batch'](media, tracks)
    assert sorted(data) == [0, 1] and data[0].text.strip() == valid.strip() and 'inferred' in data[0].source_encoding, data
    assert scope['_extract_track'](media, tracks[0]).text.strip() == valid.strip()
    empty = directory / 'empty.mkv'
    # An empty subtitle track in a valid container that still has video data.
    later = directory / 'later.srt'
    later.write_text(valid.replace('00:00:00,000 --> 00:00:01,000', '00:00:10,000 --> 00:00:11,000'))
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=32x32:r=1',
                    '-i', str(later), '-map', '0:v', '-map', '1:s', '-c:v', 'mpeg4',
                    '-c:s', 'copy', '-t', '1', str(empty)], check=True)
    assert scope['_extract_track'](empty, tracks[0]).extraction_status == 'empty'
    sidecar = {'source': 'external', 'type_index': -1, 'external_path': str(first), 'codec': 'srt'}
    assert scope['_extract_track'](media, sidecar).text == valid
    first.write_text('this is not an SRT file')
    try:
        scope['_extract_track'](media, sidecar)
    except RuntimeError as exc:
        assert 'manual replacement' in str(exc)
    else:
        raise AssertionError('Invalid external SRT cached')

print('PASS: raw one-pass SRT extraction, strict legacy decoding, verified empty tracks, malformed refusal')
