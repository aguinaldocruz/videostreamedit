"""Browser regressions using local JS and simulated API responses only."""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
base = (ROOT / 'app/static/v5.js').read_text()
api_source = base[base.index('async function api('):base.index('function toast(')]
keys = ('image', 'damaged', 'html', 'confidence', 'language', 'duplicate_audio',
        'duplicate_subtitle', 'uncommon', 'forced', 'english_only', 'audio_only',
        'external_only', 'video_titles', 'matroska_layout')

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    attrs = ('data-image-subtitle-report="movies"', 'data-damaged-subtitle-report="movies"',
             'data-html-subtitle-report="movies"', 'data-no-confidence-report="movies"',
             'data-portuguese-report="movies"', 'data-duplicate-language-report="movies:audio"',
             'data-duplicate-language-report="movies:subtitle"', 'data-uncommon-language-report="movies"',
             'data-forced-report="movies"', 'data-english-only-report="movies"',
             'data-audio-only-report="movies"', 'data-external-only-report="movies"')
    cards = ''.join('<section class="reports-group" ' + ('data-report-group="external-only"' if 'external-only' in attr else '') +
                    '><h3>Report</h3><div class="reports-group-actions"><button ' + attr + '>Movies</button></div></section>' for attr in attrs)
    page.set_content('''<button data-page="reports">Reports</button>
        <section id="reports"><div class="page-title"><p>Reports</p></div><p class="reports-note"></p>
        <div class="reports-groups">''' + cards + '''</div></section>
        <dialog id="image-subtitle-report-dialog"><h2 data-report-title>Report</h2>
        <button data-report-html-fix hidden>HTML cleanup</button><button data-report-language-fix hidden>Language fix</button>
        <div data-report-items></div><div class="dialog-actions"><button data-close>Close</button></div></dialog>''')
    page.evaluate('''keys => {
        window.keys=keys;window.approved=false;window.countRequests=0;window.finalEvents=0;
        window.deferCount=false;window.releaseCount=null;window.reportLoads=0;
        window.esc=String;window.toast=()=>{};
        window.counts=()=>({reports:Object.fromEntries(keys.map(k=>[k,{movies:!approved,tv:false}])),
            counts:Object.fromEntries(keys.map(k=>[k,{movies:approved?0:1,tv:0}]))});
        window.fetch=async(url,options)=>{
            if(url==='/api/v19/reports/availability'){
                countRequests++;const result=counts();
                if(deferCount){deferCount=false;await new Promise(resolve=>releaseCount=resolve)}
                return {ok:true,json:async()=>result};
            }
            if(options.method==='PUT'){
                if(url==='/fixture/fail')return {ok:false,status:500,statusText:'Error',json:async()=>({detail:'Failed'})};
                const body=JSON.parse(options.body);approved=body.final_version;
                return {ok:true,json:async()=>body};
            }
            return {ok:true,json:async()=>({final_version:approved})};
        };
        document.addEventListener('media-final-version-changed',()=>finalEvents++);
        const report=document.querySelector('#image-subtitle-report-dialog');
        document.querySelector('[data-html-subtitle-report]').onclick=async()=>{
            reportLoads++;report.showModal();
            report.querySelector('[data-report-items]').innerHTML=approved?
                '<p>No findings</p>':'<div class="report-item"><strong>Fixture movie</strong></div>';
        };
        report.querySelector('[data-close]').onclick=()=>report.close();
        // Fail observer loops promptly instead of hanging the browser test.
        const NativeObserver=window.MutationObserver;
        window.observerCalls=0;
        window.MutationObserver=class extends NativeObserver{
            constructor(callback){super((records,observer)=>{
                if(++window.observerCalls>100){observer.disconnect();throw Error('Observer did not settle')}
                callback(records,observer);
            })}
        };
    }''', keys)
    page.add_script_tag(content=api_source)
    page.add_script_tag(content=(ROOT / 'app/static/v105-reports.js').read_text())
    page.wait_for_function("!document.querySelector('#reports').classList.contains('reports-checking')")
    assert page.locator('[data-html-subtitle-report]').inner_text() == 'Movies · 1'
    page.click('[data-html-subtitle-report]')
    page.fill('.report-result-toolbar input', 'Fixture')
    # Real base API emits the change event only on successful committed writes.
    page.evaluate("api('/api/v86/note',{method:'PUT',body:JSON.stringify({entity_type:'movie',entity_key:'/fixture.mkv',final_version:true})})")
    page.wait_for_function("document.querySelector('[data-html-subtitle-report]').hidden")
    assert page.locator('[data-report-items]').inner_text() == 'No findings'
    assert page.locator('.report-result-toolbar input').input_value() == 'Fixture'
    assert page.evaluate('finalEvents') == 1
    page.evaluate("document.querySelector('#image-subtitle-report-dialog').close()")
    page.evaluate("api('/api/v86/final-version',{method:'PUT',body:JSON.stringify({path:'/fixture.mkv',final_version:false})})")
    page.wait_for_function("!document.querySelector('[data-html-subtitle-report]').hidden && !document.querySelector('#reports').classList.contains('reports-checking')")
    assert page.locator('[data-html-subtitle-report]').inner_text() == 'Movies · 1'
    # Race: old counts start first; approval commits while they are in flight.
    page.evaluate('() => {deferCount=true;refreshReportAvailability(true)}')
    page.wait_for_function('typeof releaseCount === "function"')
    requests = page.evaluate('countRequests')
    page.evaluate("api('/api/v86/final-version/show',{method:'PUT',body:JSON.stringify({entity_key:'tv:Show',final_version:true})})")
    page.evaluate('releaseCount()')
    page.wait_for_function("document.querySelector('[data-html-subtitle-report]').hidden && !document.querySelector('#reports').classList.contains('reports-checking')")
    assert page.evaluate('countRequests') == requests + 1, 'Old in-flight response was reused'
    assert page.evaluate('finalEvents') == 3
    page.evaluate("api('/api/v86/final-version').then(()=>api('/fixture/fail',{method:'PUT',body:'{}'})).catch(()=>{})")
    assert page.evaluate('finalEvents') == 3, 'Reads or failed requests changed eligibility'
    # Hidden pages must invalidate their cache without an eager count request.
    page.evaluate("document.querySelector('#reports').classList.add('hidden')")
    requests = page.evaluate('countRequests')
    page.evaluate("api('/api/v86/final-version',{method:'PUT',body:JSON.stringify({path:'/fixture.mkv',final_version:false})})")
    assert page.evaluate('countRequests') == requests
    page.evaluate("document.querySelector('#reports').classList.remove('hidden');document.querySelector('[data-page=reports]').click()")
    page.wait_for_function("!document.querySelector('[data-html-subtitle-report]').hidden")
    assert page.evaluate('countRequests') == requests + 1
    assert not errors, errors

    # Dashboard report findings obey the same cache invalidation rule.
    dashboard = browser.new_page()
    dashboard.on('pageerror', lambda error: errors.append(str(error)))
    dashboard.route('**/*', lambda route: route.fulfill(body='<html></html>'))
    dashboard.goto('http://fixture.invalid/')
    dashboard.set_content('''<section id="dashboard"><div class="page-title"></div>
        <div id="dashboard-content"></div></section>''')
    dashboard.evaluate('''() => {
        window.approved=false;window.esc=String;window.page=()=>{};window.formatAppTime=String;
        window.appTimezone=()=>'';window.dashboardBytes=String;
        window.api=async url=>url==='/api/v19/reports/availability'?
            {counts:{html:{movies:approved?0:1,tv:0}}}:
            {collection:{movies:{total:1,indexed:1,final:0,reviewed:0,changed:0},tv:{total:1,indexed:1,final:0,reviewed:0,changed:0,shows:1}},
             work:{tasks:[],indexes:[],preflight:[],running:[],index_paused:{},completed_hour:0},continue:[]};
    }''')
    dashboard.add_script_tag(content=(ROOT / 'app/static/dashboard.js').read_text())
    dashboard.wait_for_selector('[data-finding="html:movies"]')
    dashboard.evaluate("approved=true;document.dispatchEvent(new Event('media-final-version-changed'))")
    dashboard.wait_for_function("!document.querySelector('[data-finding=\"html:movies\"]')")
    dashboard.evaluate("approved=false;document.dispatchEvent(new Event('media-final-version-changed'))")
    dashboard.wait_for_selector('[data-finding="html:movies"]')
    assert not errors, errors
    browser.close()

print('PASS: final approval refreshes open reports and counts, unfreezing restores visibility, stale responses cannot reintroduce approved media, Dashboard findings refresh, no real writes')
