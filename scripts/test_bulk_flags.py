"""Isolated bulk flag regression: no catalog, queues or real media writes."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

root=Path(__file__).resolve().parents[1]
def load(file,names,scope):
    tree=ast.parse((root/file).read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    for n in nodes: n.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),file,'exec'),scope)

def request(action,flag='forced',targets=None):
    return NS(paths=['fixture'],filters=NS(stream_type='subtitle'),changed_fields=[flag],target_keys=targets,
              default_action=action if flag=='default' else 'unchanged',forced_action=action if flag=='forced' else 'unchanged',
              remove=False,integrate=False,language='',region='',track_name='')

details={'streams':[dict(codec_type=kind,type_index=i,language=lang,region='',title='',default=True,forced=True)
                    for kind,i,lang in [('audio',0,'en'),('subtitle',0,'pt'),('subtitle',1,'en'),('subtitle',2,'pt')]],'external_subtitles':[]}
def validate(data):
    data=dict(data)
    if isinstance(data.get('filters'),dict): data['filters']=NS(**data['filters'])
    return NS(**data)
scope={'SeasonStreamBulkEdit':NS(model_validate=validate),'media_details_with_ietf':lambda _:details,
       'filter_matches':lambda s,f:s['codec_type']==f.stream_type and s['language']=='pt'}
load('app/v79.py',{'episode_bulk_edit','consolidated_tv_edit'},scope)
for flag in ('default','forced'):
    clear=request('clear',flag)
    edit,_=scope['episode_bulk_edit']('fixture',clear)
    assert [(t['type_index'],t[flag]) for t in edit['tracks']]==[(0,False),(2,False)]
    assert all(edit[f'{f}_{k}']=='__preserve__' for f in ('default','forced') for k in ('audio','subtitle'))
    journal=[vars(clear)]
    draft,_=scope['consolidated_tv_edit']('fixture',journal)
    assert [(t['type_index'],t[flag]) for t in draft['tracks']]==[(0,False),(2,False)]
    unchanged,_=scope['episode_bulk_edit']('fixture',request('unchanged',flag))
    assert not unchanged['tracks']
    set_edit,_=scope['episode_bulk_edit']('fixture',request('set',flag))
    assert set_edit[f'{flag}_subtitle']=='embedded:subtitle:2'
    assert set_edit[f'{flag}_audio']=='__preserve__'
    draft,_=scope['consolidated_tv_edit']('fixture',[vars(request('set',flag)),vars(clear)])
    assert all(t[flag] is False for t in draft['tracks'])
    no_match,_=scope['episode_bulk_edit']('fixture',request('set',flag,[]))
    assert no_match[f'{flag}_subtitle']=='__preserve__'

for stream in details['streams']: stream['title']='Existing name'
clear_name=request('unchanged')
clear_name.changed_fields=['track_name']
for edit in (scope['episode_bulk_edit']('fixture',clear_name)[0],
             scope['consolidated_tv_edit']('fixture',[vars(clear_name)])[0]):
    assert [(t['type_index'],t['title']) for t in edit['tracks']]==[(0,''),(2,'')]
for stream in details['streams']: stream['title']=''

command_scope={'Path':Path,'media_editor':NS(ReorderEditRequest=object)}
load('app/v43.py',{'desired_tag','matroska_metadata_command'},command_scope)
edit,_=scope['episode_bulk_edit']('fixture',request('clear'))
tracks=[NS(language=None,region=None,title=None,default=None,**t) for t in edit['tracks']]
payload=NS(clear_video_titles=False,**{**edit,'tracks':tracks})
typed={kind:[{'disposition':{'default':1,'forced':1}} for s in details['streams'] if s['codec_type']==kind] for kind in ('audio','subtitle')}
command=command_scope['matroska_metadata_command'](Path('/fixture.mkv'),payload,typed)
assert command==['mkvpropedit','/fixture.mkv','--edit','track:s1','--set','flag-forced=0','--edit','track:s3','--set','flag-forced=0'],command
print('PASS: unchanged/set/clear, subtitle/audio isolation, multiple flags, draft journal replay, no-match preservation, exact mkvpropedit scope')
