"""Read-only dictionary tokens, not an automatic subtitle correction rule."""
from __future__ import annotations

import unicodedata
from collections import Counter

from app.subtitle_damage_policy import visible_dialogue


def replacement_words(text: str) -> Counter:
    """Return whole visible words with U+FFFD, preserving case and accents.

    Quotes/dashes are retained only inside a word. Punctuation-only U+FFFD
    tokens are retained too: a completely missing word is still useful evidence.
    Markup, links, timestamps and cue numbering never become dictionary words.
    """
    words = Counter()
    for line in text.splitlines():
        if '\ufffd' not in line and '&#' not in line:
            continue
        dialogue = visible_dialogue(line)
        chars = []
        def word_char(char):
            return char == '\ufffd' or unicodedata.category(char)[0] in 'LMN'
        def flush():
            value = ''.join(chars)
            if '\ufffd' in value:
                words[value] += 1
            chars.clear()
        for index, char in enumerate(dialogue):
            if word_char(char) or (char in "'’-" and chars and index + 1 < len(dialogue) and word_char(dialogue[index + 1])):
                chars.append(char)
            else:
                flush()
        flush()
    return words
