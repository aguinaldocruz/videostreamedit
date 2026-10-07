"""Real cleanup fixtures, isolated SQLite cache, no production media/DB writes."""
import ast
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.subtitle_html import has_removable_html, strip_non_color_html
from app.subtitle_text_decode import decode_complete_srt


def load(path, names, scope):
    nodes = [node for node in ast.parse((ROOT / path).read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
             or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)]
    for node in nodes:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), scope)


def module(name, **members):
    value = types.ModuleType(name)
    value.__dict__.update(members)
    sys.modules[name] = value
    return value


class HttpError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def probe(path):
    value = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                           capture_output=True, timeout=120, check=True)
    return json.loads(value.stdout)


def external_subtitles(media):
    return [dict(path=str(path.resolve())) for path in sorted(media.parent.iterdir())
            if path.suffix in {'.srt', '.ass', '.ssa', '.vtt'} and path.stem.startswith(media.stem + '.')]


def checked_external(media, path):
    value = Path(path).resolve()
    assert str(value) in {item['path'] for item in external_subtitles(media)}
    return value


db = sqlite3.connect(':memory:')
db.row_factory = sqlite3.Row
db.execute('PRAGMA foreign_keys=ON')


@contextmanager
def connection():
    with db:
        yield db


scope = dict(Path=Path, subprocess=subprocess, json=json, re=re, os=os, copy=copy, tempfile=tempfile,
             hashlib=hashlib, dataclass=dataclass, ET=ET, connect=connection, probe=probe,
             external_subtitles=external_subtitles, decode_complete_srt=decode_complete_srt,
             CACHE_FORMAT_VERSION=1, IMAGE_CACHE_FORMAT_VERSION=1, HTTPException=HttpError,
             logger=logging.getLogger(__name__), MAX_TEXT_BYTES=32 * 1024**2,
             TEXT_CODECS={'srt', 'subrip', 'ass', 'ssa', 'webvtt', 'mov_text', 'text'},
             TEXT_SIDECAR_SUFFIXES={'.srt', '.ass', '.ssa', '.vtt'}, IMAGE_CODECS={'hdmv_pgs_subtitle': 'pgs', 'dvd_subtitle': 'vobsub'})
cache_names = {'TextSubtitle', 'ensure_subtitle_cache_schema', 'publish_media', 'publish_track', 'get_valid_media', 'get_valid_tracks',
               'get_valid_track', 'publish_replacement_cache', 'pending_revision', 'complete_pending_media', 'enqueue_media',
               'enqueue_media_many', 'invalidate_and_enqueue_media_many', 'cache_records_present', 'reconcile_source_cache'}
load('app/subtitle_cache.py', cache_names, scope)
load('app/job_safety.py', {'stamp'}, scope)
load('app/subtitle_cache_worker.py', {'_file_identity', 'text_track_manifest', 'image_track_manifest', 'reconcile_cache_after_index', '_extract_track', '_extract_embedded_batch'}, scope)
load('app/v51.py', {'decode_external', 'subtitle_payload_lines', 'validate_cleaned_srt', 'SRT_TIMING_LINE'}, scope)
scope['has_removable_html'] = has_removable_html
load('app/matroska_layout.py', {'_vint', '_element', 'inspect_layout', 'MATROSKA_SUFFIXES', 'EBML_HEADER', 'SEGMENT', 'TRACKS', 'CLUSTER', 'MAX_TOP_LEVEL_ELEMENTS', 'MAX_HEADER_BYTES'}, scope)
load('app/matroska_remux.py', {'_semantic_snapshot', '_TRACK_PROPERTIES', '_GENERATED_TAGS', '_subtitle_packets', '_verify_remux_warning'}, scope)
scope['ensure_subtitle_cache_schema']()
TextSubtitle = scope['TextSubtitle']
stamp = scope['stamp']
commands, extractions, publications, checkpoints = [], [], [], []


@contextmanager
def output_space(_path, _required):
    yield


def run_write_command(command, _directory, timeout=3600, accepted_returncodes=(0,), **_kw):
    commands.append(command)
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode not in accepted_returncodes:
        raise RuntimeError((result.stdout + result.stderr).decode(errors='replace')[-4000:])
    return dict(returncode=result.returncode, output=(result.stdout + result.stderr).decode(errors='replace'), truncated=False)


def replace_prepared(source, target, before):
    assert stamp(target) == before
    os.replace(source, target)
    return stamp(target)


def ensure_headers(path):
    assert scope['inspect_layout'](path)[0] == 'ok', 'Writer relocated track headers; another remux would be required'


