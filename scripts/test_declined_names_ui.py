"""Live assets, mocked writes: declined-name maintenance and prompt flow."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page()
    errors=[]; writes=[]
    page.on('pageerror',lambda error: errors.append(str(error)))
    declined=[{'field':'title_audio','value':'Test declined name','use_count':3}]
    def route(r):
        url=r.request.url
        if '/api/v8/declined-track-names' in url:
            if r.request.method=='DELETE':
                writes.append('clear'); declined.clear()
            return r.fulfill(json={'values':declined})
        if r.request.method!='GET':
            if '/api/v8/saved-values' in url:
                writes.append(r.request.post_data_json); declined.clear()
            return r.fulfill(json={'prompts':[], 'items':{}, 'needs_attention':False,'reasons':[]})
        r.continue_()
    page.route('**/api/**',route)
    page.goto('http://127.0.0.1:8383',wait_until='domcontentloaded')
    page.wait_for_function('typeof window.openSetupDestination === "function"')
    page.evaluate("page('setup'); window.openSetupDestination('editing','suggestions')")
    page.get_by_text('Declined track names',exact=True).click()
    page.get_by_role('button',name='Add to saved names',exact=True).click()
    page.get_by_text('No declined track names.',exact=True).wait_for()
    assert writes[0]=={'field':'title_audio','value':'Test declined name','save':True}
    declined.append({'field':'title_subtitle','value':'Another declined','use_count':4})
    page.get_by_text('Declined track names',exact=True).click()
    page.get_by_text('Declined track names',exact=True).click()
    page.get_by_text('Subtitle · Another declined',exact=True).wait_for()
    page.on('dialog',lambda dialog: dialog.accept())
    page.get_by_role('button',name='Clear denied list',exact=True).click()
    page.get_by_text('No declined track names.',exact=True).wait_for()
    assert 'clear' in writes
    assert not errors, errors
    browser.close()
print('PASS: declined list visible, individual save, confirmed clear, no browser errors; all writes mocked')
