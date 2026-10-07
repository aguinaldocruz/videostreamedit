"""Real import fixtures. Disposable files only; no production DB writes."""
import ast
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.subtitle_text_decode import decode_complete_srt
from app.subtitle_html import has_removable_html

def module(name,**members):
    value=types.ModuleType(name);value.__dict__.update(members);sys.modules[name]=value;return value

def load(path,names,scope):
    nodes=[n for n in ast.parse((ROOT/path).read_text()).body
           if (isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names)
           or (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets))]
    for n in nodes:
        if isinstance(n,ast.FunctionDef):n.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),path,'exec'),scope)

class HttpError(Exception):
    def __init__(self,status_code,detail):
        super().__init__(detail);self.status_code=status_code;self.detail=detail

def probe(path):
    result=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,check=True,timeout=120)
    return json.loads(result.stdout)

scope=dict(Path=Path,os=os,shutil=shutil,tempfile=tempfile,subprocess=subprocess,re=re,json=json,uuid=uuid,ET=ET,dataclass=dataclass,
           has_removable_html=has_removable_html,decode_complete_srt=decode_complete_srt,HTTPException=HttpError,
           SRT_TIMING_LINE=re.compile(r'^\s*\d+:\d\d:\d\d[,.]\d+\s*-->'),MAX_TEXT_BYTES=32*1024**2)
for path,names in [('app/v2.py',{'make_language'}),('app/job_safety.py',{'stamp','copy_recovery'}),
                   ('app/subtitle_cache.py',{'TextSubtitle'}),('app/subtitle_cache_worker.py',{'_extract_track','_extract_embedded_batch'}),
                   ('app/v51.py',{'decode_external','subtitle_payload_lines','validate_cleaned_srt'}),('app/v7.py',{'disposition_flags'}),
                   ('app/v5.py',{'ISO_639_TO_1','canonical_language','plex_language_pair','split_tag'}),
                   ('app/matroska_remux.py',{'_semantic_snapshot','_TRACK_PROPERTIES','_GENERATED_TAGS'})]:load(path,names,scope)
module('fastapi',HTTPException=HttpError)
module('app.matroska_remux',ensure_front_track_headers=lambda _p: None,
       **{k:scope[k] for k in ('_semantic_snapshot','_TRACK_PROPERTIES')})
module('app.matroska_layout',safe_checkpoint=lambda _p, **_kw: None)
module('app.v7',disposition_flags=scope['disposition_flags'])
module('app.v43',legacy_language_code=lambda v:{'pt':'por','en':'eng','es':'spa'}.get(v,v or 'und'))
cache_calls=[];cached={};commands=[];admissions=[]
module('app.v51',cached_subtitle_text=lambda media,kind,index,**kw:cached.get(index),decode_external=scope['decode_external'],validate_cleaned_srt=scope['validate_cleaned_srt'])
module('app.subtitle_cache_worker',TEXT_CODECS={'ass','ssa','subrip','srt','webvtt','mov_text','text'},
       _extract_track=scope['_extract_track'],_extract_embedded_batch=scope['_extract_embedded_batch'],
       text_track_manifest=lambda media,metadata=None:('fixture',[dict(source='embedded',type_index=n,external_path='',codec=s['codec_name'])
           for n,s in enumerate(t for t in (metadata or probe(media))['streams'] if t['codec_type']=='subtitle')]))
module('app.subtitle_cache',TextSubtitle=scope['TextSubtitle'],invalidate_media=lambda *_a:None,publish_track=lambda path,sig,keys,track:cache_calls.append((path,track)))
@contextmanager
def output_space(path,required):
    admissions.append(required);yield
def run_write_command(command,_directory,timeout=3600):
    commands.append(command);result=subprocess.run(command,capture_output=True,timeout=timeout)
    if result.returncode:raise RuntimeError((result.stdout+result.stderr).decode('utf-8')[-3000:])
    return dict(returncode=result.returncode)
