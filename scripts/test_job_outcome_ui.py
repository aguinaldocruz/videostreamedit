"""Offline browser regression for blocked inspection and terminal job labels."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page = browser.new_page()
    page.route('http://queue.test/**', lambda route: route.fulfill(body='<html></html>', content_type='text/html'))
    page.goto('http://queue.test/')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_content('''<main id="setup"><div class="setup-tabs"><button data-setup-tab="queue">Tasks</button></div>
      <div class="setup-tab-panels"><div data-setup-panel="queue"><div class="task-queue-maintenance"></div></div></div>
      <div class="index-maintenance"><div class="split-index-heading"><p></p></div></div></main>''')
    page.evaluate('''()=>{
      window.$=selector=>document.querySelector(selector);window.esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');window.attr=esc;
      window.formatAppDate=value=>value||'';window.toast=()=>{};window.opened=[];
      window.openEditor=(path,label)=>opened.push({path,label});
      window.api=async url=>url.includes('/core/')?{queued:0,running:0,failed:1,items:[{id:2,path:'/deleted.mkv',status:'failed',permanent_failure:true,error:'Permanent failure [media_missing]: deleted'}]}:
      {queued:1,running:0,blocked_cache:1,waiting_cache:1,items:[{id:1,path:'/damaged.mkv',status:'pending',display_status:'blocked_cache',review_path:'/damaged.mkv',error:'Blocked: subtitle cache failed; replacement characters need review'}]};
    }''')
    for name in ('v65-task-queue.js', 'v86-tasks-layout.js'):
        page.evaluate('source=>{new Function(source)}', (root/'app/static'/name).read_text())
    page.add_script_tag(content=(root/'app/static/v86-tasks-layout.js').read_text())
    page.locator('[data-tasks-tab="indexes"]').click()
    page.locator('[data-index-status="blocked_cache"]').wait_for()
    assert page.locator('[data-index-status="blocked_cache"]').inner_text() == 'Blocked: cache failed 1'
    page.locator('[data-index-status="blocked_cache"]').click()
    assert page.locator('[data-index-history-list]').inner_text().count('replacement characters need review') == 1
    page.locator('[data-index-review]').click()
    assert page.evaluate('opened[0].path') == '/damaged.mkv'
    page.locator('[data-index-status="failed"]').click()
    assert 'Permanent failure' in page.locator('[data-index-history-list]').inner_text()
    assert page.locator('[data-index-history-list] [data-index-retry]').count() == 0
    assert not errors, errors
    browser.close()
print('PASS: blocked-cache filter/count/reason/review, permanent failure without Retry, JavaScript syntax')
