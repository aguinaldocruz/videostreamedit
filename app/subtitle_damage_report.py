"""Read-only damage-report rankings and evidence from existing cached text."""
from __future__ import annotations

import re
from collections import Counter
from app.subtitle_damage_policy import MOJIBAKE, OCR_REASON, isolated_letter_stats

NOTES = {
    'Possible mojibake': 'Matches byte-compatible broken encoding sequences, such as Ã£ or â€™. Normal Unicode letters and punctuation (including NÃO and Ângela) are allowed. Context still matters: a subtitle may deliberately quote broken text.',
    'Replacement characters': 'U+FFFD (�) usually means decoding lost a character, but the symbol may already be present in the supplied subtitle.',
    'Control characters': 'Non-printing control characters other than tab/newline were found. The examples show their Unicode code points.',
    'Malformed SRT timing': 'Number-only lines exist, but no recognized SRT timestamps were found. A different subtitle format or unusual timestamp syntax can trigger this.',
    'Non-UTF-8 source bytes': 'The source required legacy-character decoding. This alone does not prove damage: correctly decoded Windows-1252 text may be perfectly readable.',
    'No recognizable text': 'At least 40 printable non-space characters contain very few letters. Signs, symbols, and deliberately nonverbal captions can resemble corruption.',
    OCR_REASON: 'Visible dialogue across the complete subtitle has at least 12 words and 8 single-letter words, plus either a 35% isolated-letter ratio with 8 letters in fragmented runs, or a 30% ratio with 8 unusual isolated letters and at least 25% symbols/punctuation. Formatting tags, links, letters inside numeric time notation, contractions and dotted initials are not OCR evidence. Normal one-letter words alone do not qualify. Spelled-out dialogue can still resemble OCR; examples are clues, not proof.',
    'Empty subtitle track (no cues)': 'The inspected text is empty. This can be an empty track or an extraction issue; the report alone cannot distinguish them.',
}
TIMING = re.compile(r'^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->')


def reason_matches(text: str, reason: str, source: str, encoding: str, classify) -> bool:
    # The frequent, simple rules match damage_kind exactly. Avoid computing
    # unrelated whole-track OCR statistics thousands of times per expansion.
    if reason == 'Possible mojibake':
        return bool(MOJIBAKE.search(text))
    if reason == 'Replacement characters':
        return '\ufffd' in text
    if reason == 'Non-UTF-8 source bytes':
        return source == 'embedded' and 'inferred' in (encoding or '')
    if reason == 'Empty subtitle track (no cues)':
        return not text.strip()
    return reason in reasons(classify(text))


def reasons(value: str) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in (value or '').split(' + ') if s.strip() and s.strip() != 'None'))


def report_tracks(items: list[dict]) -> list[dict]:
    tracks = {}
    for item in items:
        labels = {ep['path']: f"{item['title']} · {ep.get('episode', '')}" for ep in item.get('episodes', [])}
        for stream in item.get('streams', []):
            key = (stream['path'], stream['source'], stream['type_index'], stream['external_path'])
            tracks[key] = {**stream, 'label': labels.get(stream['path'], item['title'])}
    return list(tracks.values())


def reason_summary(items: list[dict]) -> dict:
    tracks = report_tracks(items)
    groups = {}
    for track in tracks:
        for reason in reasons(track['damage']):
            entry = groups.setdefault(reason, {'reason': reason, 'stream_count': 0, 'paths': set(),
                                               'note': NOTES.get(reason, 'Recorded inspection finding; review the source before deciding it is damaged.')})
            entry['stream_count'] += 1
            entry['paths'].add(track['path'])
    ranked = sorted(groups.values(), key=lambda r: (-r['stream_count'], r['reason']))
    return {'reason_count': len(ranked), 'stream_count': len(tracks),
            'reasons': [{'rank': n, 'reason': r['reason'], 'stream_count': r['stream_count'],
                         'media_count': len(r['paths']), 'note': r['note']}
                        for n, r in enumerate(ranked[:30], 1)]}


def visible(value: str, limit: int = 400) -> str:
    return ''.join(c if c.isprintable() else f'\\u{ord(c):04X}' for c in value[:limit])


