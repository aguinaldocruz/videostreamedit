"""Browser handoff regression; all writes and draft statuses are fixtures."""
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page()
    errors=[];pending=[]
    session={'session_id':'fixture','show_id':'fixture:show','show_title':'Draft fixture','status':'open','dirty':True,'operation_count':202}
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('dialog',lambda d:d.accept())
    def route(r):
        path=urlsplit(r.request.url).path
        if path=='/api/v19/tv':
            show_id=parse_qs(urlsplit(r.request.url).query).get('show_id',['fixture:show'])[0]
            return r.fulfill(json=[{'id':show_id,'name':'Draft fixture' if show_id=='fixture:show' else 'Other fixture','seasons':[{'name':'Season 1','episodes':[]}]}])
        if path.endswith('/edit-session-active'):return r.fulfill(json={'session':None})
        if path.endswith('/edit-session/open'):return r.fulfill(json=session)
        if path.endswith('/edit-session/fixture'):return r.fulfill(json={**session,'operations':[]})
        if path.endswith('/edit-session/fixture/save'):
            pending.append(r);return
        if path.endswith('/edit-session/fixture/status'):return r.fulfill(json={**session,'status':'committing','dirty':False,'task_id':123,'task':{'id':123,'status':'pending'}})
        if r.request.method!='GET':return r.fulfill(json={'items':{},'needs_attention':False,'reasons':[]})
        r.continue_()
    page.route('**/api/**',route)
    page.goto('http://127.0.0.1:8383',wait_until='domcontentloaded')
    page.wait_for_function('typeof window.renderTvEditMode === "function"')
    page.evaluate('''()=>{loadTv=async()=>{};page('tv');state.shows=[{id:'fixture:show',name:'Draft fixture',seasons:[]},{id:'fixture:other',name:'Other fixture',seasons:[]}];state.currentShow=state.shows[0];renderShows();renderEpisodes()}''')
    page.locator('#tv-show-edit-mode').click()
    page.wait_for_function('window.tvShowEditSession?.session_id === "fixture"')
    page.locator('#tv-show-edit-mode').click()
    page.locator('#global-busy-overlay').wait_for(state='visible')
    page.wait_for_function('document.querySelector("#tv-show-edit-mode").disabled')
    assert pending
    pending.pop().fulfill(json={'session_id':'fixture','status':'committing','queued':True,'task_id':123,'operation_count':202})
    page.locator('#global-busy-overlay').wait_for(state='detached')
    page.wait_for_function('document.querySelector("#tv-edit-session-status").textContent.includes("Job #123 queued")')
    assert not page.locator('#show-list .show-card').last.is_disabled()
    page.locator('#show-list .show-card').last.click()
    page.wait_for_function('state.currentShow?.id === "fixture:other" && !window.tvShowEditSession')
    assert not errors,errors
    browser.close()
print('PASS: immediate submission splash, queue acknowledgement, persistent job status and navigation released; all writes mocked')
