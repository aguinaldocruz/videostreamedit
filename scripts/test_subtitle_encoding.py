"""Encoding-only validation and damaged-word tokens; no catalog media writes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.subtitle_encoding import LEGACY_ENCODING, validate_encoding_fix
from app.subtitle_replacement_words import replacement_words

srt = '1\n00:00:01,000 --> 00:00:02,000\n<i>Fez café?</i> Não, amanhã.\n'
validate_encoding_fix(srt, LEGACY_ENCODING)
for text, encoding in [(srt, 'UTF-8'), (srt.replace('café', 'caf�'), LEGACY_ENCODING),
                       (srt.replace('café', 'cafÃ©'), LEGACY_ENCODING),
                       (srt + '\x00', LEGACY_ENCODING), (srt + '\x81', LEGACY_ENCODING),
                       (srt.replace('café', '漢字'), LEGACY_ENCODING),
                       (srt.replace('café', 'only ASCII').replace('Não, amanhã.', 'no'), LEGACY_ENCODING),
                       ('1\nno timestamps\ncafé\n', LEGACY_ENCODING), ('', LEGACY_ENCODING)]:
    try:
        validate_encoding_fix(text, encoding)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError((text, encoding))
text = '<font color="red">caf�, a��o CAF�! d’�gua guarda-ch�va �</font>\n<i>caf�</i>\nwww.bad�.test\nNÃO é bom.\nCafe\u0301� &#65533;\n'
words = replacement_words(text)
assert words == {'caf�': 2, 'a��o': 1, 'CAF�': 1, 'd’�gua': 1, 'guarda-ch�va': 1, '�': 2, 'Café�': 1}, words
assert not replacement_words('NÃO café <font color="red">bom</font>')
assert not replacement_words('<font color="caf�">Normal</font>\n<a href="caf�">Fine</a>')
print('PASS: reversible legacy-only normalization, no guessing replacement/control/mixed/empty/malformed text; complete words, case/accents, presentation/link exclusion')
