"""Disposable integration harness: synthetic media + SQLite, never the real catalog.

Run inside the application image without production mounts or database credentials.
"""
import ast
import contextlib
import importlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = Path(tempfile.mkdtemp(prefix='vse-review-fixture-'))
os.environ['MEDIA_STREAM_DIR'] = str(TEMP / 'buffers')
os.environ['MEDIA_REVIEW_AUDIO_STAGE'] = str(TEMP / 'staged')
MEDIA = TEMP / 'fixture.mkv'
APP = FastAPI()


def probe(path):
    return json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]))


def authorized(path):
    if Path(path).resolve() != MEDIA:
        raise HTTPException(403,'Fixture path only')
    return MEDIA


def extract(filename,names,scope):
    tree=ast.parse((ROOT/filename).read_text())
    nodes=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.ClassDef)) and node.name in names]
    for node in nodes:
        node.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),filename,'exec'),scope)


def module(name,**values):
    result=types.ModuleType(name)
    result.__dict__.update(values)
    sys.modules[name]=result
    return result


class Database:
    def __init__(self):
        self.db=sqlite3.connect(TEMP/'stages.sqlite', timeout=30)
        self.db.row_factory=sqlite3.Row
    def execute(self,sql,args=()):
        if 'pg_advisory' in sql or sql.startswith('ALTER TABLE'):
            return self.db.execute('SELECT 1')
        return self.db.execute(sql.replace(' FOR UPDATE',''),args)


@contextlib.contextmanager
def connection():
    db=Database()
    try:
        yield db
        db.db.commit()
    finally:
        db.db.close()


with connection() as db:
    db.execute('CREATE TABLE task_queue(id INTEGER PRIMARY KEY,status TEXT,error TEXT)')
    db.execute('CREATE TABLE plex_media(path TEXT,library_key TEXT,show_title TEXT)')
    db.execute('INSERT INTO plex_media VALUES(?,?,?)',(str(MEDIA),'fixture','Fixture'))

JOBS={}
def enqueue(kind,payload,label='',**kwargs):
    identity=len(JOBS)+1
    JOBS[identity]=(kind,payload)
    with connection() as db:db.execute('INSERT INTO task_queue(id,status) VALUES(?,?)',(identity,'pending'))
    return {'id':identity}


module('app.v11',connection=connection)
module('app.v28',authorized_import_file=authorized)
module('app.v2',probe=probe)
module('app.v86',assert_media_editable=lambda path:None)
module('app.v13',matroska_tracks=lambda path:{'audio':[], 'subtitle':[]})
module('app.v65',TASK_HANDLERS={},enqueue=enqueue,update_progress=lambda *args:None,queue_shutdown=threading.Event())
scope={'Path':Path,'subprocess':subprocess,'HTTPException':HTTPException,'external_subtitles':lambda path:[],
       'TEXT_SUBTITLE_CODECS':{'subrip','srt','ass','webvtt'}}
extract('app/v83.py',{'_aac_browser_safe','_review_plan','_subtitle_file'},scope)
module('app.v83',app=APP,_review_metadata=probe,_review_plan=scope['_review_plan'],
       _subtitle_file=scope['_subtitle_file'],TEXT_SUBTITLE_CODECS=scope['TEXT_SUBTITLE_CODECS'])
# Exercise the playback conversion using a complete extracted SRT without
# importing the production cache/database/scheduler into this isolated harness.
def cached_subtitle(media, source, index, external_path='', metadata=None):
    selected = str(media) if source == 'embedded' else external_path
    text = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', selected,
                                    '-map', f'0:s:{index}' if source == 'embedded' else '0:0',
                                    '-f', 'srt', 'pipe:1']).decode()
    return types.SimpleNamespace(text=text)
module('app.subtitle_cache_worker', cached_or_extract_track=cached_subtitle)
playback=importlib.import_module('app.review_playback')
audio=importlib.import_module('app.review_audio')
audio.initialize_audio_stages()

class ExternalSubtitleChange(BaseModel):
    path:str
    embed:bool=False
    language:str=''
    region:str=''
    title:str=''
    forced:bool=False

editor={'Path':Path,'BaseModel':BaseModel,'Field':Field,'Literal':Literal,'ExternalSubtitleChange':ExternalSubtitleChange,
        'authorized_file':authorized,'probe':probe,'os':os,'uuid':uuid,'json':json,'subprocess':subprocess,'HTTPException':HTTPException,
        'logger':logging.getLogger('test'),'make_language':lambda language,region:'-'.join(filter(None,[language,region])),
        'checked_external':lambda source,path:Path(path)}
extract('app/v7.py',{'TrackChange','OrderItem','AudioCompatibility','ReorderEditRequest','disposition_flags','key_for','persist_remux_language_tags','_reorder_edit_impl'},editor)
DRAFT=[]
class DraftOperation(BaseModel):
    path:str
    operation:dict
