"""Isolated browser schedule controls and stable paginated log viewer."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page=browser.new_page(viewport={'width':1150,'height':900});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.set_content('''<main id="setup"><div class="tasks-shell"><div class="tasks-tabs">
      <button data-tasks-tab="indexes">Indexes</button><button data-tasks-tab="priorities">Priorities</button></div>
      <section data-tasks-panel="indexes"><article data-index-job="core"><div class="index-schedule"></div></article>
      <article data-index-job="subtitles"><div class="index-schedule"></div></article><div class="plex-sync-schedule"></div></section>
      <section data-tasks-panel="priorities"></section></div></main>''')
    page.add_style_tag(content=(ROOT/'app/static/subtitle-cache-schedule.css').read_text()+' .hidden{display:none} button{min-height:2rem}')
    # localStorage needs a non-opaque origin. Keep all browser traffic fake.
    page.route('http://schedule.test/**',lambda r:r.fulfill(body='<html></html>',content_type='text/html'))
    markup=page.content();page.goto('http://schedule.test/');page.set_content(markup)
    page.evaluate('''()=>{window.requests=[];window.esc=s=>String(s);window.attr=s=>String(s);window.formatAppDate=s=>s;window.appTimezone=()=> 'America/Sao_Paulo';window.toast=()=>{};
      window.api=async(url,options={})=>{requests.push({url,options});
        if(url.includes('latest-log'))return {title:'Subtitle language detection',run:{run_id:'stable-run',status:'running',source:'manual',started_at:'2026-10-03T10:00:00Z',summary:'1 media queued'},entries:[{id:url.includes('before=')?1:2,created_at:'2026-10-03T10:00:00Z',level:'error',message:'Missing file <unsafe>',path:'/media/example.mkv',task_id:12,item_status:'failed',error:'Moved externally'}],more:!url.includes('before='),before:2};
        if(url.includes('run-now'))return {task_id:42,status:'pending'};
        if(url.includes('/schedule')&&url.includes('subtitle-cache'))return {frequency:'disabled',time:'01:00',hours:3,minutes:0,remaining_work:0};
        return {frequency:'disabled',time:'03:00'};};}''')
    page.add_script_tag(content=(ROOT/'app/static/v98-scheduled-tasks.js').read_text())
    for job in ('backup','preflight_cleanup'):
        page.locator('#scheduled-task-cards').evaluate('(e,job)=>{const card=document.createElement("section");card.dataset.scheduleActionsJob=job;card.className="scheduled-task-card";card.textContent=job;e.append(card)}',job)
    page.add_script_tag(content=(ROOT/'app/static/scheduled-task-actions.js').read_text())
    page.locator('[data-tasks-tab=schedules]').click()
    assert page.locator('#scheduled-task-cards>.scheduled-task-card').first.get_attribute('data-subtitle-cache')==''
    assert page.locator('[data-schedule-run]').count()==8
    assert page.locator('[data-schedule-log]').count()==8
    for job in ('core','subtitles','plex_sync','subtitle_detection','voice_detection','backup','preflight_cleanup'):
        page.locator(f'[data-schedule-run="{job}"]').click()
        page.wait_for_function('job=>requests.some(r=>r.url===`/api/scheduled-tasks/${job}/run-now`)',arg=job)
    page.locator('[data-schedule-log=subtitle_detection]').click()
    page.wait_for_function('document.querySelector("[data-log-summary]").textContent.includes("1 media queued")')
    assert page.locator('.scheduled-log-entry').count()==1
    assert page.locator('.scheduled-log-entry').inner_text().find('Missing file <unsafe>')>=0
    assert page.locator('.scheduled-log-entry unsafe').count()==0
    page.locator('[data-log-more]').click()
    page.wait_for_function('document.querySelectorAll(".scheduled-log-entry").length===2')
    assert page.evaluate('requests.some(r=>r.url.includes("run_id=stable-run"))')
    page.wait_for_timeout(700)
    assert page.locator('.scheduled-log-dialog').evaluate('d=>d.open')
    page.locator('[data-log-close]').click()
    page.locator('[data-schedule-log=core]').click()
    page.locator('[data-log-refresh]').click()
    page.wait_for_timeout(100)
    assert page.locator('.scheduled-log-entry').count()==1
    page.set_viewport_size({'width':390,'height':844})
    assert page.locator('.scheduled-log-dialog').evaluate('d=>d.getBoundingClientRect().right<=innerWidth')
    assert not errors,errors
    browser.close()
print('PASS: cache first, eight Run now/Latest log controls, correct requests, safe readable errors, stable pagination, refresh and narrow layout')
