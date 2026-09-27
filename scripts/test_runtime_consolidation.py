"""No startup or media writes; exercise asset responses and route ownership."""
import asyncio
from unittest.mock import patch
from starlette.requests import Request
from starlette.responses import Response
from app import db_bootstrap
from app import v86, v80, v81, v82, web_assets

hooks=v86.app.router.on_startup
names=[h.__name__ for h in hooks]
assert names[0]=='initialize_postgres_workflow'
assert names.index('initialize_unified_stream_index') < names.index('initialize_index_queues')
services={'initialize_preflight_dispatcher','start_v82_services','initialize_backup'}
assert all(n in services for n in names[-3:])
route=next(r for r in v86.app.routes if getattr(r,'path','')=='/api/v79/language-detection/flush')
assert route.endpoint is v80.flush_language_detection
with patch.object(v80,'flush_deferred_language_detection',return_value={'flushed':True}) as flush:
    assert route.endpoint('/fixture.mkv')=={'flushed':True}
    flush.assert_called_once_with('/fixture.mkv')
assert len(v86.app.user_middleware)==2 # asset dispatch and gzip only
assert v86.app.router.on_shutdown[0] is v81.shutdown_performance_monitor
v80.index_shutdown.set()
try:
    with patch.object(v80.threading,'Thread',side_effect=AssertionError('Worker revived during shutdown')):
        v80.start_index_queue_workers()
finally:
    v80.index_shutdown.clear()
with patch.object(v82,'ensure_unified_index') as schema, patch.object(v82,'connection',side_effect=AssertionError('Retired migration ran')):
    v82.initialize_unified_stream_index()
    schema.assert_called_once()


async def check():
    async def fallback(request):
        return Response(status_code=404)
    async def get(path, etag=None):
        request=Request({'type':'http','method':'GET','path':path,'query_string':b'',
                         'headers':[(b'if-none-match',etag.encode())] if etag else []})
        return await web_assets.current_web_assets(request,fallback)
    for path in ['/','/assets/v19.js','/assets/v19.css']:
            first=await get(path)
            assert first.status_code==200, (path,first.status_code)
            if path.startswith('/assets/'):
                with patch.object(web_assets.Path if hasattr(web_assets,'Path') else type(web_assets.STATIC_DIR),'read_text',side_effect=AssertionError('Warm cache read disk')):
                    second=await get(path)
                    assert second.body==first.body
                    cached=await get(path,first.headers['etag'])
                    assert cached.status_code==304 and not cached.body
    assert (await get('/api/not-asset')).status_code==404
    assert any(getattr(r,'path','')=='/brand/{filename}' for r in v86.app.routes)
asyncio.run(check())
print('PASS: schema before workers, flush route safe, single asset owner, warm bundle cache, ETag/304, branding')
