"""Translate cached SRT timestamps without changing dialogue or cue structure.

FFmpeg's SRT exports use a timeline relative to the container start. Native
Matroska passthrough keeps physical packet timestamps instead. Replacements
must restore that origin, and verification must compare the same timeline.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


_TIMING = re.compile(r'^(\s*)(\d{1,3}):(\d{2}):(\d{2})([,.])(\d{3})(\s*-->\s*)'
                     r'(\d{1,3}):(\d{2}):(\d{2})([,.])(\d{3})([^\r\n]*)$', re.M)


def container_start_ms(data: dict) -> int:
    value = (data.get('format') or {}).get('start_time')
    if value in (None, '', 'N/A'):
        return 0
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        return int((number * 1000).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise RuntimeError('Cannot establish subtitle timeline origin; original retained') from exc


def shift_srt(text: str, milliseconds: int) -> str:
    if not milliseconds:
        return text

    def timestamp(h, m, s, ms, separator):
        total = ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms) + milliseconds
        if total < 0:
            # mkvmerge drops negative-start cues. Never silently clip or lose
            # one; this unusual source needs a separately reviewed repair.
            raise RuntimeError('Subtitle timeline would contain a negative cue; original retained')
        seconds, fraction = divmod(total, 1000)
        minutes, second = divmod(seconds, 60)
        hour, minute = divmod(minutes, 60)
        return f'{hour:02d}:{minute:02d}:{second:02d}{separator}{fraction:03d}'

    def cue(match):
        prefix, h1, m1, s1, sep1, ms1, arrow, h2, m2, s2, sep2, ms2, suffix = match.groups()
        return (prefix + timestamp(h1, m1, s1, ms1, sep1) + arrow
                + timestamp(h2, m2, s2, ms2, sep2) + suffix)

    return _TIMING.sub(cue, text)


def native_shift_ms(identified: dict | None) -> int:
    """mkvmerge shifts a Matroska input's negative packets up to zero.

    FFprobe can also report a negative start due solely to CodecDelay, which
    does NOT mean Matroska has negative blocks. Use native minimum timestamps,
    never infer this shift from FFprobe's format start alone.
    """
    minimum = min((int(t['properties'].get('minimum_timestamp') or 0)
                   for t in (identified or {}).get('tracks', [])), default=0)
    return int((Decimal(max(0, -minimum)) / 1000000).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def set_cached_timeline(entry: dict, data: dict, native: bool, identified: dict | None = None) -> None:
    origin = container_start_ms(data) + native_shift_ms(identified) if native else 0
    # Fail before a media-sized write if mkvmerge would drop a selected cue.
    shift_srt(entry['cleaned'], origin)
    entry['text_origin_ms'] = origin
    entry['offset'] = origin / 1000