def evidence_lines(text: str, reason: str, encoding: str = ''):
    """Evidence candidates, not a second corruption classifier.

    The caller separately reruns the existing damage_kind rule to confirm the
    recorded finding. Whole-track criteria must not imply each example is bad.
    """
    if reason in {'Possible mojibake', 'Replacement characters'}:
        # Search once through the text in C, then inspect only matching lines.
        # Walking every timestamp/dialogue line in Python is expensive for a
        # large report. Repeated tokens in one line are counted together.
        pattern = MOJIBAKE if reason == 'Possible mojibake' else re.compile('\ufffd')
        end = -1; counted_to = 0; number = 1
        for match in pattern.finditer(text):
            if match.start() < end:
                continue
            start = text.rfind('\n', 0, match.start()) + 1
            end = text.find('\n', match.start())
            if end < 0:
                end = len(text)
            number += text.count('\n', counted_to, start)
            counted_to = start
            line = text[start:end]
            arrow = text.rfind('-->', 0, start)
            cue = ''; timing = ''
            if arrow >= 0:
                time_start = text.rfind('\n', 0, arrow) + 1
                time_end = text.find('\n', arrow)
                candidate = text[time_start:time_end]
                if TIMING.match(candidate):
                    timing = candidate.strip()
                    previous = text[text.rfind('\n', 0, max(0, time_start - 1)) + 1:max(0, time_start - 1)].strip()
                    cue = previous if previous.isdigit() else ''
            context = {'line': number, 'cue': cue, 'timing': timing, 'text': visible(line)}
            for token, count in Counter(pattern.findall(line)).items():
                yield ('U+FFFD · �' if reason == 'Replacement characters' else visible(token)), count, context
        return
    cue = ''; timing = ''; first = None
    for number, line in enumerate(text.split('\n'), 1):
        if line.strip().isdigit():
            cue = line.strip(); timing = ''
        if TIMING.match(line):
            timing = line.strip()
        context = {'line': number, 'cue': cue, 'timing': timing, 'text': visible(line)}
        payload = bool(line.strip() and not line.strip().isdigit() and not TIMING.match(line))
        if payload and first is None:
            first = context
            if reason == 'Non-UTF-8 source bytes':
                break
        if reason == 'Control characters':
            for char, count in Counter(c for c in line if ord(c) < 32 and c not in '\t\r\n').items():
                yield f'U+{ord(char):04X}', count, context
        elif reason == OCR_REASON and payload:
            stats = isolated_letter_stats(line)
            fragment_evidence = stats.fragmented >= 3 or (stats.unusual >= 2 and stats.symbol_ratio >= .25)
            if stats.words >= 3 and stats.ratio >= .30 and fragment_evidence:
                yield visible(line.strip(), 240), 1, context
        elif reason == 'No recognizable text' and payload:
            printable = [c for c in line if c.isprintable() and not c.isspace()]
            suspicious = bool(printable) and sum(c.isalpha() for c in printable) < max(1, len(printable) / 12)
            if suspicious:
                yield visible(line.strip(), 240), 1, context
        elif reason == 'Malformed SRT timing' and '-->' in line:
            yield visible(line.strip(), 240), 1, context
    if reason == 'Non-UTF-8 source bytes':
        yield encoding or 'Encoding not recorded', 1, first or {'line': None, 'cue': '', 'timing': '', 'text': '(No dialogue available)'}
    elif reason == 'Empty subtitle track (no cues)':
        yield '(Empty subtitle)', 1, {'line': None, 'cue': '', 'timing': '', 'text': '(No cues)'}
    elif reason == 'Malformed SRT timing' and '-->' not in text:
        yield '(No SRT timestamp arrows)', 1, first or {'line': None, 'cue': '', 'timing': '', 'text': visible(text)}


class EvidenceRanking:
    def __init__(self, reason: str):
        self.reason = reason
        self.groups = {}
        self.cached = 0
        self.unreproduced = []

    def add(self, track: dict, text: str, encoding: str, current_reasons: list[str]):
        self.cached += 1
        if self.reason not in current_reasons:
            self.unreproduced.append(track)
            return
        for token, count, context in evidence_lines(text, self.reason, encoding):
            group = self.groups.setdefault(token, {'example': token, 'occurrences': 0, 'paths': set(), 'tracks': set(),
                                                   'locations': [], 'quickfix_paths': set()})
            group['occurrences'] += count
            group['paths'].add(track['path'])
            key = (track['path'], track['source'], track['type_index'], track['external_path'])
            group['tracks'].add(key)
            if (self.reason == 'Non-UTF-8 source bytes' and encoding == 'Windows-1252 (inferred)'
                    and track['source'] == 'embedded' and (track.get('codec') or '').casefold() in {'srt', 'subrip'}):
                group['quickfix_paths'].add(track['path'])
            # Three distinct streams provide context without returning full subtitles.
            if len(group['locations']) < 3 and not any(location['key'] == key for location in group['locations']):
                group['locations'].append({'key': key, **track, **context})

    def result(self, total: int) -> dict:
        ordered = sorted(self.groups.values(), key=lambda g: (-g['occurrences'], -len(g['tracks']), g['example']))
        return {'reason': self.reason, 'reported_streams': total, 'cached_streams': self.cached,
                'unavailable_streams': total - self.cached, 'unreproduced_count': len(self.unreproduced),
                'unreproduced': self.unreproduced[:30], 'distinct_examples': len(ordered),
                'examples': [{'rank': n, 'example': g['example'], 'occurrences': g['occurrences'],
                              'stream_count': len(g['tracks']), 'media_count': len(g['paths']),
                              'quickfix_media_count': len(g['quickfix_paths']),
                              'locations': [{k: v for k, v in location.items() if k != 'key'} for location in g['locations']]}
                             for n, g in enumerate(ordered[:30], 1)]}