def add_operation(session,request):
    DRAFT.append(request.operation)
    return {'dirty':True}
module('app.v79',_tv_edit_session=lambda identity:{'status':'open','show_id':'fixture:Fixture'},
       TvEditOperationRequest=DraftOperation,add_tv_edit_operation=add_operation)

def stream_details(path):
    result=[]
    for kind in ('audio','subtitle'):
        for index,stream in enumerate(s for s in probe(path)['streams'] if s['codec_type']==kind):
            result.append({'codec_type':kind,'type_index':index,'language':stream.get('tags',{}).get('language',''),
                           'title':stream.get('tags',{}).get('title',''), 'default':bool(stream.get('disposition',{}).get('default')),
                           'forced':bool(stream.get('disposition',{}).get('forced'))})
    return {'streams':result,'external_subtitles':[]}
draft_scope={'media_details_with_ietf':stream_details}
extract('app/v79.py',{'consolidated_tv_edit'},draft_scope)


@APP.post('/test/draft/{action}')
def draft_action(action:str):
    if action=='discard':
        audio.release_draft_audio('fixture-draft')
    else:
        edit,_=draft_scope['consolidated_tv_edit'](str(MEDIA),DRAFT)
        editor['_reorder_edit_impl'](editor['ReorderEditRequest'].model_validate(edit))
    DRAFT.clear()
    return {'done':True}


@APP.get('/test/fixture')
def fixture():return {'path':str(MEDIA),'size':MEDIA.stat().st_size,'streams':probe(MEDIA)['streams']}


@APP.get('/test/resources')
def resources():
    return {'sessions':[{'done':s.get('done'), 'produced':s['produced'], 'position':s['position'],
                         'buffer_duration':s['buffer_duration'], 'error':s['error']}
                        for s in playback.SESSIONS.values()],
            'buffer_directories':len(list(playback.ROOT.iterdir())), 'staged_directories':len(list(audio.ROOT.iterdir()))}


@APP.post('/test/touch')
def touch():
    os.utime(MEDIA, None)
    return {'changed':True}


@APP.post('/test/run/{identity}')
def run_job(identity:int):
    kind,payload=JOBS[identity]
    if kind=='review_audio_prepare':result=audio.process_audio(identity,payload)
    else:result=editor['_reorder_edit_impl'](editor['ReorderEditRequest'].model_validate(payload['edit']))
    with connection() as db:db.execute("UPDATE task_queue SET status='succeeded' WHERE id=?",(identity,))
    return result


@APP.get('/test/player')
def page():
    from fastapi.responses import HTMLResponse
    content=f'''<html><head><link rel="stylesheet" href="/test/style"></head><body style="background:#111;color:white">
    <div id="selected-file">Synthetic media</div><button id="review-media">Review</button><dialog id="media-review-dialog"></dialog>
    <div id="stream-content"><div class="stream-row" data-key="embedded:audio:0" data-codec-type="audio" data-type-index="0" data-codec="aac"><input name="language" value="en"><input name="title" value="AAC"><input type="radio" name="default-audio" value="embedded:audio:0" checked></div>
    <div class="stream-row" data-key="embedded:audio:1" data-codec-type="audio" data-type-index="1" data-codec="ac3"><input name="language" value="pt"><input name="title" value="AC3"></div>
    <div class="stream-row" data-key="embedded:subtitle:0" data-codec-type="subtitle" data-type-index="0" data-codec="subrip"><input name="language" value="en"><input name="title" value="Fixture"></div></div>
    <script>window.state={{selectedPath:{json.dumps(str(MEDIA))}}};window.toast=console.log;</script><script src="/test/script"></script></body></html>'''
    return HTMLResponse(content)


@APP.get('/test/script')
def script():
    from fastapi.responses import FileResponse
    return FileResponse(ROOT/'app/static/review-player.js',media_type='text/javascript')


@APP.get('/test/style')
def style():
    from fastapi.responses import FileResponse
    return FileResponse(ROOT/'app/static/review-player.css',media_type='text/css')


@APP.get('/api/v83/media-review/staged-subtitles')
def subtitles(path:str):return {'items':[]}


if __name__=='__main__':
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=24:duration=90',
      '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=90','-f','lavfi','-i','sine=frequency=660:sample_rate=48000:duration=90',
      '-i',str(ROOT/'scripts/fixtures/review.srt'),'-map','0:v','-map','1:a','-map','2:a','-map','3:s',
      '-c:v','libx264','-threads','2','-preset','veryfast','-g','144','-sc_threshold','0','-bf','3',
      '-c:a:0','aac','-c:a:1','ac3','-c:s','srt','-output_ts_offset','5','-y',str(MEDIA)],check=True)
    uvicorn.run(APP,host='0.0.0.0',port=18383)
