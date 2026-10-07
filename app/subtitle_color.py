"""Cache-first SRT color edits, through the native metadata-preserving writer."""
from __future__ import annotations

import html
import logging
import re
import shutil
import tempfile
import uuid
from html.parser import HTMLParser
from pathlib import Path

COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')
TIMING = re.compile(r'^\s*\d+:\d{2}:\d{2}[,.]\d{3}\s*-->')


class _RemoveColor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.fonts = []

    def handle_starttag(self, tag, attrs):
        attributes = []
        for name, value in attrs:
            if name.lower() == 'color':
                continue
            if name.lower() == 'style' and value:
                value = ';'.join(p for p in value.split(';') if not re.match(r'\s*color\s*:', p, re.I))
                if not value.strip():
                    continue
            attributes.append((name, value))
        omit = tag == 'font' and not attributes
        if tag == 'font':
            self.fonts.append(omit)
        if not omit:
            suffix = ''.join(' ' + name + ('' if value is None else '="' + html.escape(value, quote=True) + '"')
                             for name, value in attributes)
            self.parts.append('<' + tag + suffix + '>')

    def handle_endtag(self, tag):
        if tag == 'font':
            if not self.fonts:
                raise ValueError('Unbalanced font tags; review the subtitle before recoloring')
            if self.fonts.pop():
                return
        self.parts.append('</' + tag + '>')

    def handle_startendtag(self, tag, attrs):
        if tag == 'font':
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)
        else:
            self.parts.append(self.get_starttag_text())

    def handle_data(self, data): self.parts.append(data)
    def handle_entityref(self, name): self.parts.append('&' + name + ';')
    def handle_charref(self, name): self.parts.append('&#' + name + ';')
    def handle_comment(self, data): self.parts.append('<!--' + data + '-->')


def color_srt(text: str, color: str) -> str:
    """Set cue color without changing dialogue, cue times or other formatting."""
    if not COLOR.fullmatch(color):
        raise ValueError('Subtitle color must be #RRGGBB')
    if re.search(r'\{\\[^}]*\}', text):
        raise ValueError('SRT contains ASS override styling; review it before HTML recoloring')
    blocks = re.split(r'(\n[ \t]*\n)', text.replace('\r\n', '\n').replace('\r', '\n'))
    count = 0
    for n in range(0, len(blocks), 2):
        lines = blocks[n].split('\n')
        position = next((i for i, line in enumerate(lines) if TIMING.match(line)), None)
        if position is None:
            if blocks[n].strip():
                raise ValueError('Malformed SRT cue; no subtitles changed')
            continue
        count += 1
        payload = '\n'.join(lines[position + 1:])
        suffix = '\n' if payload.endswith('\n') else ''
        payload = payload.rstrip('\n')
        if not payload.strip():
            continue
        parser = _RemoveColor()
        parser.feed(payload)
        parser.close()
        if parser.fonts:
            raise ValueError('Unbalanced font tags; review the subtitle before recoloring')
        colored = f'<font color="{color.upper()}">' + ''.join(parser.parts) + '</font>'
        blocks[n] = '\n'.join(lines[:position + 1]) + '\n' + colored + suffix
    if not count and text.strip():
        raise ValueError('No SRT cue timestamps; no subtitles changed')
    return ''.join(blocks)


def validate_color_srt(expected: str, actual: str) -> None:
    # Container muxers may renumber cues/normalize line endings. Compare each
    # timestamp and payload, not cue numbers; tags and text must survive.
    def cues(value):
        result = []
        for block in re.split(r'\n\s*\n', value.replace('\r\n', '\n').strip()):
            lines = block.split('\n')
            i = next((i for i, line in enumerate(lines) if TIMING.match(line)), None)
            if i is not None:
                result.append((lines[i].strip(), '\n'.join(lines[i + 1:]).strip()))
        return result
    if cues(expected) != cues(actual):
        raise RuntimeError('Subtitle color output changed cue timing, text or formatting; original retained')


