"""Encoding-only repair: preserve text, never guess a missing character."""
from __future__ import annotations

from app.subtitle_damage_policy import has_mojibake
from app.subtitle_text_decode import decode_complete_srt

LEGACY_ENCODING = 'Windows-1252 (inferred)'


def validate_encoding_fix(text: str, source_encoding: str) -> None:
    """Reconstruct and strictly re-decode the recorded reversible legacy bytes.

    This is deliberately not a general charset detector. Mixed UTF-8, missing
    characters, controls and broken encoding sequences require manual repair.
    The writer additionally binds this evidence to the current source/cache.
    """
    if source_encoding != LEGACY_ENCODING:
        raise ValueError('Only verified Windows-1252 source bytes support this quickfix; no encoding is guessed')
    if len(text.encode('utf-8')) > 32 * 1024**2:
        raise ValueError('Subtitle exceeds the 32 MiB encoding-review safety limit')
    try:
        decoded = decode_complete_srt(text.encode('cp1252', errors='strict'))
    except (UnicodeError, RuntimeError) as exc:
        raise ValueError(f'Encoding quickfix refused: {exc}. Use manual review or an explicit autofix rule.') from exc
    if decoded.encoding != LEGACY_ENCODING or decoded.text != text or decoded.status != 'ready':
        raise ValueError('Legacy source evidence is inconsistent or empty; refresh inspection before repair')
    if has_mojibake(text):
        raise ValueError('Encoding conversion alone would leave possible mojibake. Review a specific autofix rule instead.')
    # Validate the complete result, not just the timestamp sample used by the
    # cache decoder. Encoding changes cannot add/drop cues or presentation.
    from app.subtitle_cleanup import validate_srt
    validate_srt(text, text)
