"""Execute the real report query with indexed-codec fixtures, not detector rows."""
import ast
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
db.executescript('''CREATE TABLE media_stream_index(path TEXT,source TEXT,type_index INTEGER,stream_type TEXT,
 external_path TEXT,language TEXT,region TEXT,track_name TEXT,codec TEXT);
 CREATE TABLE preflight_requests(operation_type TEXT,status TEXT,payload_json TEXT);''')
db.executemany('INSERT INTO media_stream_index VALUES(?,?,?,?,?,?,?,?,?)',[
    ('/movie.mkv','embedded',0,'subtitle','','pt','BR','Image','hdmv_pgs_subtitle'),
    ('/movie.mkv','embedded',1,'subtitle','','en','','Text','subrip'),
    ('/external.mkv','external',-1,'external','/external.idx','en','','Image','idx'),
    ('/ep.mkv','embedded',0,'subtitle','','en','','Image','dvd_subtitle'),
    ('/final.mkv','embedded',0,'subtitle','','en','','Image','pgs'),
    ('/converting.mkv','embedded',0,'subtitle','','en','','Image','pgs'),
])
db.execute('INSERT INTO preflight_requests VALUES(?,?,?)',('image_subtitle_convert_bulk','pending',
           json.dumps({'_bulk_items':[{'path':'/converting.mkv'}]})))
@contextmanager
def connection():yield db
tree=ast.parse((Path(__file__).resolve().parents[1]/'app/v19.py').read_text())
nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='image_subtitle_report'
       or isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='IMAGE_SUBTITLE_CODECS' for t in node.targets)]
for node in nodes:
    if isinstance(node,ast.FunctionDef):node.decorator_list=[]
scope={'Path':Path,'json':json,'connection':connection,'HTTPException':RuntimeError,
       'report_blocked_paths':lambda:{'/final.mkv'},
       '_detection_row_current':lambda row:(_ for _ in ()).throw(AssertionError('Image rows must not be filtered by detector metadata')),
       'plex_movies':lambda:[{'name':p[1:],'path':p} for p in ('/movie.mkv','/external.mkv','/final.mkv','/converting.mkv')],
       'plex_tv':lambda:[{'id':'show','name':'Show','seasons':[{'episodes':[{'path':'/ep.mkv','name':'S01E01'}]}]}]}
exec(compile(ast.Module(body=nodes,type_ignores=[]),'image-report','exec'),scope)
movies=scope['image_subtitle_report']('movies')
assert movies['title_count']==2 and movies['media_count']==2,movies
assert sum(item['image_subtitle_count'] for item in movies['items'])==2
assert {s['source'] for item in movies['items'] for s in item['streams']}=={'external','embedded'}
tv=scope['image_subtitle_report']('tv')
assert tv['title_count']==1 and tv['media_count']==1,tv
assert tv['items'][0]['episodes'][0]['episode']=='S01E01'
print('PASS: actual image report SQL, movie and TV grouping, graphical external sources, exclusion of text/final/converting media, no language-result dependency')
