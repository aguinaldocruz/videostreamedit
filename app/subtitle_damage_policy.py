"""Offline Unicode-aware subtitle damage signals shared by inspection/reports.

Valid letters and punctuation are safe regardless of track metadata. This
covers all Setup common languages without requiring language identification,
downloading dictionaries, or rejecting legitimate multilingual dialogue.
"""
import html
import re
import unicodedata
from dataclasses import dataclass

from app.subtitle_html import PRESENTATION_TAGS, TAG


def _continuations() -> str:
    # UTF-8 continuation bytes misread as Latin-1 or Windows-1252. An ordinary
    # letter after Ã/Â is NOT such a byte: NÃO, Ângela, and ÂNIMO are valid.
    chars = {chr(value) for value in range(0x80, 0xC0)}
    for value in range(0x80, 0xC0):
        try:
            chars.add(bytes([value]).decode('cp1252'))
        except UnicodeDecodeError:
            pass
    return '[' + re.escape(''.join(sorted(chars))) + ']'


_CONT = _continuations()
MOJIBAKE = re.compile(r'(?:[ÃÂ]' + _CONT + r'|â' + _CONT + r'{2})')


def has_mojibake(text: str) -> bool:
    return bool(MOJIBAKE.search(text))


def language_punctuation_only(text: str) -> bool:
    """Punctuation alone is insufficient evidence of damaged text.

    Low language-confidence detection remains a separate concern. Hidden
    controls and U+FFFD are deliberately not part of this allowance.
    """
    return bool(text) and all(unicodedata.category(c).startswith('P') for c in text)


OCR_REASON = 'Likely OCR gibberish (isolated letters)'
_TIMING = re.compile(r'^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->')
_ASS_STYLE = re.compile(r'\{\\[^}]*\}')
_LINK = re.compile(r'(?:https?://|www\.)\S+|\b[\w.+-]{1,64}@[\w.-]{1,255}\.[\w-]{1,63}\b', re.IGNORECASE)
# Whole alphabetic tokens only: 43m32s, 1h07m47s, IDs and underscored
# filenames are not isolated m/s/h letters or pieces of broken dialogue.
_WORDS = re.compile(r"(?<!\w)[^\W\d_]+(?:['’][^\W\d_]+)*(?!\w)", re.UNICODE)
_WORD_GAP = re.compile(r'[ \t\u00a0]+\Z')
_GRAMMATICAL_LETTERS = frozenset({'a', 'à', 'e', 'é', 'i', 'o', 'y'})


def visible_dialogue(line: str) -> str:
    """Analysis copy only; never rewrite cached text or remove literal <John>."""
    line = TAG.sub(lambda match: ('\n' if match.group(2).lower() == 'br' else '')
                   if match.group(2).lower() in PRESENTATION_TAGS else match.group(0), line)
    line = _ASS_STYLE.sub('', line)
    line = html.unescape(line)
    return unicodedata.normalize('NFC', _LINK.sub(' ', line))


@dataclass(frozen=True)
class IsolatedLetterStats:
    words: int = 0
    isolated: int = 0
    fragmented: int = 0
    unusual: int = 0
    symbols: int = 0
    printable: int = 0

    @property
    def ratio(self) -> float:
        return self.isolated / self.words if self.words else 0.0

    @property
    def suspicious(self) -> bool:
        spaced_fragments = self.ratio >= .35 and self.fragmented >= 8
        symbol_rich = self.ratio >= .30 and self.unusual >= 8 and self.symbol_ratio >= .25
        return self.words >= 12 and self.isolated >= 8 and (spaced_fragments or symbol_rich)

    @property
    def symbol_ratio(self) -> float:
        return self.symbols / self.printable if self.printable else 0.0


def isolated_letter_stats(text: str) -> IsolatedLetterStats:
    """Require fragmented words, not styling, punctuation, credits or grammar.

    A fragment run is at least three whitespace-separated single letters,
    including two that are not usual one-letter grammatical words. Dotted
    initials and hyphenated spelling are not such runs. Symbol-rich corruption
    can also qualify, so OCR that inserts punctuation between letters is not
    hidden by the whitespace-run safeguard. Counts always use the complete
    supplied dialogue; the report uses this same helper per line.
    """
    words = isolated = fragmented = unusual_total = symbols = printable = 0
    for original in text.splitlines():
        if original.strip().isdigit() or _TIMING.match(original):
            continue
        for line in visible_dialogue(original).splitlines():
            for char in line:
                if char.isprintable() and not char.isspace():
                    printable += 1
                    symbols += unicodedata.category(char)[0] in {'P', 'S'}
            run = unusual = 0
            previous_end = None
            for match in _WORDS.finditer(line):
                token = match.group(0)
                words += 1
                single = len(token) == 1
                isolated += single
                initial = token.isupper() and line[match.end():match.end() + 1] == '.'
                hyphenated = line[max(0, match.start() - 1):match.start()] in {'-', '‐', '‑'} or (
                    line[match.end():match.end() + 1] in {'-', '‐', '‑'})
                # Ideographs and uncased letters can legitimately be whole
                # one-character words. Do not infer OCR from those scripts.
                cased_letter = single and unicodedata.category(token) in {'Ll', 'Lu', 'Lt'}
                unusual_letter = cased_letter and token.casefold() not in _GRAMMATICAL_LETTERS and not initial and not hyphenated
                unusual_total += unusual_letter
                contiguous = previous_end is not None and _WORD_GAP.fullmatch(line[previous_end:match.start()])
                if not single or not contiguous:
                    if run >= 3 and unusual >= 2:
                        fragmented += run
                    run = unusual = 0
                if single:
                    run += 1
                    unusual += unusual_letter
                previous_end = match.end()
            if run >= 3 and unusual >= 2:
                fragmented += run
    return IsolatedLetterStats(words, isolated, fragmented, unusual_total, symbols, printable)
