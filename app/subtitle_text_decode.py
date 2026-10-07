"""Conservative decoding of complete SRT payloads; never edit source files."""

from __future__ import annotations

import re
from dataclasses import dataclass


_TIMING = re.compile(r"(?m)^\s*\d{1,3}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,3}:\d{2}:\d{2}[,.]\d{3}")
_UTF8_SEQUENCE = re.compile(
    rb"(?:[\xc2-\xdf][\x80-\xbf]|\xe0[\xa0-\xbf][\x80-\xbf]|"
    rb"[\xe1-\xec\xee-\xef][\x80-\xbf]{2}|\xed[\x80-\x9f][\x80-\xbf]|"
    rb"\xf0[\x90-\xbf][\x80-\xbf]{2}|[\xf1-\xf3][\x80-\xbf]{3}|"
    rb"\xf4[\x80-\x8f][\x80-\xbf]{2})"
)


@dataclass(frozen=True)
class DecodedSubtitle:
    text: str
    encoding: str
    status: str


def decode_complete_srt(payload: bytes) -> DecodedSubtitle:
    """Accept UTF encodings or strict, reversible Windows-1252 with SRT cues.

    Legacy decoding is a cache interpretation, not a repair. Undefined bytes,
    control characters, replacement glyphs and non-SRT content are refused.
    Genuine empty/BOM-only tracks are distinguishable from failed extraction.
    """
    encoding = "UTF-8"
    if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encoding, codec = "UTF-32", "utf-32"
    elif payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding, codec = "UTF-16", "utf-16"
    else:
        codec = "utf-8-sig"
    try:
        text = payload.decode(codec, errors="strict")
    except UnicodeError as exc:
        if codec != "utf-8-sig":
            raise RuntimeError(f"Invalid {encoding} subtitle bytes; manual review required") from exc
        try:
            text = payload.decode("cp1252", errors="strict")
        except UnicodeError as legacy_exc:
            raise RuntimeError("Subtitle encoding cannot be decoded safely as UTF-8 or Windows-1252; manual review required") from legacy_exc
        if text.encode("cp1252", errors="strict") != payload:
            raise RuntimeError("Subtitle legacy decoding was not reversible; manual review required")
        # Mixing valid UTF-8 sequences with invalid bytes is ambiguous. Do not
        # reinterpret the entire payload as legacy text and introduce mojibake.
        # A three/four-byte prefix is not a complete UTF-8 character. For
        # example cp1252 'á… ' contains E1 85 20 and is not mixed UTF-8.
        if _UTF8_SEQUENCE.search(payload):
            raise RuntimeError("Subtitle mixes UTF-8 and invalid bytes; manual review required")
        encoding = "Windows-1252 (inferred)"
    if any((ord(char) < 32 and char not in "\r\n\t") or 127 <= ord(char) < 160
           or char == "\ufffd" for char in text):
        raise RuntimeError("Subtitle contains undecodable/control characters; manual review required")
    if not text.strip("\ufeff \t\r\n"):
        return DecodedSubtitle("", encoding, "empty")
    if not _TIMING.search(text):
        raise RuntimeError("Subtitle has no valid SRT cue timestamps; manual replacement required")
    return DecodedSubtitle(text, encoding, "ready")