def apply_color_edit(request) -> dict:
    from fastapi import HTTPException
    from app.v2 import authorized_file
    from app.v5 import external_subtitles
    from app.v51 import cached_subtitle_text
    from app.subtitle_cache_worker import _extract_embedded_batch, _extract_track, text_track_manifest
    from app.movie_import_pipeline import make_plan, prepare_subtitles, _native_command, _native_headers, _verify
    from app.job_safety import stamp, output_space, run_write_command, replace_prepared
    from app.matroska_remux import ensure_front_track_headers
    from app.matroska_layout import safe_checkpoint

    media = authorized_file(request.path)
    if media.suffix.casefold() not in {'.mkv', '.mka', '.mks', '.mk3d'}:
        raise HTTPException(422, 'Subtitle color currently requires Matroska media; no changes made')
    if request.audio_compatibility:
        raise HTTPException(422, 'Apply approved audio integration separately before subtitle recoloring')
    before = stamp(media)
    plan = make_plan(media, request, external_subtitles(media), [])
    # Retained external files are not implicitly integrated or overwritten.
    warnings = []
    wanted = []
    for entry in plan['ordered']['subtitle']:
        codec = (entry.get('stream') or {}).get('codec_name')
        if entry['index'] is not None and codec in {'srt', 'subrip'}:
            wanted.append(dict(source='embedded', type_index=entry['index'], external_path='', codec=codec))
        elif entry['index'] is None and entry['path'].suffix.casefold() == '.srt':
            wanted.append(dict(source='external', type_index=-1, external_path=str(entry['path']), codec='srt'))
        else:
            warnings.append(f"Unchanged subtitle {entry['index'] + 1 if entry['index'] is not None else entry['path'].name}: only SRT/SubRip supports this color action")
    for path in plan['known']:
        if f'external:{path}' not in plan['entries'] and f'external:{path}' not in plan['removed']:
            warnings.append(f'External subtitle left unchanged: {Path(path).name}; select Integrate to include an SRT in recoloring')
    if not wanted:
        raise HTTPException(422, 'No retained SRT/SubRip subtitle is eligible for recoloring')
    signature, tracks = text_track_manifest(media, plan['data'])
    snapshot = (signature, {(t['source'], t['type_index'], t['external_path']) for t in tracks})
    texts, missing = {}, []
    cached_original = {}
    for track in tracks:
        if track['source'] != 'embedded':
            continue
        cached = cached_subtitle_text(media, 'embedded', track['type_index'], snapshot=snapshot)
        if cached is not None:
            cached_original[track['type_index']] = cached
    external_stamps = {str(path): stamp(path) for path in plan['known'].values()}
    for track in wanted:
        key = (track['source'], track['type_index'], track['external_path'])
        text = cached_original.get(track['type_index']) if track['source'] == 'embedded' else cached_subtitle_text(media, track['source'], track['type_index'], external_path=track['external_path'], snapshot=snapshot)
        if text is None: missing.append(track)
        else: texts[key] = text
    cached_count = len(texts)
    batch = _extract_embedded_batch(media, [t for t in missing if t['source'] == 'embedded'])
    for track in missing:
        item = batch.get(track['type_index']) if track['source'] == 'embedded' else None
        texts[(track['source'], track['type_index'], track['external_path'])] = (item or _extract_track(media, track)).text
    temporary = media.with_name(f'.{media.stem}.vse-color-{uuid.uuid4().hex}{media.suffix}')
    try:
        with tempfile.TemporaryDirectory(prefix='vse-subtitle-color-') as folder_name, output_space(media.parent, int(before['size'] * 1.1) + 64 * 1024**2):
            folder = Path(folder_name)
            prepare_subtitles(plan, folder)
            plan['subtitle_validator'] = validate_color_srt
            changed = 0
            for number, entry in enumerate(plan['ordered']['subtitle']):
                key = ('embedded', entry['index'], '') if entry['index'] is not None else ('external', -1, str(entry['path']))
                if key not in texts: continue
                colored = color_srt(texts[key], request.subtitle_color)
                if colored == texts[key]:
                    continue
                changed += 1
                candidate = folder / f'color-{number}.srt'
                candidate.write_text(colored, encoding='utf-8')
                entry.update(path=candidate, verification_text=colored)
            # Avoid a needless whole-file remux when every eligible subtitle
            # already has this color and the request contains no other edits.
            ordered_keys = [entry['key'] for kind in ('video', 'audio', 'subtitle') for entry in plan['ordered'][kind]]
            core_order = []
            for kind in ('video', 'audio', 'subtitle'):
                core_order.extend(key for key, _position in sorted(
                    ((key, position) for key, position in plan['original_positions'].items() if key.startswith(f'embedded:{kind}:')),
                    key=lambda item: item[1]))
            noncolor_change = bool(request.clear_video_titles or request.remove or request.audio_compatibility
                                   or any(entry.get('track') and entry['properties'] != entry['track']['properties']
                                          for kind in ('video', 'audio', 'subtitle') for entry in plan['ordered'][kind])
                                   or any(item.embed for item in request.external_subtitles)
                                   or ordered_keys != core_order)
            if not changed and not noncolor_change:
                return dict(edited=str(media), operation='no_change', warnings=warnings,
                            subtitle_color=request.subtitle_color.upper(), colored_subtitles=0,
                            cached_inputs=cached_count, _color_cache={})
            run_write_command(_native_command(plan, temporary, folder), media.parent, timeout=6 * 60 * 60)
            _native_headers(plan, temporary)
            ensure_front_track_headers(temporary)
            _verify(plan, temporary)
            if any(stamp(Path(p)) != s for p, s in external_stamps.items()):
                raise HTTPException(409, 'External subtitle changed during preparation; original retained')
            shutil.copystat(media, temporary)
            replace_prepared(temporary, media, before)
    finally:
        temporary.unlink(missing_ok=True)
    safe_checkpoint(media, force=True)
    for path in plan['known']:
        key = f'external:{path}'
        if key in plan['removed'] or key in plan['entries']:
            try:
                if stamp(Path(path)) != external_stamps[path]:
                    raise RuntimeError('file changed; retained')
                Path(path).unlink()
            except Exception as exc:
                warnings.append(f'Could not remove integrated/deleted external subtitle {path}: {exc}')
    cache_output = dict(plan['cache'])
    for output_index, entry in enumerate(plan['ordered']['subtitle']):
        if output_index not in cache_output and entry['index'] in cached_original:
            cache_output[output_index] = cached_original[entry['index']]
    return dict(edited=str(media), operation='single_remux', warnings=warnings,
                subtitle_color=request.subtitle_color.upper(), colored_subtitles=changed,
                cached_inputs=cached_count, _color_cache=cache_output)


def publish_color_cache(media: Path, texts: dict) -> None:
    """Publish after normal edit invalidation, once for the whole media."""
    from app.subtitle_cache import invalidate_media, TextSubtitle, publish_track
    from app.subtitle_cache_worker import text_track_manifest
    try:
        invalidate_media(str(media))
        signature, tracks = text_track_manifest(media)
        expected = {(t['source'], t['type_index'], t['external_path']) for t in tracks}
        for track in tracks:
            if track['source'] == 'embedded' and track['type_index'] in texts:
                publish_track(str(media), signature, expected, TextSubtitle('embedded', track['type_index'], '', track['codec'], texts[track['type_index']]))
        if text_track_manifest(media)[0] != signature:
            invalidate_media(str(media))
            raise RuntimeError('Media changed during color cache publication')
    except Exception:
        logging.getLogger('uvicorn.error').exception('subtitle_color event=cache_publish_failed path=%s', media)
