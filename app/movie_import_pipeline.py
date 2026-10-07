"""Build an imported movie once, without editing or deleting its source.

There is no full-size staging copy: the only movie-sized temporary file lives
in the destination filesystem and becomes the final file after verification.
This module does not enqueue indexes or mutate catalog/learning state.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi import HTTPException

from app.job_safety import copy_recovery, output_space, run_write_command, stamp
from app.subtitle_html import strip_non_color_html
from app.subtitle_timing import container_start_ms, native_shift_ms, set_cached_timeline, shift_srt
from app.v2 import DATA_DIR, make_language, probe


def identify(path: Path) -> dict:
    result = subprocess.run(['mkvmerge', '-J', str(path)], capture_output=True,
                            text=True, check=True, timeout=120)
    value = json.loads(result.stdout)
    if not value.get('tracks'):
        raise RuntimeError('Cannot identify import tracks; original retained')
    return value


def _flags(request, kind: str, key: str, properties: dict, update=None) -> dict:
    properties = copy.deepcopy(properties)
    for flag, prop in (('default', 'default_track'), ('forced', 'forced_track')):
        override = getattr(update, flag, None)
        selection = getattr(request, f'{flag}_{kind}')
        properties[prop] = (bool(override) if override is not None else
                            bool(properties.get(prop)) if selection == '__preserve__' else key == selection)
    if update is not None:
        if update.title is not None:
            properties['track_name'] = update.title.strip() or None
        if update.language is not None or update.region is not None:
            from app.v43 import legacy_language_code
            language = make_language(update.language, update.region)
            properties['language'] = legacy_language_code(update.language) or 'und'
            properties['language_ietf'] = language or None
    return properties


def make_plan(source: Path, request, sidecars: list[dict], cleanups: list) -> dict:
    """Resolve original identities once; never confuse indexes after removal."""
    data = probe(source)
    native = source.suffix.casefold() in {'.mkv', '.mk3d', '.mka', '.mks', '.webm'}
    identified = identify(source) if native else None
    typed = {kind: [s for s in data.get('streams', []) if s.get('codec_type') == kind
                   and not (native and (s.get('disposition') or {}).get('attached_pic'))]
             for kind in ('video', 'audio', 'subtitle')}
    known = {str(Path(s['path']).resolve()): Path(s['path']).resolve() for s in sidecars}
    externals = {}
    for item in request.external_subtitles:
        path = str(Path(item.path).resolve())
        if path not in known or item.path != path or path in externals:
            raise HTTPException(409, 'External subtitle selection no longer matches the source movie')
        externals[path] = item
    valid = {f'embedded:{kind}:{n}' for kind in ('audio', 'subtitle') for n in range(len(typed[kind]))}
    valid |= {f'external:{path}' for path in known}
    if any(key not in valid for key in request.remove):
        raise HTTPException(409, 'A selected removal no longer matches the source movie')
    updates = {(t.codec_type, t.type_index): t for t in request.tracks}
    if len(updates) != len(request.tracks):
        raise HTTPException(409, 'Conflicting duplicate stream changes in the import request')
    if any(index >= len(typed[kind]) for kind, index in updates):
        raise HTTPException(409, 'A selected stream no longer exists in the source movie')
    removed = set(request.remove)
    entries = {}
    original_positions = {}
    for kind in ('video', 'audio', 'subtitle'):
        native_tracks = [t for t in (identified or {}).get('tracks', [])
                         if t['type'] == ('subtitles' if kind == 'subtitle' else kind)]
        if native and len(native_tracks) != len(typed[kind]):
            raise RuntimeError('FFprobe and Matroska track identities disagree; import refused')
        for index, stream in enumerate(typed[kind]):
            key = f'embedded:{kind}:{index}'
            if key in removed:
                continue
            track = native_tracks[index] if native else None
            props = copy.deepcopy(track['properties']) if track else {
                'language': (stream.get('tags') or {}).get('language') or 'und',
                'track_name': (stream.get('tags') or {}).get('title') or None,
                'default_track': bool((stream.get('disposition') or {}).get('default')),
                'forced_track': bool((stream.get('disposition') or {}).get('forced')),
            }
            if kind != 'video':
                props = _flags(request, kind, key, props, updates.get((kind, index)))
            elif request.clear_video_titles:
                props['track_name'] = None
            entries[key] = dict(key=key, kind=kind, index=index, stream=stream, properties=props,
                                track=track, input=0, track_id=track['id'] if track else stream['index'])
            if native:
                original_positions[key] = identified['tracks'].index(track)
    for path, item in externals.items():
        key = f'external:{path}'
        if not item.embed or key in removed:
            continue
        from app.v43 import legacy_language_code
        entries[key] = dict(key=key, kind='subtitle', index=None, stream=None, track=None, path=known[path],
                            properties=_flags(request, 'subtitle', key, {
                                'language': legacy_language_code(item.language) or 'und',
                                'language_ietf': make_language(item.language or 'und', item.region),
                                'track_name': item.title.strip() or None,
                                'default_track': False, 'forced_track': item.forced,
                            }))
    ordered = {kind: [] for kind in typed}
    for item in request.order:
        key = f'embedded:{item.codec_type}:{item.type_index}' if item.source == 'embedded' else f'external:{item.path}'
        if key not in valid:
            raise HTTPException(409, 'Requested stream order no longer matches the source movie')
        if key in entries and entries[key] not in ordered[item.codec_type]:
            ordered[item.codec_type].append(entries[key])
    for entry in entries.values():
        if entry not in ordered[entry['kind']]:
            ordered[entry['kind']].append(entry)
    for kind in ('audio', 'subtitle'):
        for flag in ('default', 'forced'):
            selected = getattr(request, f'{flag}_{kind}')
            if selected and selected != '__preserve__' and selected not in {t['key'] for t in ordered[kind]}:
                raise HTTPException(409, 'Selected default/forced stream is removed or not integrated')
    cleanup_keys = set()
    for item in cleanups:
        key = f'external:{item.external_path}' if item.external_path else f'embedded:subtitle:{item.type_index}'
        if key not in valid:
            raise HTTPException(409, 'HTML cleanup selection no longer matches the source movie')
        if key not in removed:
            cleanup_keys.add(key)
    if not ordered['video']:
        raise HTTPException(422, 'Import source has no video stream')
    return dict(source=source, request=request, data=data, native=native, identified=identified,
                ordered=ordered, entries=entries, externals=externals, known=known, removed=removed,
                cleanup_keys=cleanup_keys, original_positions=original_positions, cache={}, inputs=[])


def prepare_subtitles(plan: dict, folder: Path) -> None:
    from app.subtitle_cache_worker import TEXT_CODECS, _extract_track, _extract_embedded_batch, text_track_manifest
    from app.v51 import cached_subtitle_text, decode_external, validate_cleaned_srt
    source = plan['source']
    wanted = []
    for entry in plan['ordered']['subtitle']:
        if entry['key'] not in plan['cleanup_keys'] or entry['index'] is None:
            continue
        codec = entry['stream'].get('codec_name', '')
        if codec not in TEXT_CODECS:
            raise HTTPException(422, 'HTML cleanup requires a text subtitle, not an image subtitle')
        wanted.append(dict(source='embedded', type_index=entry['index'], external_path='', codec=codec))
    snapshot = None
    if wanted:
        signature, tracks = text_track_manifest(source, plan['data'])
        snapshot = (signature, {(t['source'], t['type_index'], t['external_path']) for t in tracks})
    texts, missing = {}, []
    for track in wanted:
        text = cached_subtitle_text(source, 'embedded', track['type_index'], snapshot=snapshot, mutation_input=True)
        if text is None:
            missing.append(track)
        else:
            texts[track['type_index']] = text
    batch = _extract_embedded_batch(source, missing)
    for track in missing:
        extracted = batch.get(track['type_index']) or _extract_track(source, track)
        texts[track['type_index']] = extracted.text
    for entry in plan['ordered']['subtitle']:
        if entry['index'] is not None and entry['index'] in texts:
            old = texts[entry['index']]
            cleaned = strip_non_color_html(old)
            if cleaned != old:
                validate_cleaned_srt(cleaned, cleaned)
                path = folder / f"cleaned-{entry['index']}.srt"
                path.write_text(cleaned, encoding='utf-8')
                entry['path'] = path
                entry['cleaned'] = cleaned
                set_cached_timeline(entry, plan['data'], plan['native'], plan['identified'])
    plan['sidecar_cleaned'] = {}
    for path in plan['known']:
        key = f'external:{path}'
        if key not in plan['cleanup_keys']:
            continue
        original = plan['known'][path]
        if original.suffix.casefold() not in {'.srt', '.ass', '.ssa', '.vtt'}:
            raise HTTPException(422, 'HTML cleanup requires a text external subtitle')
        if original.stat().st_size > 32 * 1024**2:
            raise HTTPException(422, 'External subtitle exceeds the 32 MiB safety limit')
        old, _encoding = decode_external(original.read_bytes())
        if '\ufffd' in old:
            raise HTTPException(422, 'External subtitle cannot be safely decoded for HTML cleanup')
        cleaned = strip_non_color_html(old)
        prepared = folder / f'external-{len(plan["sidecar_cleaned"])}{original.suffix}'
        prepared.write_text(cleaned, encoding='utf-8')
        plan['sidecar_cleaned'][path] = prepared
        if key in plan['entries']:
            plan['entries'][key]['path'] = prepared
        if original.suffix.casefold() == '.srt' and cleaned.strip():
            validate_cleaned_srt(cleaned, cleaned)
            if key in plan['entries']:
                plan['entries'][key]['verification_text'] = cleaned
    request = plan['request']
    if request.audio_compatibility:
        from app.review_audio import resolve_integrations
        integrations = resolve_integrations(source, request.audio_compatibility)
        for change, path, stage in integrations:
            key = f"embedded:audio:{stage['audio_index']}"
            entry = plan['entries'].get(key)
            if not entry or key in plan['removed']:
                raise HTTPException(409, 'Approved AAC source was removed from this import')
            if change.action == 'add':
                entry = copy.deepcopy(entry)
                entry['added_compatibility'] = True
                entry['properties']['track_name'] = (entry['properties'].get('track_name') or '') + ' · AAC stereo'
                entry['properties'].update(default_track=False, forced_track=False)
                plan['ordered']['audio'].append(entry)
            entry.update(path=path, compatibility=True,
                         offset=float(json.loads(stage['metadata_json']).get('start_time') or 0),
                         input_snapshot=stamp(path))


def requires_remux(plan: dict) -> bool:
    if not plan['native'] or any(t.get('path') for kind in plan['ordered'].values() for t in kind):
        return True
    for kind in ('audio', 'subtitle'):
        indexes = [t['index'] for t in plan['ordered'][kind]]
        count = sum(s.get('codec_type') == kind for s in plan['data'].get('streams', []))
        if indexes != list(range(count)):
            return True
    return False


def _native_command(plan: dict, output: Path, folder: Path) -> list[str]:
    from app.matroska_remux import _TRACK_PROPERTIES
    # In-place subtitle operations keep the original global order, including
    # containers with subtitles interleaved with audio/video tracks. Imports
    # retain their explicitly requested, per-kind ordering.
    entries = plan.get('ordered_entries') or [t for kind in ('video', 'audio', 'subtitle') for t in plan['ordered'][kind]]
    selected = {t['track_id'] for t in entries if not t.get('path')}
    command = ['mkvmerge', '--ui-language', 'en_US', '--quiet', '--engage', 'force_passthrough_packetizer',
               '--normalize-language-ietf', 'off', '--disable-track-statistics-tags', '-o', str(output)]
    if output.suffix.casefold() == '.webm':
        command.append('--webm')
    inputs = []
    for entry in entries:
        if not entry.get('path'):
            continue
        data = identify(entry['path'])
        matching = [t for t in data['tracks'] if t['type'] == ('subtitles' if entry['kind'] == 'subtitle' else entry['kind'])]
        if len(matching) != 1 or len(data['tracks']) != 1:
            raise HTTPException(422, 'An import subtitle/audio input must contain exactly one selected stream')
        entry.update(input=len(inputs) + 1, track_id=matching[0]['id'], input_track=matching[0])
        if entry.get('compatibility'):
            # A deliberately transcoded audio input owns its new decoder
            # delay; do not inherit the original codec's delay here.
            entry['properties']['codec_delay'] = matching[0]['properties'].get('codec_delay')
        inputs.append(entry)
        if not entry.get('track'):
            props = {key: matching[0]['properties'].get(key) for key in _TRACK_PROPERTIES}
            props['enabled_track'] = matching[0]['properties'].get('enabled_track', True)
            props.update(entry['properties'])
            entry['properties'] = props
    other = [t for t in plan['identified']['tracks'] if t['type'] not in {'video', 'audio', 'subtitles'}]
    command += ['--track-order', ','.join([f"{t['input']}:{t['track_id']}" for t in entries] +
                                        [f"0:{t['id']}" for t in other])]
    for kind, option in (('audio', '--audio-tracks'), ('subtitles', '--subtitle-tracks')):
        ids = [str(t['id']) for t in plan['identified']['tracks'] if t['type'] == kind and t['id'] in selected]
        command += [option, ','.join(ids)] if ids else ['--no-audio' if kind == 'audio' else '--no-subtitles']
    command.append('=' + str(plan['source']))  # Do not auto-concatenate numbered source movies.
    tags = None
    if any(entry.get('track') for entry in inputs):
        result = subprocess.run(['mkvextract', str(plan['source']), 'tags'], capture_output=True, check=True, timeout=120)
        tags = ET.fromstring(result.stdout)
    for entry in inputs:
        command += ['--no-global-tags', '--no-chapters', '--no-attachments']
        if entry.get('offset'):
            command += ['--sync', f"{entry['track_id']}:{round(entry['offset'] * 1000)}"]
        if entry.get('track') and not entry.get('added_compatibility') and tags is not None:
            root = ET.Element('Tags')
            uid = str(entry['track']['properties'].get('uid'))
            for tag in tags.findall('Tag'):
                if tag.findtext('Targets/TrackUID') == uid:
                    cloned = copy.deepcopy(tag)
                    cloned.find('Targets').remove(cloned.find('Targets/TrackUID'))
                    root.append(cloned)
            if len(root):
                path = folder / f"tags-{entry['input']}.xml"
                ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
                command += ['--tags', f"{entry['track_id']}:{path}"]
        # Explicit zero prevents mkvmerge's automatic first-default promotion.
        command += ['--default-track-flag', f"{entry['track_id']}:{int(entry['properties'].get('default_track', False))}",
                    '--forced-display-flag', f"{entry['track_id']}:{int(entry['properties'].get('forced_track', False))}"]
        # Set replacement metadata while writing Tracks at the front. Adding
        # a long name/region/SDH flag only afterwards can relocate the headers
        # and require another whole-file layout-repair remux.
        properties = entry['properties']
        command += ['--language', f"{entry['track_id']}:{properties.get('language_ietf') or properties.get('language') or 'und'}",
                    '--track-name', f"{entry['track_id']}:{properties.get('track_name') or ''}"]
        for prop, option in (('enabled_track', '--track-enabled-flag'),
                             ('flag_hearing_impaired', '--hearing-impaired-flag'),
                             ('flag_visual_impaired', '--visual-impaired-flag'),
                             ('flag_commentary', '--commentary-flag'),
                             ('flag_original', '--original-flag'),
                             ('flag_text_descriptions', '--text-descriptions-flag')):
            if properties.get(prop) is not None:
                command += [option, f"{entry['track_id']}:{int(properties[prop])}"]
        command.append('=' + str(entry['path']))
    plan['output_entries'] = entries
    return command


def _native_headers(plan: dict, output: Path) -> None:
    """Metadata-only header edits also undo unwanted ISO/IETF normalization."""
    from app.matroska_remux import _TRACK_PROPERTIES
    entries = plan.get('output_entries') or [t for t in plan['identified']['tracks']]
    current = identify(output)['tracks']
    options = []
    property_names = dict(track_name='name', default_track='flag-default', forced_track='flag-forced',
                          enabled_track='flag-enabled', flag_hearing_impaired='flag-hearing-impaired',
                          flag_visual_impaired='flag-visual-impaired', flag_commentary='flag-commentary',
                          flag_original='flag-original', flag_text_descriptions='flag-text-descriptions',
                          codec_delay='codec-delay')
    if not plan.get('output_entries'):
        by_id = {t['track_id']: t for kind in plan['ordered'].values() for t in kind}
        entries = [by_id.get(t['id'], dict(properties=t['properties'])) for t in entries]
        plan['output_entries'] = entries
    for index, entry in enumerate(entries):
        desired = entry['properties']
        actual = current[index]['properties']
        edits = []
        # Write both, in this order: changing the ISO code can rewrite IETF.
        if desired.get('language') != actual.get('language') or desired.get('language_ietf') != actual.get('language_ietf'):
            edits += ['--set', f"language={desired.get('language') or 'und'}"]
            edits += ['--set', f"language-ietf={desired['language_ietf']}"] if desired.get('language_ietf') else ['--delete', 'language-ietf']
        for prop in _TRACK_PROPERTIES:
            if prop not in property_names or desired.get(prop) == actual.get(prop):
                continue
            value = desired.get(prop)
            if value is None:
                if prop in {'track_name', 'codec_delay'}:
                    edits += ['--delete', property_names[prop]]
                continue
            edits += ['--set', f"{property_names[prop]}={int(value) if isinstance(value, bool) else value}"]
        if edits:
            options += ['--edit', f'track:{index + 1}', *edits]
    if options:
        run_write_command(['mkvpropedit', str(output), *options], output.parent, timeout=600)


def _ffmpeg_command(plan: dict, output: Path) -> list[str]:
    from app.v7 import disposition_flags
    entries = [t for kind in ('video', 'audio', 'subtitle') for t in plan['ordered'][kind]]
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(plan['source'])]
    for entry in entries:
        if entry.get('path'):
            entry['input'] = 1 + sum(bool(t.get('path')) for t in entries[:entries.index(entry)])
            if entry.get('offset'):
                command += ['-itsoffset', str(entry['offset'])]
            command += ['-i', str(entry['path'])]
    for entry in entries:
        command += ['-map', f"{entry['input']}:0" if entry.get('path') else f"0:{entry['stream']['index']}"]
    command += ['-map', '0:t?', '-map', '0:d?', '-map_metadata', '0', '-map_chapters', '0', '-c', 'copy']
    for index, entry in enumerate(entries):
        properties = entry['properties']
        from app.v43 import legacy_language_code
        language = legacy_language_code(properties.get('language')) or 'und'
        command += [f'-metadata:s:{index}', f'language={language}',
                    f'-metadata:s:{index}', f"title={properties.get('track_name') or ''}",
                    f'-disposition:{index}', disposition_flags(entry.get('stream') or {},
                        bool(properties.get('default_track')), bool(properties.get('forced_track')))]
    if output.suffix.casefold() in {'.mp4', '.m4v', '.mov'}:
        command += ['-movflags', '+faststart', '-c:s', 'mov_text']
    command.append(str(output))
    plan['output_entries'] = entries
    return command


def _verify(plan: dict, output: Path) -> None:
    from app.subtitle_cache_worker import _extract_track, _extract_embedded_batch
    from app.v51 import validate_cleaned_srt
    data = probe(output)
    entries = plan.get('ordered_entries') or [t for kind in ('video', 'audio', 'subtitle') for t in plan['ordered'][kind]]
    streams = [s for s in data.get('streams', []) if s.get('codec_type') in {'video', 'audio', 'subtitle'}
               and not (plan['native'] and (s.get('disposition') or {}).get('attached_pic'))]
    if [s['codec_type'] for s in streams] != [t['kind'] for t in entries]:
        raise RuntimeError('Import output failed stream-count/order verification; original retained')
    for entry, actual in zip(entries, streams):
        codec = entry['stream']['codec_name'] if entry.get('stream') else None
        if entry.get('compatibility'):
            codec = 'aac'
            if actual.get('channels') != 2:
                raise RuntimeError('Approved AAC import is not stereo; original retained')
        elif entry.get('cleaned'):
            codec = 'subrip' if plan['native'] else 'mov_text' if output.suffix.casefold() in {'.mp4', '.m4v', '.mov'} else 'subrip'
        if codec and actual.get('codec_name') != codec:
            raise RuntimeError('Import output changed an unrequested codec; original retained')
        if plan.get('verify_passthrough_cache') and entry['kind'] in ('audio','video') and not entry.get('path'):
            before_start=entry['stream'].get('start_time')
            after_start=actual.get('start_time')
            if before_start not in (None,'N/A') and after_start not in (None,'N/A'):
                observed=round((float(after_start)-float(before_start))*1000)
                if observed != plan.get('source_shift_ms',0):
                    raise RuntimeError('Unrequested audio/video timeline change; original retained')
        if not plan['native']:
            from app.v5 import split_tag, plex_language_pair
            tags = actual.get('tags') or {}
            properties = entry['properties']
            wanted = properties.get('language_ietf') or properties.get('language') or 'und'
            found = tags.get('language') or 'und'
            if plex_language_pair(*split_tag(wanted)) != plex_language_pair(*split_tag(found)):
                raise HTTPException(422, f'This container cannot preserve requested language/region {wanted}; use Matroska')
            if (tags.get('title') or tags.get('handler_name') or '') != (properties.get('track_name') or ''):
                # MP4 handler_name is usually a generated "VideoHandler" or
                # "SoundHandler" rather than a user track title.
                if (tags.get('title') or '') != (properties.get('track_name') or ''):
                    raise HTTPException(422, 'This container did not preserve the requested track name; use Matroska')
    if plan['native']:
        from app.matroska_remux import _semantic_snapshot, _TRACK_PROPERTIES
        original = _semantic_snapshot(plan['source'])
        actual = _semantic_snapshot(output)
        desired = copy.deepcopy(original)
        wanted_tracks = []
        positions = {}
        for position, entry in enumerate(entries):
            before_position = plan['original_positions'].get(entry['key'])
            if before_position is not None and before_position not in positions:
                positions[before_position] = position
            codec = (entry.get('input_track') or entry.get('track'))['codec']
            wanted_tracks.append(('subtitles' if entry['kind'] == 'subtitle' else entry['kind'], codec,
                                  tuple((key, entry['properties'].get(key)) for key in _TRACK_PROPERTIES)))
        for position, track in enumerate(plan['identified']['tracks']):
            if track['type'] not in {'video', 'audio', 'subtitles'}:
                positions[position] = len(wanted_tracks)
                wanted_tracks.append(original['tracks'][position])
        desired['tracks'] = wanted_tracks
        desired['tags'] = sorted([( ('track', positions[owner[1]]) if owner[0] == 'track' else owner, *values)
                                  for owner, *values in original['tags']
                                  if owner[0] != 'track' or owner[1] in positions], key=str)
        if actual != desired:
            sections = ', '.join(key for key in desired if desired[key] != actual[key])
            details = []
            for n, (wanted, found) in enumerate(zip(desired['tracks'], actual['tracks']), 1):
                if wanted[:2] != found[:2]:
                    details.append(f'track {n} codec/type: {wanted[:2]} != {found[:2]}')
                for key, value in wanted[2]:
                    if value != dict(found[2]).get(key):
                        details.append(f'track {n} {key}: {value!r} != {dict(found[2]).get(key)!r}')
            raise RuntimeError(f'Import failed Matroska metadata verification ({sections}); original retained: ' + '; '.join(details)[:1200])
    for kind in ('audio', 'subtitle'):
        selected = [s for s in streams if s['codec_type'] == kind]
        for entry, actual in zip(plan['ordered'][kind], selected):
            for prop, flag in (('default_track', 'default'), ('forced_track', 'forced')):
                if bool((actual.get('disposition') or {}).get(flag)) != bool(entry['properties'].get(prop)):
                    raise RuntimeError(f'Import failed {kind} {flag} verification; original retained')
    for field in ('duration',):
        before = float((plan['data'].get('format') or {}).get(field) or 0)
        after = float((data.get('format') or {}).get(field) or 0)
        # Removing the longest trailing subtitle may legitimately shorten the container duration.
        if before and after > before + 1:
            raise RuntimeError('Import output timeline unexpectedly increased; original retained')
    clean_tracks = [dict(source='embedded', type_index=n, external_path='', codec=streams_by_kind['codec_name'])
                    for n, (entry, streams_by_kind) in enumerate(zip(plan['ordered']['subtitle'],
                        [s for s in streams if s['codec_type'] == 'subtitle'])) if entry.get('cleaned') or entry.get('verification_text')]
    batch = _extract_embedded_batch(output, clean_tracks)
    passthrough_checks = set()
    for track in clean_tracks:
        index = track['type_index']
        entry = plan['ordered']['subtitle'][index]
        passthrough = plan['native'] and plan.get('verify_passthrough_cache') and not entry.get('path')
        try:
            text = (batch.get(index) or _extract_track(output, track)).text
            expected = shift_srt(entry.get('verification_text') or entry['cleaned'],
                                 entry.get('text_origin_ms', 0) - container_start_ms(data))
            plan.get('subtitle_validator', validate_cleaned_srt)(expected, text)
            timing = lambda value: re.findall(r'^\s*(\d\d:\d\d:\d\d[,.]\d\d\d)\s*-->\s*(\d\d:\d\d:\d\d[,.]\d\d\d)', value, re.M)
            if timing(text) != timing(expected):
                raise RuntimeError('Cleaned subtitle timing changed during import; original retained')
        except RuntimeError as exc:
            if not passthrough:
                raise RuntimeError(f'Subtitle 0:s:{index}: {exc}') from exc
            # An old cache decoder may have normalized tags or malformed text.
            # It is not evidence that an untouched native track was changed.
            # Prove physical packet bytes AND timestamps below, then discard
            # that track's stale cache. Never relax a selected replacement's
            # strict dialogue/timing validation or silently "repair" corruption.
            passthrough_checks.add(entry['stream']['index'])
            plan.setdefault('discard_cached_subtitles', set()).add(index)
            continue
        plan['cache'][index] = text
    if passthrough_checks:
        from app.matroska_remux import _subtitle_packets
        delta=plan.get('source_shift_ms',0)
        before = [(p[0],p[1],p[2]+delta,p[3]) for p in _subtitle_packets(plan['source']) if p[0] in passthrough_checks]
        after = [p for p in _subtitle_packets(output) if p[0] in passthrough_checks]
        if not before or before != after:
            raise RuntimeError('Untouched subtitle bytes or physical timing changed; original retained')


def _register_output(path: Path, token: str, *, target: Path | None = None, sidecars: list[str] = ()) -> Path:
    """Small durable ownership receipt; startup can remove interrupted output."""
    root = DATA_DIR / 'import-work'
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / f'{token}.json'
    value = dict(version=1, temporary=str(path), identity=stamp(path), target=str(target) if target else '',
                 sidecars=[stamp(Path(sidecar)) for sidecar in sidecars])
    prepared = root / f'{token}.json.tmp'
    prepared.write_text(json.dumps(value), encoding='utf-8')
    with prepared.open('rb') as saved:
        os.fsync(saved.fileno())
    os.replace(prepared, receipt)
    with receipt.open('rb') as saved:
        os.fsync(saved.fileno())
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return receipt


def recover_interrupted_imports() -> None:
    """Called before workers start; never removes final files or original media."""
    root = DATA_DIR / 'import-work'
    if not root.is_dir():
        return
    for receipt in root.glob('*.json'):
        try:
            value = json.loads(receipt.read_text())
            token = receipt.stem
            path = Path(value['temporary'])
            saved = value['identity']
            if (value.get('version') != 1 or not re.fullmatch(r'[a-f0-9]{32}', token)
                    or not path.is_absolute() or not path.name.startswith('.')
                    or f'.vse-import-{token}.' not in path.name):
                continue
            if path.exists():
                current = stamp(path)
                if any(current[field] != saved[field] for field in ('path', 'device', 'inode')):
                    continue
                final = Path(value['target']) if value.get('target') else None
                published = bool(final and final.parent == path.parent and final.exists()
                                 and final.stat().st_dev == current['device'] and final.stat().st_ino == current['inode'])
                if not published:
                    for side in value.get('sidecars', []):
                        owned = Path(side['path'])
                        if (owned.parent == path.parent and owned.is_file()
                                and stamp(owned) == side):
                            owned.unlink()
                path.unlink()
            receipt.unlink()
        except (OSError, ValueError, KeyError):
            import logging
            logging.getLogger('videostreamedit').warning('movie_import event=interrupted_output_requires_review receipt=%s', receipt)


def build_import(source: Path, target: Path, request, sidecars: list[dict], cleanups: list, progress,
                 source_snapshot: dict, sidecar_snapshots: list[dict]) -> dict:
    """One movie output, bounded free space, no-clobber publication."""
    token = uuid.uuid4().hex
    temporary = target.with_name(f'.{target.stem}.vse-import-{token}{target.suffix}')
    published_sidecars = []
    owned = []
    receipt = None
    committed = False
    plan = make_plan(source, request, sidecars, cleanups)
    # Fail obvious destination conflicts before reading/writing a large movie.
    output_names = [target] + [target.with_name(target.stem + Path(item['path']).name[len(source.stem):])
                              for item in sidecars if f"external:{item['path']}" not in plan['removed']
                              and f"external:{item['path']}" not in plan['entries']]
    if any(path.exists() or path.is_symlink() for path in output_names):
        raise HTTPException(409, 'An output movie or subtitle already exists; existing files retained')
    snapshots = [source_snapshot, *sidecar_snapshots]
    required = int(source_snapshot['size'] * 1.1) + sum(s['size'] for s in sidecar_snapshots) + 64 * 1024**2
    warnings = []
    durable = False
    try:
        with output_space(target.parent, required), tempfile.TemporaryDirectory(prefix='vse-import-subtitles-') as small:
            folder = Path(small)
            progress(2, 'Preparing subtitle changes', 'Reusing valid cached text; extracting missing selected subtitles together')
            prepare_subtitles(plan, folder)
            required += sum(t['path'].stat().st_size for kind in plan['ordered'].values() for t in kind if t.get('compatibility'))
            # Additional approved audio is admitted too; nested admission is reentrant.
            with output_space(target.parent, required):
                with temporary.open('xb'):
                    pass
                owned.append(temporary)
                receipt = _register_output(temporary, token, target=target)
                remux = requires_remux(plan)
                if remux:
                    progress(3, 'Writing imported media', 'One stream-copy pass: cleanup, removals, order and integrations together')
                    command = _native_command(plan, temporary, folder) if plan['native'] else _ffmpeg_command(plan, temporary)
                    result = run_write_command(command, target.parent)
                    receipt = _register_output(temporary, token, target=target)
                    if result.get('returncode'):
                        raise RuntimeError('Import tool reported a warning; output requires review')
                else:
                    progress(3, 'Copying media', 'Metadata-only import: copy plus a small header edit',
                             copy_path=temporary, copy_bytes=source_snapshot['size'])
                    copy_recovery(source, temporary)
                if plan['native']:
                    _native_headers(plan, temporary)
                from app.matroska_remux import ensure_front_track_headers
                ensure_front_track_headers(temporary)
                progress(4, 'Preparing external subtitles', 'Only retained sidecars are copied; originals remain untouched')
                for number, item in enumerate(sidecars):
                    old = Path(item['path'])
                    key = f'external:{old}'
                    if key in plan['removed'] or key in plan['entries']:
                        continue
                    suffix = old.name[len(source.stem):]
                    new = target.with_name(target.stem + suffix)
                    prepared = folder / f'sidecar-{number}{old.suffix}'
                    shutil.copy2(plan['sidecar_cleaned'].get(str(old), old), prepared)
                    # Exclusive creation refuses existing sidecars. Small
                    # preparation files can live on another filesystem.
                    with new.open('xb') as outgoing, prepared.open('rb') as incoming:
                        owned.append(new)
                        shutil.copyfileobj(incoming, outgoing)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    shutil.copystat(old, new)
                    published_sidecars.append(str(new))
                    receipt = _register_output(temporary, token, target=target, sidecars=published_sidecars)
                progress(5, 'Verifying imported media', 'Checking streams, language/region, flags, subtitle text and unchanged sources')
                _verify(plan, temporary)
                if any(stamp(Path(s['path'])) != s for s in snapshots):
                    raise HTTPException(409, 'Source movie or subtitle changed during import; originals retained')
                from app.v5 import external_subtitles
                if {s['path'] for s in external_subtitles(source)} != set(plan['known']):
                    raise HTTPException(409, 'Source subtitles changed during import')
                if any(stamp(entry['path']) != entry['input_snapshot'] for entry in plan['ordered']['audio']
                       if entry.get('input_snapshot')):
                    raise HTTPException(409, 'Approved audio input changed during import; originals retained')
                shutil.copystat(source, temporary)
                with temporary.open('rb') as completed:
                    os.fsync(completed.fileno())
                # Hard-link publication is atomic and cannot overwrite an existing movie.
                os.link(temporary, target)
                committed = True
                from app.matroska_layout import safe_checkpoint
                safe_checkpoint(target, force=True)
                descriptor = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                    durable = True
                finally:
                    os.close(descriptor)
    except FileExistsError as exc:
        raise HTTPException(409, 'An output movie or subtitle already exists; existing files retained') from exc
    except Exception:
        if not committed:
            raise
        warnings.append('Movie imported, but a post-commit durability acknowledgement failed; verify storage health.')
    finally:
        for path in reversed(owned):
            if path == temporary or not committed:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    warnings.append(f'Temporary import output could not be removed: {path}')
        if receipt and not temporary.exists():
            try:
                receipt.unlink(missing_ok=True)
            except OSError:
                warnings.append('Small import ownership receipt will be checked at next startup.')
    return dict(external_subtitles=published_sidecars, warnings=warnings, plan=plan, durable=durable,
                target_snapshot=stamp(target), target_subtitles=[stamp(Path(p)) for p in published_sidecars],
                operation='single_remux' if remux else 'copy_and_metadata', media_writes=1)


def publish_import_cache(target: Path, plan: dict) -> None:
    """Publish all verified replacements together, using their OUTPUT indexes."""
    if not plan['cache']:
        return
    from app.subtitle_cache import TextSubtitle, publish_track, invalidate_media
    from app.subtitle_cache_worker import text_track_manifest
    signature, tracks = text_track_manifest(target)
    expected = {(t['source'], t['type_index'], t['external_path']) for t in tracks}
    for track in tracks:
        if track['source'] == 'embedded' and track['type_index'] in plan['cache']:
            publish_track(str(target), signature, expected, TextSubtitle(
                'embedded', track['type_index'], '', track['codec'], plan['cache'][track['type_index']]))
    if text_track_manifest(target)[0] != signature:
        invalidate_media(str(target))
        raise RuntimeError('Imported movie changed while publishing subtitle cache')