module('app.job_safety',stamp=scope['stamp'],copy_recovery=scope['copy_recovery'],output_space=output_space,run_write_command=run_write_command)
def external_subtitles(media):
    return [dict(path=str(p),codec=p.suffix[1:]) for p in sorted(media.parent.glob(media.stem+'.*.srt'))]
module('app.v5',external_subtitles=external_subtitles,split_tag=scope['split_tag'],plex_language_pair=scope['plex_language_pair'])
def request(**kw):
    values=dict(path='',tracks=[],external_subtitles=[],order=[],remove=[],default_audio='__preserve__',forced_audio='__preserve__',
                default_subtitle='__preserve__',forced_subtitle='__preserve__',clear_video_titles=False,audio_compatibility=[],final_version=None)
    values.update(kw);return types.SimpleNamespace(**values)
def track(kind,index,**kw):
    return types.SimpleNamespace(**(dict(codec_type=kind,type_index=index,language=None,region=None,title=None,default=None,forced=None)|kw))
def order(kind,index):return types.SimpleNamespace(source='embedded',codec_type=kind,type_index=index,path=None)

with tempfile.TemporaryDirectory(prefix='vse-import-fixtures-') as directory:
    root=Path(directory);destination=root/'out';destination.mkdir()
    module('app.v2',DATA_DIR=root/'data',make_language=scope['make_language'],probe=probe)
    from app import movie_import_pipeline as pipeline
    srt='1\n00:00:00,000 --> 00:00:01,000\n<b>Olá</b><br/> <i><u>mundo</u></i> <font color="red">hoje</font></br>\n\n2\n00:00:02,000 --> 00:00:03,000\n<b>Fim</b>\n'
    first=root/'first.srt';second=root/'second.srt';first.write_text(srt);second.write_text(srt.replace('Olá','Outro'))
    source=root/'source.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=s=64x48:r=4','-f','lavfi','-i','sine=frequency=440',
        '-f','lavfi','-i','sine=frequency=880','-i',str(first),'-i',str(second),'-map','0:v','-map','1:a','-map','2:a','-map','3:s','-map','4:s',
        '-t','4','-c:v','mpeg4','-c:a','aac','-c:s','copy',str(source)],check=True)
    subprocess.run(['mkvpropedit',str(source),'--edit','track:a1','--set','language=eng','--edit','track:a2','--set','language=por',
        '--set','language-ietf=pt-BR','--edit','track:s1','--set','language=por','--set','language-ietf=pt-BR','--set','name=Original',
        '--set','flag-forced=1','--set','flag-hearing-impaired=1','--edit','track:s2','--set','language=eng'],capture_output=True,check=True)
    # Genuine track tags, global tags, chapters and attachments must survive
    # replacement of a tagged subtitle and reordered stream identities.
    tags=root/'tags.xml';chapters=root/'chapters.xml';attachment=root/'notice.txt'
    uid=pipeline.identify(source)['tracks'][3]['properties']['uid']
    tags.write_text(f'<Tags><Tag><Targets/><Simple><Name>COMMENT</Name><String>Global note</String></Simple></Tag>'
        f'<Tag><Targets><TrackUID>{uid}</TrackUID></Targets><Simple><Name>COMMENT</Name><String>Subtitle note</String></Simple></Tag></Tags>')
    chapters.write_text('<?xml version="1.0" encoding="UTF-8"?><Chapters><EditionEntry><ChapterAtom><ChapterTimeStart>00:00:00.000</ChapterTimeStart>'
        '<ChapterTimeEnd>00:00:04.000</ChapterTimeEnd><ChapterDisplay><ChapterString>Opening</ChapterString>'
        '<ChapterLanguage>eng</ChapterLanguage></ChapterDisplay></ChapterAtom></EditionEntry></Chapters>')
    attachment.write_text('Keep this attachment')
    enriched=root/'enriched.mkv'
    fixture=subprocess.run(['mkvmerge','--quiet','-o',str(enriched),'--global-tags',str(tags),'--chapters',str(chapters),
        '--attach-file',str(attachment),str(source)],capture_output=True)
    assert fixture.returncode==0,(fixture.stdout+fixture.stderr).decode()
    os.replace(enriched,source)
    subtitle=root/'source.pt.srt';subtitle.write_text(srt)
    before=scope['stamp'](source);original=source.read_bytes();original_side=subtitle.read_bytes()
    original_flags=pipeline.identify(source)['tracks'][3]['properties']
    def build(name,edit,cleanups=()):
        progress=[];sides=external_subtitles(source)
        result=pipeline.build_import(source,destination/name,edit,sides,list(cleanups),lambda *a,**kw:progress.append((a,kw)),
            scope['stamp'](source),[scope['stamp'](Path(s['path'])) for s in sides])
        assert source.read_bytes()==original and subtitle.read_bytes()==original_side
        assert [args[0] for args,_ in progress]==[2,3,4,5]
        assert not list(destination.glob('.*.vse-import-*')) and not list((root/'data/import-work').glob('*.json'))
        return result
    start=len(commands);result=build('metadata.mkv',request(tracks=[track('audio',0,title='Changed')]))
    assert result['operation']=='copy_and_metadata' and result['media_writes']==1
    assert not any(c[0] in {'mkvmerge','ffmpeg'} for c in commands[start:])
    assert pipeline.identify(destination/'metadata.mkv')['tracks'][2]['properties']['language_ietf']=='pt-BR'
    cached[0]=scope['_extract_track'](source,dict(source='embedded',type_index=0,external_path='',codec='subrip')).text
    edit=request(default_subtitle='embedded:subtitle:0',forced_subtitle='embedded:subtitle:0',
        tracks=[track('subtitle',0,language='pt',region='BR',title='Cleaned')],remove=['embedded:audio:0'],
        order=[order('audio',1),order('subtitle',1),order('subtitle',0)],
        external_subtitles=[types.SimpleNamespace(path=str(subtitle),embed=True,language='pt',region='PT',title='External',forced=False)])
    cleanups=[types.SimpleNamespace(type_index=n,external_path=None) for n in (0,1)]
    cleanups.append(types.SimpleNamespace(type_index=None,external_path=str(subtitle)))
    start=len(commands);result=build('combined.mkv',edit,cleanups)
    assert result['operation']=='single_remux' and result['external_subtitles']==[]
    assert sum(c[0] in {'mkvmerge','ffmpeg'} for c in commands[start:])==1
    props=pipeline.identify(destination/'combined.mkv')['tracks']
    assert props[1]['properties']['language_ietf']=='pt-BR'
    assert props[2]['properties']['language']=='eng' and props[3]['properties']['track_name']=='Cleaned'
    assert props[3]['properties']['forced_track'] and props[3]['properties'].get('flag_hearing_impaired'), (original_flags,props[3]['properties'])
    assert props[4]['properties']['language_ietf']=='pt-PT'
    pipeline.publish_import_cache(destination/'combined.mkv',result['plan'])
    assert [t.type_index for _,t in cache_calls]==[0,1,2] and all('<b>' not in t.text and '<i>' in t.text for _,t in cache_calls)
    assert all('<br/>' in t.text and '</br>' in t.text and '</i>' in t.text and '</font>' in t.text for _,t in cache_calls)
    assert all('<u>mundo</u>' in t.text for _,t in cache_calls)
    result=build('sidecar.mkv',request(),[types.SimpleNamespace(type_index=None,external_path=str(subtitle))])
    assert '<b>' not in Path(result['external_subtitles'][0]).read_text() and '<i>' in Path(result['external_subtitles'][0]).read_text()
    assert '<br/>' in Path(result['external_subtitles'][0]).read_text() and '</br>' in Path(result['external_subtitles'][0]).read_text()
    assert '<u>mundo</u>' in Path(result['external_subtitles'][0]).read_text()
    existing=destination/'conflict.pt.srt';existing.write_text('keep')
    try:build('conflict.mkv',request())
    except HttpError as exc:assert exc.status_code==409
    else:raise AssertionError('Existing sidecar overwritten')
    assert existing.read_text()=='keep' and not (destination/'conflict.mkv').exists()
    real_verify=pipeline._verify;pipeline._verify=lambda *_a:(_ for _ in ()).throw(RuntimeError('Injected verification failure'))
    try:build('broken.mkv',request())
    except RuntimeError:pass
    else:raise AssertionError('Unverified output published')
    assert not (destination/'broken.mkv').exists() and not (destination/'broken.pt.srt').exists()
    pipeline._verify=real_verify
    token=uuid.uuid4().hex;partial=destination/f'.sample.vse-import-{token}.mkv';partial.write_bytes(b'partial')
    pipeline._register_output(partial,token);safe_final=destination/'successful.mkv';os.link(partial,safe_final)
    pipeline.recover_interrupted_imports()
    assert not partial.exists() and safe_final.read_bytes()==b'partial'
    token=uuid.uuid4().hex;partial=destination/f'.unfinished.vse-import-{token}.mkv';partial.write_bytes(b'partial')
    owned_side=destination/'unfinished.pt.srt';owned_side.write_text(srt)
    pipeline._register_output(partial,token,target=destination/'unfinished.mkv',sidecars=[str(owned_side)])
    pipeline.recover_interrupted_imports()
    assert not partial.exists() and not owned_side.exists()
    assert scope['stamp'](source)==before
    # Queued imports must not pay for a second full-source recovery copy.
    load('app/postgres_store.py',{'prepare_task_artifact'},scope)
    scope['prepare_task_artifact']('fixture-group',1,'movie_import',str(source),{})
    # Non-Matroska imports still use one output; plain ISO language survives.
    mp4=root/'portable.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(source),'-map','0:v:0','-map','0:a:0',
                    '-c','copy','-metadata:s:a:0','language=eng',str(mp4)],check=True)
    portable=request(tracks=[track('audio',0,language='en',region='')])
    output=destination/'portable.mp4'
    result=pipeline.build_import(mp4,output,portable,[],[],lambda *a,**kw:None,scope['stamp'](mp4),[])
    assert result['operation']=='single_remux' and output.exists()
    # Do not silently drop a regional tag the destination container cannot store.
    portable.tracks=[track('audio',0,language='pt',region='BR')]
    try:pipeline.build_import(mp4,destination/'unsupported-region.mp4',portable,[],[],lambda *a,**kw:None,scope['stamp'](mp4),[])
    except HttpError as exc:assert exc.status_code==422 and 'Matroska' in exc.detail
    else:raise AssertionError('Unsupported regional language silently dropped')
    assert not (destination/'unsupported-region.mp4').exists()
    # Publication races never overwrite someone else's newly-created movie.
    raced=destination/'raced.mkv'
    def race(plan,output):
        real_verify(plan,output)
        raced.write_bytes(b'other owner')
    pipeline._verify=race
    try:build('raced.mkv',request())
    except HttpError as exc:assert exc.status_code==409
    else:raise AssertionError('Concurrent destination creation overwritten')
    assert raced.read_bytes()==b'other owner' and not (destination/'raced.pt.srt').exists()
    # New sidecars arriving during processing also invalidate the source set.
    unexpected=root/'source.en.srt'
    def change_sidecars(plan,output):
        real_verify(plan,output)
        unexpected.write_text(srt)
    pipeline._verify=change_sidecars
    try:build('changed-source.mkv',request())
    except HttpError as exc:assert exc.status_code==409
    else:raise AssertionError('Changed source sidecar set accepted')
    assert not (destination/'changed-source.mkv').exists() and source.read_bytes()==original
    unexpected.unlink();pipeline._verify=real_verify

print('PASS: single remux; metadata fast path, exact pt-BR/flags, color/italics/underline/breaks and closing tags preserved, cache output indexes, conflicts, rollback and interrupted-output recovery')