real_extract = scope['_extract_track']
real_batch = scope['_extract_embedded_batch']
def extract(path, track):
    extractions.append(('single', path, [track['type_index']]))
    return real_extract(path, track)
def batch(path, tracks):
    if len(tracks) > 1:
        extractions.append(('batch', path, [track['type_index'] for track in tracks]))
    return real_batch(path, tracks)
real_publish = scope['publish_replacement_cache']
def publish(*args, **kw):
    publications.append((args, kw))
    return real_publish(*args, **kw)

module('fastapi', HTTPException=HttpError)
module('app.v2', probe=probe, DATA_DIR=Path('/tmp'), make_language=lambda language, region: language + ('-' + region if region else ''))
module('app.v5', checked_external=checked_external, external_subtitles=external_subtitles,
       split_tag=lambda value: (value, ''), plex_language_pair=lambda language, region: (language, region))
module('app.v51', decode_external=scope['decode_external'], validate_cleaned_srt=scope['validate_cleaned_srt'])
module('app.matroska_remux', _semantic_snapshot=scope['_semantic_snapshot'], _TRACK_PROPERTIES=scope['_TRACK_PROPERTIES'],
       ensure_front_track_headers=ensure_headers, _verify_remux_warning=scope['_verify_remux_warning'], _subtitle_packets=scope['_subtitle_packets'])
module('app.matroska_layout', safe_checkpoint=lambda path, **kw: checkpoints.append(path))
module('app.job_safety', stamp=stamp, replace_prepared=replace_prepared, output_space=output_space, run_write_command=run_write_command,
       copy_recovery=shutil.copyfile)
module('app.subtitle_cache', **({name: scope[name] for name in cache_names if name != 'ensure_subtitle_cache_schema'} | {'publish_replacement_cache': publish}))
module('app.subtitle_cache_worker', _extract_track=extract, _extract_embedded_batch=batch,
       text_track_manifest=scope['text_track_manifest'], image_track_manifest=scope['image_track_manifest'],
       reconcile_cache_after_index=scope['reconcile_cache_after_index'])
from app import movie_import_pipeline as pipeline, subtitle_cleanup as cleanup


