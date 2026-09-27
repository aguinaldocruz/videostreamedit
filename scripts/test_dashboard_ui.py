"""Read-only dashboard browser tests; mutations and summaries are mocked."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1366,'height':900})
    errors=[];reads=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        url=r.request.url
        if r.request.method!='GET':
            return r.fulfill(json={'items':{},'needs_attention':False,'reasons':[]})
        reads.append(url)
        if '/api/dashboard/overview' in url:
            return r.fulfill(json={'collection':{'movies':{'total':10,'bytes':10000,'indexed':9,'final':3,'reviewed':5,'changed':2},'tv':{'total':20,'bytes':20000,'indexed':20,'final':6,'reviewed':10,'changed':3,'shows':2,'final_shows':0}},'continue':[], 'libraries':[], 'updated_at':'2026-09-26T12:00:00Z','work':{'tasks':[{'type':'media_edit','status':'pending','count':4},{'type':'media_edit','status':'failed','count':1}], 'indexes':[{'type':'core','status':'running','count':1}], 'preflight':[{'status':'pending','count':2}],'paused':False,'index_paused':{},'completed_hour':12,'running':[]}})
        if '/api/dashboard/media?' in url:
            return r.fulfill(json={'items':[{'path':'/fixture.mkv','title':'Example movie','library_name':'Movies'}],'more':False})
        if '/api/v86/dashboard/stats' in url:
            return r.fulfill(json={'languages':{'movies':{'audio':[{'language':'pt-BR','media_count':4}], 'subtitle':[{'language':'pt-PT','media_count':2}]},'tv':{'audio':[],'subtitle':[]}}, 'libraries':[], 'subtitle_coverage':[{'kind':'movie','embedded':1,'external':1,'media_count':2}]})
        if '/reports/availability' in url:
            return r.fulfill(json={'reports':{'html':{'movies':True,'tv':False}},'counts':{'html':{'movies':2,'tv':0}}})
        r.continue_()
    page.route('**/api/**',route)
    page.goto('http://127.0.0.1:8383',wait_until='domcontentloaded')
    page.locator('.dash-total').first.wait_for(timeout=60000)
    assert not any('/dashboard/stats' in url for url in reads), 'Optional insights loaded eagerly'
    assert '1 not yet indexed' in page.locator('#dashboard').inner_text()
    page.locator('[data-review="movies:reviewed"]').click()
    page.locator('.dashboard-drilldown .dash-media-row').wait_for()
    assert any('kind=movies&status=reviewed' in url for url in reads)
    page.locator('.dashboard-drilldown [data-close]').click()
    page.locator('[data-jobs="tasks:failed:"]').click()
    page.wait_for_function('document.querySelector("[data-queue-status=failed]")?.classList.contains("active")')
    assert any('/api/v65/queue?status=failed' in url for url in reads)
    page.locator('[data-page=dashboard]').click()
    page.locator('[data-jobs="indexes:running:core"]').click()
    assert page.locator('[data-index-type=core]').get_attribute('aria-pressed')=='true'
    assert page.locator('[data-index-status=running]').get_attribute('aria-pressed')=='true'
    page.locator('[data-page=dashboard]').click()
    page.locator('#dashboard-scope').select_option('movies')
    assert page.locator('.dash-total').count()==1
    page.locator('[data-insights] summary').click()
    page.wait_for_function('document.querySelector("[data-insight-body]").textContent.includes("Embedded + external")')
    assert 'Brazil' in page.locator('[data-insight-body]').inner_text()
    page.set_viewport_size({'width':720,'height':800})
    page.screenshot(path='/tmp/dashboard-redesign.png')
    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'), 'Page overflows horizontally'
    assert not errors,errors
    browser.close()
    print('PASS: summary, lazy insights, review drilldown, exact task/index filters, scope and narrow layout')
