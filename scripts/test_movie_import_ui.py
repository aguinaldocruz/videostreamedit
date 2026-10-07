"""Live browser import test; all mutations intercepted, never sent to the server."""
import os
import sys
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

local_assets={}
if os.getenv('VSE_IMPORT_LOCAL_ASSETS'):
    # Build workspace assets without app startup or database access.
    root=Path(__file__).resolve().parents[1]
    node=next(n for n in ast.parse((root/'app/web_assets.py').read_text()).body
              if isinstance(n,ast.AsyncFunctionDef) and n.name=='current_web_assets')
    node.decorator_list=[]
    scope={'Request':object,'STATIC_DIR':root/'app/static','_bundles':{},'_bootstrap_mode':False,
           'store_bundle':lambda request,content,media_type:content}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'test-assets','exec'),scope)
    for name,content_type in [('v19.js','text/javascript'),('v19.css','text/css')]:
        body=asyncio.run(scope['current_web_assets'](SimpleNamespace(method='GET',url=SimpleNamespace(path='/assets/'+name)),None))
        local_assets[name]=(body,content_type)

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page=browser.new_page(viewport={'width':1366,'height':900}); errors=[]; writes=[]; import_requests=[]; queue_requests=[]; fail=[False]
    page.on('pageerror',lambda error:errors.append(str(error)))
    metadata={'streams':[{'codec_type':'audio','type_index':0,'codec':'aac','language':'en','region':'',
                          'title':'English','external':False,'default':True,'forced':False}],
              'external_subtitles':[],'portuguese_detection':[]}
    def route(r):
        path=urlsplit(r.request.url).path
        if path.endswith('/media/details-fast'):return r.fulfill(json=metadata)
        if path.endswith('/subtitle-properties'):return r.fulfill(json={'properties':[]})
        if path.endswith('/final-version'):return r.fulfill(json={'final_version':False})
        if path.endswith('/import/config'):return r.fulfill(json={'input_folder':'/fixture','last_input_folder':'/fixture'})
        if path.endswith('/import/destinations'):return r.fulfill(json=[{'path':'/fixture/output','name':'Movies','movie_count':1}])
        if '/import/browse' in path:return r.fulfill(json={'path':'/fixture','parent':None,'directories':[],
                                                         'files':[{'path':'/fixture/import.mkv','name':'import.mkv','size':1000000}]})
        if '/import/progress/' in path:return r.fulfill(json={'status':'running','step':2,'total':6,
            'message':'Copying movie','detail':'import.mkv','copy_percent':50,'copy_current':500000,'copy_total':1000000})
        if r.request.method=='GET':return r.continue_()
        writes.append(path)
        if path.endswith('/import/movie'):
            import_requests.append(r.request.post_data_json)
            if fail[0]:return r.fulfill(status=409,json={'detail':'Existing output; original retained'})
            return r.fulfill(json={'target':'/fixture/output/import.mkv','warnings':[],
                'source_cleanup_status':'removed' if r.request.post_data_json.get('remove_original') else 'not_requested'})
        if path.endswith('/queue'):
            queue_requests.append(r.request.post_data_json)
            return r.fulfill(json={'id':999999})
        if path.endswith('/video-titles'):return r.fulfill(json={'items':{}})
        return r.fulfill(json={'ok':True,'needs_attention':False,'reasons':[]})
    page.route('**/api/**',route)
    def serve_local_asset(route):
        body,content_type=local_assets[Path(urlsplit(route.request.url).path).name]
        route.fulfill(body=body,content_type=content_type)
    for name in local_assets:
        page.route('**/assets/'+name,serve_local_asset)
    # Delay the intercepted import response without delaying progress polling.
    page.add_init_script('''(() => {const old=window.fetch;window.fetch=async(...args)=>{
      if(String(args[0]).endsWith('/api/v28/import/movie'))await new Promise(r=>setTimeout(r,1800));
      return old(...args);
    };})();''')
    page.goto(sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:8383/',wait_until='domcontentloaded')
    page.wait_for_function('typeof openEditor==="function" && !!window.updateStreamEditorContext')
    # Suppress learning dialogs; they have independent regression coverage.
    page.evaluate('offerSavedValues=async()=>{}')
    assert page.locator('#movie-import-page-button').inner_text()=='Import Media'
    assert page.locator('#import-media-kind option').all_text_contents()==['Movie','TV episode']
    def open_import(kind='movie'):
        page.wait_for_function('!document.querySelector("#global-busy-overlay")')
        page.evaluate('''async kind=>{
          movieImportMode={source:"/fixture/import.mkv",sourceName:"import.mkv",filename:"import.mkv",mediaKind:kind,destination:"/fixture/output",editing:true};
          await openEditor(movieImportMode.source,movieImportMode.sourceName);
        }''',kind)
        page.wait_for_function('!window.isStreamEditorBusy()')
        assert page.locator('#stream-form [type=submit]').is_enabled()
    open_import()
    # Import without edits must be possible, and the progress modal must be on top.
    assert page.evaluate('queuedChangeCount()')==0
    page.locator('#stream-form [type=submit]').click()
    page.locator('#import-remove-original').check()
    page.locator('[data-import-processing=now]').click()
    page.wait_for_function('!!document.querySelector("#global-busy-overlay")')
    assert page.locator('#global-busy-overlay').evaluate('e=>e.open')
    page.wait_for_function('document.querySelector("#global-busy-overlay").textContent.includes("50% copied")')
    assert page.locator('#global-busy-overlay').evaluate('e=>e.matches(":modal")')
    page.wait_for_function('!document.querySelector("#stream-dialog").open && !window.isMovieImportBusy()')
    assert len(import_requests)==1 and len(import_requests[0]['operation_id'])==32
    assert import_requests[0]['remove_original'] is True
    assert '/api/v28/import/cleanup' not in writes
    open_import('episode')
    title=page.locator('#stream-content [name=title]').first
    title.fill('Updated audio')
    page.locator('#stream-form [type=submit]').click()
    page.locator('#import-remove-original').check()
    page.locator('[data-import-processing=queue]').click()
    page.wait_for_function('!document.querySelector("#stream-dialog").open && !window.isMovieImportBusy()')
    assert writes.count('/api/v65/queue')==1 and len(import_requests)==1
    assert queue_requests[0]['payload']['media_kind']=='episode'
    assert queue_requests[0]['payload']['remove_original'] is True
    # The unchecked choice must also survive an episode import.
    open_import('episode')
    page.locator('#stream-form [type=submit]').click()
    assert not page.locator('#import-remove-original').is_checked()
    page.locator('[data-import-processing=now]').click()
    page.wait_for_function('!document.querySelector("#stream-dialog").open && !window.isMovieImportBusy()')
    assert import_requests[-1]['media_kind']=='episode' and import_requests[-1]['remove_original'] is False
    # Failure releases the splash, keeps the proposed changes, and allows retry.
    open_import();fail[0]=True;title.fill('Retry title')
    page.locator('#stream-form [type=submit]').click()
    page.locator('[data-import-processing=now]').click()
    page.wait_for_function('!window.isMovieImportBusy() && !document.querySelector("#global-busy-overlay")')
    assert page.locator('#stream-dialog').evaluate('e=>e.open')
    assert title.input_value()=='Retry title'
    assert page.locator('#stream-form [type=submit]').is_enabled()
    assert not errors,errors
    assert not any(path.endswith('/media/edit') for path in writes),writes
    browser.close()
print('PASS: movie/episode import, checked/unchecked removal carried to server, no browser cleanup request, splash, queue, failure unlocks; no production writes')
