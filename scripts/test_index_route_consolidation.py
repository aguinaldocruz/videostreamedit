"""Index entry points use the canonical queue; no catalog/media writes."""
from unittest.mock import patch
from pathlib import Path
from app import db_bootstrap
from app import v86, v38, v39, v52, v54, v80, v82

with patch.object(v80,'rebuild_index_queue',return_value={'id':17}) as rebuild:
    assert v39.rebuild_movie_index() == {'id':17}
    assert v52.rebuild_all_movie_indexes() == {'id':17}
    assert v54.rebuild_job('subtitles') == {'id':17}
    assert [c.args for c in rebuild.call_args_list] == [('core',),('core',),('subtitles',)]
with patch.object(v80,'request_media_indexes',return_value=1) as request:
    assert v38.invalidate_movie_stream_filter(v38.MovieStreamIndexInvalidate(path='/fixture/movie.mkv'))['invalidated']
    assert v54.invalidate_indexes(v38.MovieStreamIndexInvalidate(path='/fixture/movie.mkv'))['queued']==1
    assert all(c.kwargs.get('defer_detection') for c in request.call_args_list)
with patch.object(v82,'unified_core_index') as index:
    v54.index_core({'path':'/fixture/movie.mkv'})
    index.assert_called_once_with({'path':'/fixture/movie.mkv'})
assert v38._value_groups([{'stream_type':'video','language':'en'},{'stream_type':'subtitle','language':'pt'}],'language') == {'all':['pt'],'audio':[],'subtitle':['pt']}
for filename in ('v38.py','v39.py','v51.py','v52.py','v54.py','v79.py'):
    source=(Path('app')/filename).read_text()
    assert 'movie_stream_index_value' not in source
    assert 'tv_stream_index_value' not in source
    assert '_run_index' not in source
assert not any(h.__name__ in {'schedule_extended_subtitle_index_migration','preserve_existing_preview_samples'} for h in v86.app.router.on_startup)
print('PASS: canonical queue routes, processor, delayed detection, unsupported filter types, obsolete workers/migrations removed')
