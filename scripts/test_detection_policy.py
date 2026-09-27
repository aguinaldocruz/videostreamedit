"""Pure policy regression tests; no database, service, or media writes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.detection_policy import language_key, language_matches, voice_consensus

assert language_key('por') == ('pt', '')
assert language_key('pob') == ('pt', 'BR')
assert language_matches('pt-BR', 'pt')
assert language_matches('pt', 'por', 'PT')
assert not language_matches('pt-BR', 'pt', 'PT')
assert language_matches('en', 'eng')
assert not language_matches('en', 'und')
assert not language_matches('', 'en')
def samples(*codes):
    return [{'language': code, 'confidence': .95 if code else 0} for code in codes]
assert voice_consensus(samples('en','en','en','en'))[0] == 'en'
assert voice_consensus(samples('en','en','en','la'))[0] == 'en'
assert voice_consensus(samples('en','en','la','la'))[0] == ''
assert voice_consensus(samples('la','','',''))[0] == ''
assert voice_consensus(samples('en'))[0] == ''
assert voice_consensus(samples('en','en','en','la'))[1] < .8
print('PASS: regional/base language agreement, aliases, unknown language, sample conflicts, failures and confidence calibration')
