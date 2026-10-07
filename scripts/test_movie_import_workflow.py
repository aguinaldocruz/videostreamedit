"""HTTP/queued import wrapper regression checks with an isolated pipeline."""
import ast
import copy
import logging
import os
import shutil
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)

def stamp(path):
    path = Path(path)
    st = path.stat()
    return {'path': str(path.resolve()), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
            'ctime_ns': st.st_ctime_ns, 'device': st.st_dev, 'inode': st.st_ino}

safety = types.ModuleType('app.job_safety')
safety.stamp = stamp
sys.modules['app.job_safety'] = safety
pipeline = types.ModuleType('app.movie_import_pipeline')
sys.modules['app.movie_import_pipeline'] = pipeline

class Edit:
    def __init__(self, data):
        self.data = data
    def model_copy(self, update):
        return types.SimpleNamespace(**(copy.deepcopy(self.data) | update))

scope = {'Path': Path, 'HTTPException': HttpError,
         'logger': logging.getLogger('import-test'), 'MovieImportRequest': object,
         'ImportCleanupRequest': types.SimpleNamespace, 'ReorderEditRequest': object}
nodes = [n for n in ast.parse((ROOT/'app/v28.py').read_text()).body if isinstance(n, ast.FunctionDef)
         and n.name in {'inside','execute_movie_import','cleanup_import_source','import_movie'}]
for node in nodes:
    node.decorator_list = []
exec(compile(ast.Module(body=nodes, type_ignores=[]),'movie-import', 'exec'), scope)