with tempfile.TemporaryDirectory(prefix='vse-cleanup-fixtures-') as directory:
    root = Path(directory)
    srt = '1\n00:00:00,000 --> 00:00:01,000\n<b>Olá</b><br/> <i><u>mundo</u></i> <font color="red">hoje</font></br>\n\n2\n00:00:02,000 --> 00:00:03,000\n42\n'
    first, second, third = (root / name for name in ('first.srt', 'second.srt', 'third.srt'))
    first.write_text(srt)
    second.write_text(srt.replace('Olá', 'Outro'))
    third.write_text(srt.replace('Olá', 'Unchanged'))
    original = root / 'original.mkv'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=64x48:r=4',
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-f', 'lavfi', '-i', 'sine=frequency=880',
                    '-i', str(first), '-i', str(second), '-i', str(third),
                    '-map', '0:v', '-map', '3:s', '-map', '1:a', '-map', '4:s', '-map', '2:a', '-map', '5:s',
                    '-t', '4', '-c:v', 'mpeg4', '-c:a', 'aac', '-c:s', 'copy', str(original)], check=True)
    subprocess.run(['mkvpropedit', str(original), '--edit', 'track:a1', '--set', 'language=eng',
                    '--edit', 'track:a2', '--set', 'language=por', '--set', 'language-ietf=pt-BR',
                    '--edit', 'track:s1', '--set', 'language=por', '--set', 'language-ietf=pt-BR', '--set', 'name=Original',
                    '--set', 'flag-forced=1', '--set', 'flag-hearing-impaired=1',
                    '--edit', 'track:s2', '--set', 'language=por', '--set', 'language-ietf=pt-PT',
                    '--set', 'flag-default=0', '--edit', 'track:s3', '--set', 'language=eng'], capture_output=True, check=True)
    tags, chapters, attachment = root / 'tags.xml', root / 'chapters.xml', root / 'notice.txt'
    uid = next(track for track in pipeline.identify(original)['tracks'] if track['type'] == 'subtitles')['properties']['uid']
    tags.write_text(f'<Tags><Tag><Targets/><Simple><Name>COMMENT</Name><String>Global note</String></Simple></Tag>'
                    f'<Tag><Targets><TrackUID>{uid}</TrackUID></Targets><Simple><Name>COMMENT</Name><String>Subtitle note</String></Simple></Tag></Tags>')
    chapters.write_text('<?xml version="1.0" encoding="UTF-8"?><Chapters><EditionEntry><ChapterAtom><ChapterTimeStart>00:00:00.000</ChapterTimeStart>'
                        '<ChapterTimeEnd>00:00:04.000</ChapterTimeEnd><ChapterDisplay><ChapterString>Opening</ChapterString>'
                        '<ChapterLanguage>eng</ChapterLanguage></ChapterDisplay></ChapterAtom></EditionEntry></Chapters>')
    attachment.write_text('Keep this attachment')
    enriched = root / 'enriched.mkv'
    result = subprocess.run(['mkvmerge', '--quiet', '-o', str(enriched), '--global-tags', str(tags), '--chapters', str(chapters),
                             '--attach-file', str(attachment), '--track-order', '0:0,0:1,0:2,0:3,0:4,0:5', str(original)], capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    os.replace(enriched, original)

    def fixture(name, cached_indexes=(0, 1, 2)):
        media = root / f'{name}.mkv'
        shutil.copyfile(original, media)
        data = probe(media)
        signature, tracks = scope['text_track_manifest'](media, data)
        for track in tracks:
            if track['type_index'] in cached_indexes:
                scope['publish_track'](str(media), signature, {('embedded', i, '') for i in (0, 1, 2)}, real_extract(media, track))
        return media

    media = fixture('cached')
    before_semantics = scope['_semantic_snapshot'](media)
    before_cache = {track.type_index: track for track in scope['get_valid_tracks'](str(media), scope['text_track_manifest'](media)[0])}
    progress = []
    command_start, extraction_start, publication_start = len(commands), len(extractions), len(publications)
    result = cleanup.clean_subtitles(media, [{'type_index': 0}, {'type_index': 1}, {'type_index': 0}],
                                    operation_id='fixture', progress=lambda step, message: progress.append((step, message)))
    assert result['changed_subtitles'] == 2 and result['remuxes'] == 1 and result['cached_inputs'] == 2 and result['extracted_inputs'] == 0, result
    assert sum(command[0] in {'ffmpeg', 'mkvmerge'} for command in commands[command_start:]) == 1
    assert all(path != media for _kind, path, _tracks in extractions[extraction_start:]), 'Source subtitles extracted despite a valid cache'
    assert len(publications) == publication_start + 1
    assert scope['_semantic_snapshot'](media) == before_semantics
    after = scope['get_valid_media'](str(media), scope['text_track_manifest'](media)[0])
    assert len(after) == 3 and after[2].text == before_cache[2].text
    for track in after[:2]:
        assert not has_removable_html(track.text)
        assert all(value in track.text for value in ('<i>', '</i>', '<u>', '</u>', '<br/>', '</br>', '<font color="red">', '</font>'))
        cleanup.validate_srt(strip_non_color_html(before_cache[track.type_index].text), track.text)
    assert [step for step, _message in progress] == list(range(7))
    assert scope['reconcile_cache_after_index'](media)
    assert scope['get_valid_media'](str(media), scope['text_track_manifest'](media)[0]) == after
    assert scope['pending_revision'](str(media)) is None

    # FFmpeg-created Matroska can start before zero (AAC encoder delay).
    # Cache export rebases SRT times; native replacements must put that origin
    # back, and unchanged cache verification must account for output origin.
    negative = root / 'negative-start.mkv'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=64x48:r=4',
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-i', str(first), '-i', str(second),
                    '-map', '0:v', '-map', '1:a', '-map', '2:s', '-map', '3:s', '-t', '4',
                    '-c:v', 'mpeg4', '-c:a', 'aac', '-c:s', 'copy', '-avoid_negative_ts', 'disabled', str(negative)], check=True)
    negative_data = probe(negative)
    assert float(negative_data['format']['start_time']) < 0, negative_data['format']
    signature, tracks = scope['text_track_manifest'](negative, negative_data)
    scope['publish_media'](str(negative), signature, {('embedded', n, '') for n in (0, 1)}, [real_extract(negative, t) for t in tracks])
    physical_before = scope['_subtitle_packets'](negative)
    verify_negative=pipeline._verify
    def check_negative(plan, output):
        try:
            return verify_negative(plan,output)
        except RuntimeError:
            print('Negative timeline diagnostics', json.dumps({
                'before': negative_data['format'].get('start_time'), 'after': probe(output)['format'].get('start_time'),
                'before_packets': physical_before, 'after_packets':scope['_subtitle_packets'](output),
                'before_text':[real_extract(negative,t).text for t in tracks],
                'after_text':[real_extract(output,t).text for t in tracks],
                'before_tracks':pipeline.identify(negative)['tracks'],
                'after_tracks':pipeline.identify(output)['tracks']}))
            raise
    with patch.object(pipeline,'_verify',side_effect=check_negative):
        result = cleanup.clean_subtitles(negative, [{'type_index': 0}])
    physical_after = scope['_subtitle_packets'](negative)
    # Selected dialogue changes, but packet timestamps and the untouched
    # subtitle's bytes/timestamps must remain exact, not merely "close".
    # After commit minimum timestamps are zero; the captured original origin
    # is the source shift in this synthetic negative-block fixture.
    shift = -round(float(negative_data['format']['start_time'])*1000)
    assert sorted((p[0],p[2]+shift,p[3]) for p in physical_before) == sorted((p[0],p[2],p[3]) for p in physical_after)
    untouched = tracks[1]['type_index']
    assert sorted((p[0],p[1],p[2]+shift,p[3]) for p in physical_before if p[0]==3) == [p for p in physical_after if p[0]==3]
    assert result['remuxes']==1 and len(scope['get_valid_media'](str(negative),scope['text_track_manifest'](negative)[0]))==2

    # Real-world AAC containers may have nonnegative physical blocks plus a
    # CodecDelay header. mkvmerge can drop that header even in passthrough mode.
    # Preserve it, rather than accepting an audio-only timestamp displacement.
    delayed = root / 'codec-delay.mkv'
    shutil.copyfile(original, delayed)
    subprocess.run(['mkvpropedit',str(delayed),'--edit','track:a1','--set','codec-delay=23000000'],
                   capture_output=True,check=True)
    delay_data=probe(delayed)
    delay_native=pipeline.identify(delayed)
    assert any(track['properties'].get('codec_delay')==23000000 for track in delay_native['tracks'])
    delay_semantics=scope['_semantic_snapshot'](delayed)
    delay_packets=scope['_subtitle_packets'](delayed)
    signature,tracks=scope['text_track_manifest'](delayed,delay_data)
    scope['publish_media'](str(delayed),signature,{('embedded',n,'') for n in (0,1,2)},[real_extract(delayed,t) for t in tracks])
    result=cleanup.clean_subtitles(delayed,[{'type_index':0}])
    assert result['remuxes']==1
    assert scope['_semantic_snapshot'](delayed)==delay_semantics
    assert sorted((p[0],p[2],p[3]) for p in scope['_subtitle_packets'](delayed)) == sorted((p[0],p[2],p[3]) for p in delay_packets)
    assert [s.get('start_time') for s in probe(delayed)['streams'] if s.get('codec_type') in ('audio','video')] == [s.get('start_time') for s in delay_data['streams'] if s.get('codec_type') in ('audio','video')]

    # Matroska cover pictures are attachments even though FFprobe presents
    # them as additional video streams. Their exact attachment survives.
    cover = root / 'cover.mkv'
    subprocess.run(['mkvmerge','-q','-o',str(cover),'--attachment-name','cover.png',
                    '--attachment-mime-type','image/png','--attach-file',str(ROOT/'app/static/brand/favicon-16.png'),
                    str(original)], check=True)
    assert any((s.get('disposition') or {}).get('attached_pic') for s in probe(cover)['streams'])
    signature, tracks=scope['text_track_manifest'](cover)
    scope['publish_media'](str(cover),signature,{('embedded',n,'') for n in (0,1,2)},[real_extract(cover,t) for t in tracks])
    semantic_before=scope['_semantic_snapshot'](cover)
    cleanup.clean_subtitles(cover,[{'type_index':0}])
    assert scope['_semantic_snapshot'](cover)==semantic_before

    # A legacy cache representation of an untouched stream is not proof of
    # its actual packet contents. A mismatch may fall back only to exact raw
    # bytes/timestamp preservation, dropping that cache rather than blessing it.
    stale = fixture('stale-untouched-cache')
    signature, tracks=scope['text_track_manifest'](stale)
    scope['publish_track'](str(stale),signature,{('embedded',n,'') for n in (0,1,2)},
                           TextSubtitle('embedded',2,'','subrip',srt.replace('Olá','Old decoder normalized text')))
    old_packets=scope['_subtitle_packets'](stale)
    cleanup.clean_subtitles(stale,[{'type_index':0}])
    assert [p for p in old_packets if p[0]==5]==[p for p in scope['_subtitle_packets'](stale) if p[0]==5]
    assert len(scope['get_valid_tracks'](str(stale),scope['text_track_manifest'](stale)[0]))==2
    assert scope['pending_revision'](str(stale)) is not None

    # Obsolete cached renderer output is refreshed only for a mutation target.
    # It must never overwrite real dialogue that the old decoder had omitted.
    legacy_cached = fixture('legacy-cached-target')
    signature, tracks=scope['text_track_manifest'](legacy_cached)
    scope['publish_track'](str(legacy_cached),signature,{('embedded',n,'') for n in (0,1,2)},
                           TextSubtitle('embedded',0,'','subrip',srt.replace('Olá','WRONG OLD TEXT'),extraction_revision=0))
    result=cleanup.clean_subtitles(legacy_cached,[{'type_index':0}])
    refreshed=scope['get_valid_tracks'](str(legacy_cached),scope['text_track_manifest'](legacy_cached)[0])[0]
    assert 'Olá' in refreshed.text and 'WRONG OLD TEXT' not in refreshed.text
    assert result['extracted_inputs']==1 and result['cached_inputs']==0 and refreshed.extraction_revision==2

    from app.subtitle_timing import shift_srt
    try:
        shift_srt(srt, -21)
    except RuntimeError:
        pass
    else:
        raise AssertionError('Negative-start cue would be silently dropped')

    unchanged = media.read_bytes()
    command_start = len(commands)
    result = cleanup.clean_subtitles(media, [{'type_index': 0}, {'type_index': 1}], operation_id='fixture')
    assert not result['changed'] and not result['remuxes'] and media.read_bytes() == unchanged and len(commands) == command_start

    # Two missing source tracks: one source extraction pass, one output
    # verification pass, one remux. A genuinely partial cache stays partial.
    media = fixture('missing', ())
    extraction_start = len(extractions)
    result = cleanup.clean_subtitles(media, [{'type_index': 0}, {'type_index': 1}], operation_id='fixture')
    original_reads = [item for item in extractions[extraction_start:] if item[1] == media]
    assert original_reads == [('batch', media, [0, 1])], original_reads
    assert result['extracted_inputs'] == 2 and result['cached_inputs'] == 0 and result['remuxes'] == 1
    assert scope['get_valid_media'](str(media), scope['text_track_manifest'](media)[0]) is None
    assert len(scope['get_valid_tracks'](str(media), scope['text_track_manifest'](media)[0])) == 2
    assert scope['pending_revision'](str(media)) is not None
    revision = scope['pending_revision'](str(media))
    assert not scope['reconcile_cache_after_index'](media)
    assert len(scope['get_valid_tracks'](str(media), scope['text_track_manifest'](media)[0])) == 2
    assert scope['pending_revision'](str(media)) == revision

    # External files (including BOM/legacy encoding) need no container write.
    media = fixture('external')
    sidecar = media.with_suffix('.pt.srt')
    sidecar.write_bytes(b'\xef\xbb\xbf' + srt.encode())
    signature, tracks = scope['text_track_manifest'](media)
    scope['publish_media'](str(media), signature, {(t['source'], t['type_index'], t['external_path']) for t in tracks}, [real_extract(media, t) for t in tracks])
    command_start = len(commands)
    original_bytes = media.read_bytes()
    result = cleanup.clean_subtitles(media, [{'external_path': str(sidecar)}], operation_id='external')
    assert result['changed_subtitles'] == 1 and result['remuxes'] == 0 and len(commands) == command_start
    assert media.read_bytes() == original_bytes and sidecar.read_bytes().startswith(b'\xef\xbb\xbf')
    assert not has_removable_html(sidecar.read_text(encoding='utf-8-sig'))
    assert len(scope['get_valid_media'](str(media), scope['text_track_manifest'](media)[0])) == 4
    assert scope['reconcile_cache_after_index'](media)

    # Mixed embedded and sidecar changes are prepared together. External
    # native formats remain native and are normalized only for their cache.
    media = fixture('mixed')
    vtt = media.with_suffix('.en.vtt')
    vtt.write_text('WEBVTT\n\n00:00.000 --> 00:01.000\n<b>Hello</b> <i>world</i>\n')
    cp1252 = media.with_suffix('.pt.srt')
    cp1252.write_bytes(srt.encode('cp1252'))
    signature, tracks = scope['text_track_manifest'](media)
    scope['publish_media'](str(media), signature, {(t['source'], t['type_index'], t['external_path']) for t in tracks}, [real_extract(media, t) for t in tracks])
    result = cleanup.clean_subtitles(media, [{'type_index': 0}, {'type_index': 1}, {'external_path': str(vtt)}, {'external_path': str(cp1252)}])
    assert result['remuxes'] == 1 and result['changed_subtitles'] == 4, result
    assert vtt.read_text().startswith('WEBVTT') and '<b>' not in vtt.read_text() and '<i>' in vtt.read_text()
    assert cp1252.read_bytes() == strip_non_color_html(srt).encode('cp1252')
    assert len(scope['get_valid_media'](str(media), scope['text_track_manifest'](media)[0])) == 5

    # An unrelated legacy-encoded subtitle can produce mkvmerge's known UTF-8
    # warning. It must pass through byte-for-byte; selected texts are normalized
    # and validated fully. Unknown warnings are never accepted.
    third.write_bytes(srt.replace('Olá', 'Ação').encode('cp1252'))
    legacy_source = root / 'legacy-source.mkv'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(original), '-i', str(third),
                    '-map', '0:v', '-map', '0:s:0', '-map', '0:a:0', '-map', '0:s:1', '-map', '0:a:1', '-map', '1:s',
                    '-c', 'copy', str(legacy_source)], check=True)
    signature, tracks = scope['text_track_manifest'](legacy_source)
    scope['publish_media'](str(legacy_source), signature, {(t['source'], t['type_index'], t['external_path']) for t in tracks}, [real_extract(legacy_source, t) for t in tracks])
    packets_before = scope['_subtitle_packets'](legacy_source)
    def known_warning(*args, **kwargs):
        result = run_write_command(*args, **kwargs)
        # Some mkvmerge versions emit this only for particular invalid bytes.
        # Inject its exit status, but verify against real unmodified packets.
        return {**result, 'returncode': 1, 'output': 'Warning: This text subtitle track contains invalid 8-bit characters outside valid multi-byte UTF-8 sequences.'}
    with patch.object(cleanup, 'run_write_command', side_effect=known_warning):
        result = cleanup.clean_subtitles(legacy_source, [{'type_index': 0}, {'type_index': 1}])
    assert result['remuxes'] == 1 and result['warnings'], result
    unselected = next(s['index'] for i, s in enumerate([s for s in probe(legacy_source)['streams'] if s['codec_type'] == 'subtitle']) if i == 2)
    assert [p for p in packets_before if p[0] == unselected] == [p for p in scope['_subtitle_packets'](legacy_source) if p[0] == unselected]

    # Prepare/verify failures, disk admission, invalid targets and source races
    # all leave the original container unchanged and no temporary media behind.
    for failure in ('verification', 'warning', 'disk', 'target', 'race', 'cache'):
        media = fixture(failure)
        before = media.read_bytes()
        selections = [{'type_index': 0}, {'type_index': 1}]
        if failure == 'verification':
            context = patch.object(pipeline, '_verify', side_effect=RuntimeError('Injected verification failure'))
        elif failure == 'warning':
            def unknown_warning(*args, **kwargs):
                return {**run_write_command(*args, **kwargs), 'returncode': 1, 'output': 'Warning: Unknown corruption'}
            context = patch.object(cleanup, 'run_write_command', side_effect=unknown_warning)
        elif failure == 'disk':
            @contextmanager
            def no_space(*_args):
                raise RuntimeError('Insufficient disk space')
                yield
            context = patch.object(cleanup, 'output_space', no_space)
        elif failure == 'target':
            selections.append({'type_index': 99})
            context = patch.object(cleanup, 'logger')
        elif failure == 'race':
            original_verify = pipeline._verify
            def raced(plan, output):
                original_verify(plan, output)
                os.utime(media, ns=(media.stat().st_atime_ns, media.stat().st_mtime_ns + 10000000))
            context = patch.object(pipeline, '_verify', side_effect=raced)
        else:
            context = patch.object(sys.modules['app.subtitle_cache'], 'publish_replacement_cache', side_effect=RuntimeError('Injected database failure'))
        with context:
            if failure == 'cache':
                result = cleanup.clean_subtitles(media, selections, operation_id='fixture')
                assert result['changed'] and not result['cache_published']
                assert scope['pending_revision'](str(media)) is not None
            else:
                try:
                    cleanup.clean_subtitles(media, selections, operation_id='fixture')
                except (RuntimeError, HttpError):
                    pass
                else:
                    raise AssertionError(f'{failure} did not prevent replacement')
                assert media.read_bytes() == before
        assert not list(root.glob(f'.{media.stem}.subtitle-clean.vse-*'))
    # Lost/changed dialogue or timestamps cannot be hidden by cue renumbering.
    for invalid in (srt.replace('00:00:02,000', '00:00:02,100'), srt.replace('42', '43'), srt.replace('Outro', '').replace('Olá', '')):
        try:
            cleanup.validate_srt(srt, invalid)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Damaged subtitle passed timing/dialogue verification')
    assert not list(root.glob('.*.vse-*.tmp'))

    # Autofix uses this same writer, with exact user-approved texts/digests,
    # not a fresh application of a rule. Test real remux and metadata/cache.
    replacement_scope = dict(scope, TIMING=cleanup.TIMING, validate_srt=cleanup.validate_srt)
    load('app/subtitle_autofix_text.py', {'MAX_SUBTITLE_BYTES', '_timings', 'validate_replacement'}, replacement_scope)
    module('app.subtitle_autofix_text', validate_replacement=replacement_scope['validate_replacement'])
    media = fixture('autofix')
    before_semantics = scope['_semantic_snapshot'](media)
    original_cached = {track.type_index: track for track in scope['get_valid_tracks'](str(media), scope['text_track_manifest'](media)[0])}
    fixes = [dict(source='embedded', type_index=i, external_path='',
                  before_digest=hashlib.sha256(original_cached[i].text.encode()).hexdigest(),
                  text=original_cached[i].text.replace('Olá', 'Café').replace('Outro', 'Ação')) for i in (0, 1)]
    command_start, extraction_start = len(commands), len(extractions)
    result = cleanup.replace_reviewed_subtitles(media, fixes, operation_id='autofix-fixture')
    assert result['changed_subtitles'] == 2 and result['remuxes'] == 1 and result['cached_inputs'] == 2
    assert sum(cmd[0] in {'ffmpeg', 'mkvmerge'} for cmd in commands[command_start:]) == 1
    assert all(path != media for _kind, path, _tracks in extractions[extraction_start:])
    assert scope['_semantic_snapshot'](media) == before_semantics
    after = scope['get_valid_media'](str(media), scope['text_track_manifest'](media)[0])
    assert after[2].text == original_cached[2].text, 'Rejected subtitle was modified'
    for i in (0, 1):
        cleanup.validate_srt(fixes[i]['text'], after[i].text)
    assert not list(root.glob(f'.{media.stem}.subtitle-clean.vse-*'))

    # Mixed embedded/external autofix commits share the same preparation,
    # preserve sidecar filenames/BOM, and publish every verified cache track.
    media = fixture('autofix-external')
    sidecar = media.with_suffix('.pt.srt')
    sidecar.write_bytes(b'\xef\xbb\xbf' + srt.replace('Olá', 'Café').encode())
    signature, tracks = scope['text_track_manifest'](media)
    expected = {(t['source'], t['type_index'], t['external_path']) for t in tracks}
    scope['publish_media'](str(media), signature, expected, [real_extract(media, t) for t in tracks])
    cached = scope['get_valid_tracks'](str(media), signature)
    fixes = [dict(source=t.source, type_index=t.type_index, external_path=t.external_path,
                  before_digest=hashlib.sha256(t.text.encode()).hexdigest(),
                  text=t.text.replace('Olá', 'Ação').replace('Café', 'Chá'))
             for t in cached if t.source == 'external' or t.type_index == 0]
    original_semantics = scope['_semantic_snapshot'](media)
    command_start = len(commands)
    result = cleanup.replace_reviewed_subtitles(media, fixes)
    assert result['changed_subtitles']==2 and result['remuxes']==1
    assert sum(cmd[0] in {'ffmpeg','mkvmerge'} for cmd in commands[command_start:])==1
    assert scope['_semantic_snapshot'](media)==original_semantics
    assert sidecar.read_bytes().startswith(b'\xef\xbb\xbf') and 'Chá' in sidecar.read_text(encoding='utf-8-sig')
    assert len(scope['get_valid_media'](str(media),scope['text_track_manifest'](media)[0]))==4
    assert not list(root.glob('.*.vse-*.tmp'))

    for invalid in ('stale_digest', 'changed_timing', 'verification', 'disk'):
        media = fixture('autofix-' + invalid)
        original_bytes = media.read_bytes()
        cached_track = scope['get_valid_tracks'](str(media), scope['text_track_manifest'](media)[0])[0]
        fix = dict(source='embedded', type_index=0, external_path='', before_digest=hashlib.sha256(cached_track.text.encode()).hexdigest(),
                   text=cached_track.text.replace('Olá', 'Café'))
        if invalid == 'stale_digest': fix['before_digest'] = '0' * 64
        if invalid == 'changed_timing':
            timing = next(line for line in fix['text'].splitlines() if cleanup.TIMING.fullmatch(line))
            fix['text'] = fix['text'].replace(timing, timing.split('-->')[0] + '--> 00:00:59,999', 1)
        context = (patch.object(pipeline, '_verify', side_effect=RuntimeError('Injected output verification failure')) if invalid == 'verification'
                   else patch.object(cleanup, 'output_space', no_space) if invalid == 'disk' else patch.object(cleanup, 'logger'))
        with context:
            try:
                cleanup.replace_reviewed_subtitles(media, [fix])
            except (RuntimeError, HttpError):
                pass
            else:
                raise AssertionError(f'Unsafe autofix accepted: {invalid}')
        assert media.read_bytes() == original_bytes
        assert not list(root.glob(f'.{media.stem}.subtitle-clean.vse-*'))
    print('PASS: approved autofix texts share one cache-first verified remux; rejected streams/metadata preserved; stale preview, timing, disk and verification failures retain originals')

    # True non-UTF-8 Matroska packets, not a fake encoding label. Replacing
    # two approved subtitles must write once even though Unicode stays equal.
    legacy_text = '1\n00:00:00,000 --> 00:00:01,000\n<i>Olá, café.</i> Não, amanhã.\n'
    legacy1, legacy2 = root/'legacy1.srt', root/'legacy2.srt'
    legacy1.write_bytes(legacy_text.encode('cp1252'))
    legacy2.write_bytes(legacy_text.replace('café','chá').encode('cp1252'))
    media = root/'legacy-packets.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(original),'-i',str(legacy1),'-i',str(legacy2),
                    '-map','0:v','-map','1:s','-map','0:a','-map','2:s','-map','0:s:2',
                    '-map_metadata','0','-map_chapters','0','-c','copy',str(media)], check=True)
    subprocess.run(['mkvpropedit',str(media),'--edit','track:s1','--set','language=por','--set','language-ietf=pt-BR',
                    '--set','name=Legacy Portuguese','--set','flag-forced=1',
                    '--edit','track:s2','--set','language=por','--set','language-ietf=pt-PT'], capture_output=True,check=True)
    signature, tracks=scope['text_track_manifest'](media)
    originals=[real_extract(media,t) for t in tracks]
    assert originals[0].source_encoding==originals[1].source_encoding=='Windows-1252 (inferred)'
    scope['publish_media'](str(media),signature,{t.key for t in originals},originals)
    before_semantics=scope['_semantic_snapshot'](media)
    fixes=[dict(source=t.source,type_index=t.type_index,external_path=t.external_path,
                before_digest=hashlib.sha256(t.text.encode()).hexdigest(),text=t.text,
                source_encoding=t.source_encoding,normalize_encoding=True) for t in originals[:2]]
    command_start=len(commands)
    result=cleanup.replace_reviewed_subtitles(media,fixes)
    assert result['changed_subtitles']==2 and result['remuxes']==1, result
    assert sum(cmd[0] in {'ffmpeg','mkvmerge'} for cmd in commands[command_start:])==1
    assert scope['_semantic_snapshot'](media)==before_semantics
    actual=[real_extract(media,t) for t in scope['text_track_manifest'](media)[1]]
    assert all(actual[i].source_encoding=='UTF-8' and actual[i].text==originals[i].text for i in (0,1))
    assert actual[2].text==originals[2].text
    cached=scope['get_valid_media'](str(media),scope['text_track_manifest'](media)[0])
    assert cached[0].source_encoding==cached[1].source_encoding=='UTF-8'
    committed=media.read_bytes()
    try:
        cleanup.replace_reviewed_subtitles(media,fixes)
    except HttpError as exc:
        assert exc.status_code==409
    else:
        raise AssertionError('Reused legacy encoding approval was accepted for a UTF-8 source')
    assert media.read_bytes()==committed
    # Sidecar encoding-only normalization is atomic, no remux, text unchanged.
    sidecar=media.with_suffix('.pt.srt')
    sidecar.write_bytes(legacy_text.encode('cp1252'))
    signature,tracks=scope['text_track_manifest'](media)
    originals=[real_extract(media,t) for t in tracks]
    scope['publish_media'](str(media),signature,{t.key for t in originals},originals)
    external=next(t for t in originals if t.source=='external')
    fix=dict(source='external',type_index=-1,external_path=str(sidecar),text=external.text,
             before_digest=hashlib.sha256(external.text.encode()).hexdigest(),source_encoding=external.source_encoding,normalize_encoding=True)
    result=cleanup.replace_reviewed_subtitles(media,[fix])
    assert result['remuxes']==0 and result['changed_subtitles']==1
    assert sidecar.read_bytes()==legacy_text.encode('utf-8') and media.read_bytes()==committed
    print('PASS: real legacy packets normalized to UTF-8 without dialogue/timing/metadata changes; one remux, source evidence checked again, external encoding-only atomic write')

print('PASS: one remux for multiple cached subtitles; one batch for missing tracks; atomic complete/partial caches; exact timing, interleaved order, pt-BR/pt-PT, flags, tags, chapters, attachments; external BOM; no-op; failure/race/cache recovery')
