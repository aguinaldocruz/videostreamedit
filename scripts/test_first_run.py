"""Run with empty CONFIG_DIR/DATABASE_URL and no network; no database required."""
import asyncio
from pathlib import Path
from starlette.requests import Request
from starlette.responses import Response
from app import v86, v2, web_assets

assert not v86.app.router.on_startup
assert not v86.app.router.on_shutdown
assert v2.database_bootstrap_status()['bootstrap']


async def check():
    async def fallback(request):
        return Response('allowed')
    for path,expected in [('/',200),('/assets/bootstrap.js',200),('/assets/bootstrap.css',200),
                          ('/api/health',200),('/api/bootstrap/status',200),('/api/movies',503),('/api/v65/queue',503)]:
        request=Request({'type':'http','method':'GET','path':path,'query_string':b'','headers':[]})
        response=await web_assets.current_web_assets(request,fallback)
        assert response.status_code==expected, (path,response.status_code)
    assert not list(Path(v2.CONFIG_DIR).glob('*.db'))
asyncio.run(check())
print('PASS: network-free first access, wizard assets, blocked operational APIs, no SQLite store or workers')