with TemporaryDirectory(prefix='vse-import-wrapper-') as folder:
    root=Path(folder);source=root/'source.mkv';source.write_bytes(b'original')
    subtitle=root/'source.pt.srt';subtitle.write_text('original sidecar')
    destination=root/'out';destination.mkdir()
    calls=[];phases=[];learned=[];published=[]
    scope.update(authorized_import_file=lambda p:Path(p),movie_destinations=lambda:[{'path':str(destination)}],
                 external_subtitles=lambda p:[{'path':str(subtitle)}] if Path(p)==source and subtitle.exists() else [],
                 import_input_root=lambda:root,queue_post_import_refresh=lambda s,t,k:calls.append(('plex',s,t,k)),
                 _record_import_learning=lambda edit,plan:learned.append(edit))
    scope.update(_record_import_final_revision=lambda *_a:None,_complete_import_audio=lambda *_a:None)
    def build(source,target,edit,sides,cleanups,progress,snapshot,side_snapshots):
        calls.append(('build',source,target,edit,cleanups))
        assert edit.path==str(source) and snapshot==stamp(source) and side_snapshots==[stamp(subtitle)]
        for step in range(2,6):progress(step,'Import step')
        with target.open('xb') as output:output.write(source.read_bytes())
        return {'plan':{'data':{}},'warnings':[],'external_subtitles':[], 'durable':True,
                'target_snapshot':stamp(target), 'target_subtitles':[],
                'operation':'single_remux','media_writes':1}
    pipeline.build_import=build
    pipeline.publish_import_cache=lambda path,plan:published.append(path)
    request=types.SimpleNamespace(source=str(source),destination=str(destination),filename='imported.mkv',
        html_cleanups=[object()],operation_id=None,remove_original=False,media_kind='movie',
        edit=Edit({'path':'ignored spoofed path','tracks':[]}))
    result=scope['execute_movie_import'](request,lambda step,*args,**kw:phases.append(step))
    assert phases==[1,2,3,4,5,6] and len(learned)==1 and len(published)==1
    assert result['operation']=='single_remux' and result['media_writes']==1
    assert source.read_bytes()==b'original' and subtitle.exists()
    try:scope['execute_movie_import'](request)
    except HttpError as exc:assert exc.status_code==409
    else:raise AssertionError('Existing output overwritten')
    assert Path(result['target']).read_bytes()==b'original'
    scope['queue_post_import_refresh']=lambda *_a:(_ for _ in ()).throw(RuntimeError('DB unavailable'))
    request.filename='refresh-warning.mkv'
    refreshed=scope['execute_movie_import'](request)
    assert refreshed['warnings'] and Path(refreshed['target']).exists()
    pipeline.publish_import_cache=lambda *_a:(_ for _ in ()).throw(RuntimeError('Cache unavailable'))
    request.filename='cache-warning.mkv'
    assert scope['execute_movie_import'](request)['warnings']
    cleanup=types.SimpleNamespace(source=str(source),expected_source=result['source_snapshot'],expected_subtitles=result['source_subtitles'],
        target=result['target'],expected_target=stamp(result['target']),expected_target_subtitles=[])
    subtitle.write_text('external change')
    try:scope['cleanup_import_source'](cleanup)
    except HttpError as exc:assert exc.status_code==409
    else:raise AssertionError('Changed source subtitle deleted')
    assert source.exists() and subtitle.exists()
    cleanup.expected_subtitles=[stamp(subtitle)]
    removed=scope['cleanup_import_source'](cleanup)
    assert len(removed['removed'])==2 and Path(result['target']).exists()

    # Immediate and queued imports must keep exact timestamp integers on the
    # server. The former browser round-trip demonstrably rejected these values.
    queued_scope={'MovieImportRequest':types.SimpleNamespace(model_validate=lambda p:types.SimpleNamespace(**p)),
                  'movie_import':types.SimpleNamespace(execute_movie_import=scope['execute_movie_import']),
                  'tasks':types.SimpleNamespace(update_progress=lambda *_a:None)}
    queued_node=next(n for n in ast.parse((ROOT/'app/v73.py').read_text()).body
                     if isinstance(n,ast.FunctionDef) and n.name=='process_movie_import')
    exec(compile(ast.Module(body=[queued_node],type_ignores=[]),'queued-import','exec'),queued_scope)
    scope['queue_post_import_refresh']=lambda s,t,k:calls.append(('plex',s,t,k))
    pipeline.publish_import_cache=lambda *_a:None
    for kind in ('movie','episode'):
        for queued in (False,True):
            for remove in (False,True):
                source.write_bytes(b'original');subtitle.write_text('original sidecar')
                precise=1_790_000_000_123_456_789
                os.utime(source,ns=(precise,precise));os.utime(subtitle,ns=(precise,precise))
                assert int(float(stamp(source)['mtime_ns']))!=stamp(source)['mtime_ns']
                request.filename=f'{kind}-{queued}-{remove}.mkv'
                request.media_kind=kind;request.remove_original=remove
                phases=[]
                if queued:
                    payload=vars(request).copy()
                    result=queued_scope['process_movie_import'](123,payload)
                    assert payload['remove_original']==remove  # Worker must not pop the choice.
                else:
                    result=scope['execute_movie_import'](request,lambda step,*a,**kw:phases.append(step))
                    assert phases==list(range(1,8 if remove else 7))
                assert source.exists()==(not remove) and subtitle.exists()==(not remove)
                assert result['source_cleanup_status']==('removed' if remove else 'not_requested')
                assert Path(result['target']).read_bytes()==b'original'
                assert calls[-1][-1]==kind

    # A concurrent source/destination change after the verified output must
    # retain the original, as must an unconfirmed output durability barrier.
    for change in ('source','subtitle','target','durability'):
        source.write_bytes(b'original');subtitle.write_text('original sidecar')
        request.filename=f'changed-{change}.mkv';request.remove_original=True
        def modified_build(*args):
            value=build(*args)
            if change=='durability':value['durable']=False
            else:{'source':source,'subtitle':subtitle,'target':args[1]}[change].write_bytes(b'changed externally')
            return value
        pipeline.build_import=modified_build
        result=scope['execute_movie_import'](request)
        assert result['warnings'] and result['source_cleanup_status']=='retained'
        assert source.exists() and subtitle.exists() and Path(result['target']).exists()
    pipeline.build_import=build
    # Source outside the removable input root is rejected before any output.
    scope['import_input_root']=lambda:destination
    request.filename='must-not-write.mkv'
    try:scope['execute_movie_import'](request)
    except HttpError as exc:assert exc.status_code==403
    else:raise AssertionError('Accepted removal outside input root')
    assert not (destination/request.filename).exists()

print('PASS: movies/episodes, immediate/queued, exact nanoseconds, optional source removal, changed inputs/output and durability protection')
