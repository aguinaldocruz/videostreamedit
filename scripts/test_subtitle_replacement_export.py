"""Dictionary exporter with isolated report/cache/media fixtures only."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.subtitle_cache import TextSubtitle

spec = importlib.util.spec_from_file_location('dictionary_export', ROOT/'scripts/export_subtitle_replacement_words.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

with tempfile.TemporaryDirectory(prefix='vse-dictionary-export-test-') as folder:
    root = Path(folder)
    movie, episode = root/'movie.mkv', root/'episode.mkv'
    movie.write_bytes(b'unchanged movie')
    episode.write_bytes(b'unchanged episode')
    before = movie.read_bytes(), episode.read_bytes()
    source_text = '1\n00:00:00,000 --> 00:00:01,000\n<i>caf�!</i> a��o CAF�\n' + 'Ordinary line\n'*10000 + 'Última palavra: amanh�.'
    def track(path, index):
        return dict(path=str(path),source='embedded',type_index=index,external_path='',codec='subrip',damage='Replacement characters')
    report = {'movies': {'items': [dict(title='Movie',streams=[track(movie,0),track(movie,1)]),
                                  dict(title='Missing',streams=[track(root/'missing.mkv',0)])]},
              'tv': {'items': [dict(title='Show',episodes=[dict(path=str(episode),episode='S01E01')],streams=[track(episode,0)])]}}
    def streams(path, _data):
        return [dict(source='embedded',type_index=i,external_path='',codec='subrip',language=language)
                for i,language in enumerate(['pt-BR','en'] if path==movie else ['pt-PT'])]
    def manifest(path, data):
        return 'current-signature',streams(path,data)
    def cached(path, signature):
        assert signature=='current-signature'
        return [TextSubtitle('embedded',s['type_index'],'','subrip',source_text if s['language']!='en' else '1\n00:00:00,000 --> 00:00:01,000\nTh�re. caf�!') for s in streams(Path(path),{})]
    def no_extract(_path, requested):
        assert requested==[], 'Valid cache triggered extraction'
        return {}
    output=root/'dictionary'
    with patch.object(exporter,'damaged_subtitle_report',side_effect=lambda kind:report[kind]), \
         patch.object(exporter,'probe',return_value={}),patch.object(exporter,'_streams',side_effect=streams), \
         patch.object(exporter,'text_track_manifest',side_effect=manifest), \
         patch.object(exporter,'get_valid_tracks',side_effect=cached),patch.object(exporter,'extract_missing',side_effect=no_extract):
        result=exporter.export(output)
        assert result['reported_streams']==4 and result['read_streams']==3 and result['failed_streams']==1
        assert result['cached_streams']==3 and result['extracted_streams']==0
        assert {item['language'] for item in result['files']}=={'pt-BR','pt-PT','en'}
        words=(output/'pt-BR.txt').read_text().splitlines()
        assert words==['amanh�','a��o','CAF�','caf�'], words
        assert (output/'pt-PT.txt').read_text().splitlines()==words
        refs=[json.loads(line) for line in (output/'references.jsonl').read_text().splitlines()]
        assert len(refs)==4 and sum(r['status']=='failed' for r in refs)==1
        assert next(r for r in refs if r.get('language')=='pt-PT')['label']=='Show · S01E01'
        assert 'amanh�' in next(r for r in refs if r.get('language')=='pt-BR')['words'], 'Full subtitle was truncated'
        try:
            exporter.export(output)
        except FileExistsError:
            pass
        else:
            raise AssertionError('Existing user dictionary was overwritten')
    assert (movie.read_bytes(),episode.read_bytes())==before
    raw='1\n00:00:01,000 --> 00:00:02,000\ncafé\n'.encode('cp1252')
    text,encoding=exporter.diagnostic_decode(raw)
    assert 'café' in text and '\ufffd' not in text
    text,encoding=exporter.diagnostic_decode(b'Invalid \xc3\x81 with \x81')
    assert '\ufffd' in text and 'introduced replacement' in encoding
print('PASS: full cached subtitles, language/region grouping, unique case-preserved words, source references/errors, no cache/media writes or unnecessary extraction, no export overwrite, explicit diagnostic decoding loss')
