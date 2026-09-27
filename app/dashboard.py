"""Read-only dashboard summaries; no catalog traversal or media probing."""
import threading
import time
from datetime import datetime, timezone, timedelta

from app.v11 import connection
from app.v84 import app
from fastapi import Query
from typing import Literal

_lock = threading.Lock()
_cached = None
_expires = 0.0


@app.get('/api/dashboard/overview')
def overview():
    global _cached, _expires
    if _cached is not None and time.monotonic() < _expires:
        return _cached
    if not _lock.acquire(blocking=False):
        if _cached is not None:
            return {**_cached, 'refreshing': True}
        with _lock:
            return _cached or {'loading': True}
    try:
        with connection() as db:
            groups = db.execute("""SELECT p.kind,p.library_key,p.library_name,p.show_title,
                count(*) AS total,coalesce(sum(p.size),0) AS bytes,
                sum(CASE WHEN i.path IS NOT NULL THEN 1 ELSE 0 END) AS indexed,
                sum(CASE WHEN coalesce(n.final_version,0)=1 OR coalesce(s.final_version,0)=1 THEN 1 ELSE 0 END) AS final,
                sum(CASE WHEN coalesce(n.reviewed,0)=1 OR coalesce(s.reviewed,0)=1 OR coalesce(n.final_version,0)=1 OR coalesce(s.final_version,0)=1 THEN 1 ELSE 0 END) AS reviewed,
                sum(CASE WHEN coalesce(n.plex_sync_change,0)=1 OR coalesce(s.plex_sync_change,0)=1 THEN 1 ELSE 0 END) AS changed
                FROM plex_media p
                LEFT JOIN media_stream_index_state i ON i.path=p.path
                LEFT JOIN media_notes n ON n.entity_type=CASE WHEN p.kind='movie' THEN 'movie' ELSE 'tv' END
                  AND n.entity_key=CASE WHEN p.kind='movie' THEN p.path ELSE 'episode:'||p.path END
                LEFT JOIN media_notes s ON p.kind='episode' AND s.entity_type='tv' AND s.entity_key=p.library_key||':'||p.show_title
                WHERE p.kind IN ('movie','episode')
                GROUP BY p.kind,p.library_key,p.library_name,p.show_title""").fetchall()
            tasks = db.execute("SELECT task_type AS type,status,count(*) AS count FROM task_queue GROUP BY task_type,status").fetchall()
            indexes = db.execute("SELECT job AS type,status,count(*) AS count FROM index_task_queue GROUP BY job,status").fetchall()
            preflight = db.execute("SELECT status,count(*) AS count FROM preflight_requests GROUP BY status").fetchall()
            paused = db.execute("SELECT value FROM task_queue_settings WHERE key='paused'").fetchone()
            index_paused = db.execute("SELECT job,paused FROM index_queue_settings").fetchall()
            cutoff = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
            recent = {}
            for table in ('task_queue','index_task_queue'):
                row=db.execute(f"SELECT count(*) AS n FROM {table} WHERE status='succeeded' AND CAST(finished_at AS TIMESTAMPTZ)>=CAST(? AS TIMESTAMPTZ)",(cutoff,)).fetchone()
                recent[table]=int(row['n'])
            running = db.execute("SELECT label,task_type,progress_message,progress_current,progress_total FROM task_queue WHERE status='running' ORDER BY id LIMIT 5").fetchall()
        collection={kind:{'total':0,'bytes':0,'indexed':0,'final':0,'reviewed':0,'changed':0} for kind in ('movies','tv')}
        shows=[];libraries=[]
        for row in groups:
            kind='movies' if row['kind']=='movie' else 'tv'
            value={key:int(row[key] or 0) for key in collection[kind]}
            for key in value: collection[kind][key]+=value[key]
            libraries.append({'kind':kind,'name':row['library_name'], 'total':value['total']})
            if kind=='tv':
                shows.append({**value,'id':f"{row['library_key']}:{row['show_title'] or 'Unknown show'}",'title':row['show_title'] or 'Unknown show','library':row['library_name']})
        collection['tv']['shows']=len(shows)
        collection['tv']['final_shows']=sum(s['final']==s['total'] for s in shows)
        partial=sorted((s for s in shows if 0<s['final']<s['total']),key=lambda s:(-(s['final']/s['total']),s['title']))[:8]
        _cached={'collection':collection,'continue':partial,'libraries':libraries,
                 'work':{'tasks':[dict(r) for r in tasks], 'indexes':[dict(r) for r in indexes],
                         'preflight':[dict(r) for r in preflight], 'paused':bool(paused and paused['value']=='1'),
                         'index_paused':{r['job']:bool(r['paused']) for r in index_paused},
                         'completed_hour':sum(recent.values()),'running':[dict(r) for r in running]},
                 'updated_at':datetime.now(timezone.utc).isoformat()}
        _expires=time.monotonic()+30
        return _cached
    finally:
        _lock.release()


@app.get('/api/dashboard/media')
def dashboard_media(kind: Literal['movies','tv'], status: Literal['final','reviewed','unreviewed','changed'], offset: int=Query(0,ge=0)):
    final="(coalesce(n.final_version,0)=1 OR coalesce(s.final_version,0)=1)"
    reviewed="(coalesce(n.reviewed,0)=1 OR coalesce(s.reviewed,0)=1)"
    where={'final':final,'reviewed':f'{reviewed} AND NOT {final}', 'unreviewed':f'NOT {reviewed} AND NOT {final}',
           'changed':'(coalesce(n.plex_sync_change,0)=1 OR coalesce(s.plex_sync_change,0)=1)'}[status]
    with connection() as db:
        rows=db.execute(f"""SELECT p.path,p.title,p.show_title,p.season_number,p.episode_number,p.library_name
            FROM plex_media p
            LEFT JOIN media_notes n ON n.entity_type=CASE WHEN p.kind='movie' THEN 'movie' ELSE 'tv' END
              AND n.entity_key=CASE WHEN p.kind='movie' THEN p.path ELSE 'episode:'||p.path END
            LEFT JOIN media_notes s ON p.kind='episode' AND s.entity_type='tv' AND s.entity_key=p.library_key||':'||p.show_title
            WHERE p.kind=? AND ({where}) ORDER BY p.show_title,p.season_number,p.episode_number,p.title,p.path LIMIT 101 OFFSET ?""",
            ('movie' if kind=='movies' else 'episode',offset)).fetchall()
    return {'items':[dict(r) for r in rows[:100]],'more':len(rows)>100}
