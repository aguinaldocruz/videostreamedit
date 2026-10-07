"""Bounded literal/word rules on SRT dialogue, never cue numbers or timing.

Keep existing HTML/ASS presentation tags intact too. These are user-defined
substitutions, not an encoding guess or an unsupervised mojibake repair.
"""
from __future__ import annotations

import re

from fastapi import HTTPException

from app.subtitle_autofix import RuleFields
from app.subtitle_cleanup import TIMING, validate_srt

MAX_SUBTITLE_BYTES = 32 * 1024**2
MARKUP = re.compile(r'(<[^>\r\n]*>|\{\\[^}\r\n]*\})')


def _timings(text: str) -> list[tuple]:
    validate_srt(text, text)
    return [tuple(int(value) for value in match.groups())
            for line in text.replace('\r\n', '\n').replace('\r', '\n').splitlines()
            if (match := TIMING.fullmatch(line))]


def validate_replacement(original: str, fixed: str) -> None:
    if len(fixed.encode('utf-8')) > MAX_SUBTITLE_BYTES:
        raise HTTPException(422, 'Corrected subtitle exceeds the 32 MiB safety limit')
    if _timings(original) != _timings(fixed):
        raise HTTPException(422, 'Autofix must preserve every cue and timestamp; original retained')


def fix_srt(rule: RuleFields, text: str) -> dict:
    if len(text.encode('utf-8')) > MAX_SUBTITLE_BYTES:
        raise HTTPException(422, 'Subtitle exceeds the 32 MiB safety limit')
    _timings(text)
    lines = text.splitlines(keepends=True)
    # Track payload positions after a timestamp. Numeric dialogue remains
    # dialogue; only sequence numbers before a timestamp are protected.
    payload = False
    parts = []
    mutable = []
    for line in lines:
        value = line.rstrip('\r\n')
        if TIMING.fullmatch(value):
            payload = True
            parts.append(line)
        elif not value.strip():
            payload = False
            parts.append(line)
        elif payload:
            for index, part in enumerate(MARKUP.split(line)):
                if index % 2 == 0:
                    mutable.append(len(parts))
                parts.append(part)
        else:
            parts.append(line)
    counts = []
    size = len(text.encode('utf-8'))
    for replacement in rule.replacements:
        pattern = (re.compile(r'(?<!\w)' + re.escape(replacement.from_text) + r'(?!\w)')
                   if replacement.match == 'word' else None)
        count = 0
        for index in mutable:
            before = parts[index]
            matches = sum(1 for _ in pattern.finditer(before)) if pattern else before.count(replacement.from_text)
            if not matches:
                continue
            size += matches * (len(replacement.to_text.encode('utf-8')) - len(replacement.from_text.encode('utf-8')))
            if size > MAX_SUBTITLE_BYTES:
                raise HTTPException(422, 'This rule expands the subtitle too much; review its replacements')
            parts[index] = (pattern.sub(lambda _match: replacement.to_text, before) if pattern
                            else before.replace(replacement.from_text, replacement.to_text))
            count += matches
        counts.append(count)
    fixed = ''.join(parts)
    validate_replacement(text, fixed)
    return dict(text=fixed, changed=fixed != text, replacement_count=sum(counts), counts=counts)
